from app.config import settings
from app.email_client import (
    _assign_direction,
    _classify_document_kind,
    _derive_supplier_name,
    _domain_to_supplier_name,
    _extract_all_invoice_numbers,
    _extract_fields,
    _extract_invoice_number,
    _extract_invoice_number_from_subject,
    _is_own_company,
    _parse_amount_literal,
    _parse_amount_to_cents,
)
from app.models import Direction, DocumentKind


# -- Amount parsing --

def test_parse_amount_literal_dutch_notation():
    assert _parse_amount_literal("1.234,56") == 123456


def test_parse_amount_literal_plain_dot_decimal():
    assert _parse_amount_literal("39.60") == 3960


def test_parse_amount_literal_comma_decimal_no_thousands():
    assert _parse_amount_literal("39,60") == 3960


def test_parse_amount_literal_english_notation():
    assert _parse_amount_literal("1,234.56") == 123456


def test_parse_amount_literal_mixed_dots_no_comma_does_not_crash():
    # Seen in the wild from a jumbled PDF extraction -- both separators are
    # dots, so the last one must still win as the decimal point.
    assert _parse_amount_literal("1.151.15") == 115115


def test_amount_picks_total_incl_btw_over_smaller_numbers():
    text = "Aantal: 3 x EUR 1,60\nBTW 21%\nTotaal incl. BTW: EUR 39,60"
    assert _parse_amount_to_cents(text) == 3960


def test_amount_picks_te_betalen_label():
    text = "Subtotaal 1.000,00\nBTW 21,00\nTe betalen: € 1.021,00"
    assert _parse_amount_to_cents(text) == 102100


def test_amount_picks_totaalbedrag_label():
    text = "Regel 1: 5,00\nRegel 2: 10,00\nTotaalbedrag € 15,00"
    assert _parse_amount_to_cents(text) == 1500


def test_amount_handles_openstaand_label():
    text = "Factuurbedrag 100,00\nReeds betaald 20,00\nOpenstaand bedrag: EUR 80,00"
    assert _parse_amount_to_cents(text) == 8000


def test_amount_handles_amount_due_label():
    text = "Subtotal 50.00\nTax 10.50\nAmount Due: EUR 60.50"
    assert _parse_amount_to_cents(text) == 6050


def test_amount_fallback_requires_currency_marker():
    # No recognised total label anywhere: only a currency-marked number
    # should ever be picked, never a bare quantity like "3".
    text = "Aantal: 3\nEenheidsprijs: EUR 12,50"
    assert _parse_amount_to_cents(text) == 1250


def test_amount_returns_none_without_any_currency_or_label():
    text = "Aantal: 3\nEenheidsprijs: 12,50"
    assert _parse_amount_to_cents(text) is None


def test_amount_picks_last_number_on_label_line_not_first():
    # Real incasso-specificatie layout: a leading "0,00" placeholder column
    # is followed by the actual total on the same line.
    text = "Eindtotaal 0,00 197,17\nBetalingsdoc. Datum Valuta Betaling"
    assert _parse_amount_to_cents(text) == 19717


def test_amount_finds_value_on_the_next_line():
    text = "Totaalbedrag:\nEUR 245,00\nBetaaltermijn 14 dagen"
    assert _parse_amount_to_cents(text) == 24500


# -- Invoice number parsing --

def test_invoice_number_after_factuurnummer_label():
    text = "Factuurnummer: F-2026-00123\nFactuurdatum: 12-08-2026"
    assert _extract_invoice_number(text) == "F-2026-00123"


def test_invoice_number_rejects_label_words_in_between():
    # Jumbled PDF layout: "Factuurnummer" is immediately followed by the
    # NEXT label ("Factuurdatum") rather than the real value, because the
    # actual number sits in a table cell pdfplumber placed elsewhere.
    text = "Factuurnummer Factuurdatum 20260812 12345"
    result = _extract_invoice_number(text)
    assert result not in ("Factuurdatum", "Datum", "Factuur")
    assert any(ch.isdigit() for ch in result)


def test_invoice_number_rejects_bare_factuur_label_value():
    text = "FACTUUR\nNummer: 987654\nKlant: Bosstad Tweewielers"
    assert _extract_invoice_number(text) == "987654"


def test_invoice_number_invoice_no_label():
    text = "Invoice No. INV-9988\nDate: 2026-07-29"
    assert _extract_invoice_number(text) == "INV-9988"


def test_invoice_number_empty_when_nothing_found():
    text = "Bedankt voor uw bestelling. Levertijd 3-5 dagen."
    assert _extract_invoice_number(text) == ""


# -- Supplier name derivation --

def test_known_supplier_domain_kruitbosch():
    assert _domain_to_supplier_name("kruitbosch.nl") == "Kruitbosch"


def test_known_supplier_domain_vanderlaangroep():
    assert _domain_to_supplier_name("vanderlaangroep.nl") == "Van der Laan Groep"


def test_known_supplier_domain_with_subdomain():
    assert _domain_to_supplier_name("servicemail.essent.nl") == "Essent"


def test_unknown_domain_falls_back_to_prettified_label():
    assert _domain_to_supplier_name("acme-onderdelen.nl") == "Acme Onderdelen"


def test_derive_supplier_name_prefers_real_display_name():
    assert _derive_supplier_name("orders@somefirm.nl", "Some Firm B.V.") == "Some Firm B.V."


def test_derive_supplier_name_ignores_display_name_equal_to_address():
    assert _derive_supplier_name("noreply@kruitbosch.nl", "noreply@kruitbosch.nl") == "Kruitbosch"


def test_derive_supplier_name_falls_back_to_domain_when_no_display_name():
    assert _derive_supplier_name("noreply@kruitbosch.nl", "") == "Kruitbosch"


def test_derive_supplier_name_never_returns_raw_email_as_name():
    for address in [
        "noreply@kruitbosch.nl", "debtoradm@accell.nl", "notifications@tenways.com",
        "info@enra.nl", "noreply@twsc.nl", "noreply@gazelle.nl", "info@wrutteman.nl",
        "no-reply@hellorider.com", "noreply@servicemail.essent.nl", "info@fietsunie.nl",
        "facturering@vanderlaangroep.nl", "weesp@fietshuys.nl", "info@zuidewind-bv.nl",
        "webmaster.netherlands@retif.eu",
    ]:
        name = _derive_supplier_name(address, "")
        assert "@" not in name, f"{address} -> {name!r} still looks like an e-mail address"


# -- End to end --

def test_extract_fields_realistic_invoice_text():
    text = (
        "Kruitbosch B.V.\n"
        "Factuurnummer: 2026-778899\n"
        "Factuurdatum: 15 augustus 2026\n"
        "Omschrijving: Onderdelen\n"
        "Subtotaal: 100,00\n"
        "BTW 21%: 21,00\n"
        "Totaal incl. BTW: € 121,00\n"
    )
    fields = _extract_fields(text, "noreply@kruitbosch.nl", "")
    assert fields["supplier_name"] == "Kruitbosch"
    assert fields["invoice_number"] == "2026-778899"
    assert fields["amount_cents"] == 12100
    assert fields["direction"] == Direction.OUTGOING.value
    assert fields["document_kind"] == DocumentKind.INVOICE.value


# -- Direction --

def test_direction_defaults_to_outgoing():
    assert _assign_direction("Kruitbosch") == Direction.OUTGOING.value


def test_direction_incoming_for_configured_suppliers():
    assert _assign_direction("ENRA") == Direction.INCOMING.value
    assert _assign_direction("HelloRider") == Direction.INCOMING.value


def test_incoming_amount_label_gestort():
    text = "Rekening-courant overzicht\nSaldo vorige periode: 100,00\nGestort bedrag: EUR 245,00"
    assert _parse_amount_to_cents(text, direction=Direction.INCOMING.value) == 24500


# -- Own-company detection (verkoopfacturen die wij zelf versturen) --

def test_is_own_company_matches_configured_display_name(monkeypatch):
    monkeypatch.setattr(settings, "own_company_names", "Van der Linden Tweewielers,Hing B.V.")
    monkeypatch.setattr(settings, "graph_mailbox", "")
    assert _is_own_company("verzonden@example.com", "Van der Linden Tweewielers") is True
    assert _is_own_company("facturatie@boekhoudpakket.nl", "Hing B.V.") is True


def test_is_own_company_matches_own_mail_domain(monkeypatch):
    monkeypatch.setattr(settings, "own_company_names", "")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl,info@vanderlindentweewielers.nl")
    assert _is_own_company("info@vanderlindentweewielers.nl", "") is True
    assert _is_own_company("sacha@vanderlindentweewielers.nl", "") is True


def test_is_own_company_false_for_external_supplier(monkeypatch):
    monkeypatch.setattr(settings, "own_company_names", "Van der Linden Tweewielers,Hing B.V.")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")
    assert _is_own_company("noreply@kruitbosch.nl", "Kruitbosch B.V.") is False


def test_extract_fields_classifies_self_sent_sales_invoice_as_other(monkeypatch):
    monkeypatch.setattr(settings, "own_company_names", "Van der Linden Tweewielers,Hing B.V.")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl,info@vanderlindentweewielers.nl")
    text = (
        "Van der Linden Tweewielers\n"
        "Factuurnummer: V2026-0042\n"
        "Factuurdatum: 3 september 2026\n"
        "Totaal incl. BTW: € 899,00\n"
    )
    fields = _extract_fields(text, "info@vanderlindentweewielers.nl", "Van der Linden Tweewielers", "Factuur V2026-0042", "factuur.pdf")
    # A real invoice-looking document (has a number, an amount, "factuur" in
    # it) but sent by the shop itself -- never an open inkoopfactuur.
    assert fields["document_kind"] == DocumentKind.OTHER.value


# -- Document classification --

def test_general_terms_classified_as_other():
    text = "General Terms and Conditions of Sale\nTenways Technovation Europe B.V."
    assert _classify_document_kind("GTC B2B 2026.v1.pdf", "Terms", text, None) == DocumentKind.OTHER.value


def test_ubo_declaration_classified_as_other():
    text = "Uiteindelijk belanghebbende (UBO) verklaring\nWaarom dit formulier?"
    assert _classify_document_kind("ENRA - UBO verklaring.pdf", "", text, None) == DocumentKind.OTHER.value


def test_bank_account_change_notice_classified_as_other():
    text = "We are writing to inform you of a change in our payment bank account details."
    assert _classify_document_kind("Change in Payment Bank Account.pdf", "", text, None) == DocumentKind.OTHER.value


def test_packing_slip_without_amount_classified_as_other():
    text = "Pakbon\nAantal geleverde stuks: 4\nGeen bedragen op dit document."
    assert _classify_document_kind("pakbon.pdf", "", text, None) == DocumentKind.OTHER.value


def test_packing_slip_with_amount_stays_invoice():
    text = "Pakbon met factuurgegevens\nTotaal incl. BTW: EUR 50,00"
    assert _classify_document_kind("pakbon.pdf", "", text, 5000) == DocumentKind.INVOICE.value


def test_real_invoice_not_misclassified_as_other():
    text = "Factuurnummer: 123\nTotaal incl. BTW: EUR 50,00"
    assert _classify_document_kind("factuur.pdf", "Factuur 123", text, 5000) == DocumentKind.INVOICE.value


def test_specification_detected_and_lists_all_invoice_numbers():
    text = (
        "Specificatie automatische incasso\n"
        "Factuurnummer: F-100 Bedrag: 50,00\n"
        "Factuurnummer: F-200 Bedrag: 75,00\n"
        "Totaal incl. BTW: EUR 125,00"
    )
    kind = _classify_document_kind("specificatie.pdf", "", text, 12500)
    assert kind == DocumentKind.SPECIFICATION.value
    assert _extract_all_invoice_numbers(text) == ["F-100", "F-200"]


# -- Subject fallback for invoice number (Tenways) --

def test_invoice_number_from_subject_ref():
    subject = "Tenways Technovation Europe B.V. Invoice (Ref INV/2026/23162)"
    assert _extract_invoice_number_from_subject(subject) == "INV/2026/23162"


def test_extract_fields_falls_back_to_subject_when_text_has_no_number():
    text = "Bedankt voor uw bestelling.\nTotaal incl. BTW: EUR 40,00"
    subject = "Tenways Technovation Europe B.V. Invoice (Ref INV/2026/23162)"
    fields = _extract_fields(text, "notifications@tenways.com", "", subject, "invoice.pdf")
    assert fields["invoice_number"] == "INV/2026/23162"
