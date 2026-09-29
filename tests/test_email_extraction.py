from datetime import date, datetime

from app.config import settings
from app.email_client import (
    _assign_direction,
    _classify_document_kind,
    _derive_supplier_name,
    _domain_to_supplier_name,
    _extract_all_invoice_numbers,
    _extract_due_date,
    _extract_fields,
    _extract_invoice_number,
    _extract_invoice_number_from_filename,
    _extract_invoice_number_from_subject,
    _is_own_company,
    _parse_amount_literal,
    _parse_amount_to_cents,
    _text_suggests_incasso,
    invoice_dedup_key,
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


def test_amount_bare_totaal_label_with_euro_sign_between_label_and_incl_btw():
    # Real Vlechtservice layout: the € sits BETWEEN "Totaal" and "incl.
    # btw", breaking the more specific "totaal incl btw" pattern.
    text = "SUBTOTAAL 204,95\nBTW-BEDRAG 43,04\nTOTAAL € INCL. BTW 247,99"
    assert _parse_amount_to_cents(text) == 24799


def test_amount_bare_totaal_label_with_trailing_euro_sign():
    # Real Mobility Services/Lease a Bike layout: the € trails the amount
    # instead of leading it, and "Totaal" has nothing else on its own line.
    text = "Subtotaal\n4.136,19 €\nbtw (21%)\n868,60 €\nTotaal\n5.004,79 €\nIBAN: NL44 RABO 0199970777"
    assert _parse_amount_to_cents(text) == 500479


def test_amount_bare_totaal_does_not_match_subtotaal():
    # "Subtotaal" must never satisfy the bare \btotaal\b fallback -- there's
    # no word boundary between "Sub" and "totaal".
    text = "Subtotaal 100,00"
    assert _parse_amount_to_cents(text) is None


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


def test_invoice_number_rejects_short_token_like_internet_speed():
    # Real production bug: an Mbps invoice's "Nummer: 28" got accepted as
    # the invoice number -- far too short/generic to trust, and exactly the
    # kind of value that later turns up as a coincidental substring
    # elsewhere (matcher.py enforces the same minimum length).
    text = "Mbps\nNummer: 28\nAbonnement internet"
    assert _extract_invoice_number(text) == ""


def test_invoice_number_prefers_longer_number_over_short_one():
    text = "Nummer: 08\nFactuurnummer: 2026-445566"
    assert _extract_invoice_number(text) == "2026-445566"


def test_invoice_number_prefers_filename_over_incassant_id():
    # Real Kruitbosch failure mode: PDF-layout column collapse puts the
    # incassant ID right after the "Factuurnummer" label instead of the real
    # number, which is a short mostly-numeric token ("306228") -- the real
    # number ("VFNL002280953") is recoverable from the filename.
    text = "Factuurnummer 306228C Kenmerk machtiging / incassant ID"
    filename = "Kruitbosch Factuur VFNL002280953_1.pdf"
    assert _extract_invoice_number(text, filename) == "VFNL002280953"


def test_invoice_number_falls_back_to_id_like_token_without_filename():
    # No filename to cross-check against -- better a possibly-wrong guess
    # than nothing, same as before this fix.
    text = "Factuurnummer 306228C Kenmerk machtiging / incassant ID"
    assert _extract_invoice_number(text) == "306228C"


def test_invoice_number_real_label_not_overridden_by_filename():
    text = "Factuurnummer: F-2026-00123\nFactuurdatum: 12-08-2026"
    filename = "Some Supplier Factuur F-2026-00123.pdf"
    assert _extract_invoice_number(text, filename) == "F-2026-00123"


def test_extract_invoice_number_from_filename_picks_longest_meaningful_token():
    assert _extract_invoice_number_from_filename("Kruitbosch Factuur VFNL002280953_1.pdf") == "VFNL002280953"
    assert _extract_invoice_number_from_filename("factuur.pdf") == ""
    assert _extract_invoice_number_from_filename("") == ""


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
    assert _assign_direction("Mobility Services") == Direction.INCOMING.value


def test_mobility_services_lease_a_bike_invoice_is_incoming_with_amount():
    # Real production case: this platform's "factuur" is actually a
    # verkoopfactuur the shop sends to VWPFS (a leasing company) -- the IBAN
    # in the document is the shop's OWN account, so it's money coming in.
    text = (
        "Nummer: 22533\nFactuur\nVWPFS Van der Linden Tweewielers\n"
        "Subtotaal\n4.136,19 €\nbtw (21%)\n868,60 €\nTotaal\n5.004,79 €\n"
        "IBAN: NL44 RABO 0199970777"
    )
    fields = _extract_fields(text, "support@mobility-services.bike", "")
    assert fields["direction"] == Direction.INCOMING.value
    assert fields["amount_cents"] == 500479
    assert fields["supplier_name"] == "Mobility Services"


def test_incoming_amount_label_gestort():
    text = "Rekening-courant overzicht\nSaldo vorige periode: 100,00\nGestort bedrag: EUR 245,00"
    assert _parse_amount_to_cents(text, direction=Direction.INCOMING.value) == 24500


# -- ENRA "Saldo RC" reference (rekening-courant overzicht) --

def test_enra_saldo_rc_reference_and_amount_extracted():
    # Real production layout: the corresponding bank bijschrijving's own
    # description is literally "Saldo RC <datum> Agentnr. <nr>" -- no
    # separate "total" label exists the way an invoice has one.
    text = (
        "Rekening courant\n"
        "Periode 22-05-2026 t/m 29-05-2026\n"
        "Beginsaldo per 22 mei 2026 454,78\n"
        "26-05-2026 Saldo RC 22-05-2026 Agentnr. 06343 454,78\n"
    )
    fields = _extract_fields(text, "info@enra.nl", "")
    assert fields["direction"] == Direction.INCOMING.value
    assert fields["invoice_number"] == "Saldo RC 22-05-2026 Agentnr. 06343"
    assert fields["amount_cents"] == 45478


def test_enra_saldo_rc_not_applied_to_outgoing_suppliers():
    # The "Saldo RC" reference is only meaningful for incoming documents --
    # an outgoing supplier whose text happens to contain similar-looking
    # text should never have this override kick in.
    text = "Factuurnummer: F-2026-00123\nSaldo RC 22-05-2026 Agentnr. 06343 454,78\nTotaal incl. BTW: EUR 50,00"
    fields = _extract_fields(text, "noreply@kruitbosch.nl", "")
    assert fields["direction"] == Direction.OUTGOING.value
    assert fields["invoice_number"] == "F-2026-00123"
    assert fields["amount_cents"] == 5000


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


# -- Dedup key (same invoice fetched twice, once per scanned mailbox) --

def test_dedup_key_prefers_content_hash():
    a = invoice_dedup_key("abc123", "Kruitbosch", "VFNL1", 1000)
    b = invoice_dedup_key("abc123", "Different Name", "OTHER", 999)
    assert a == b  # same PDF bytes -> same identity, regardless of extracted fields


def test_dedup_key_falls_back_to_supplier_number_amount():
    a = invoice_dedup_key("", "Kruitbosch", "VFNL002280953", 59010)
    b = invoice_dedup_key("", "kruitbosch", "vfnl002280953", 59010)  # case-insensitive
    assert a == b


def test_dedup_key_differs_for_different_invoices():
    a = invoice_dedup_key("", "Kruitbosch", "VFNL1", 1000)
    b = invoice_dedup_key("", "Kruitbosch", "VFNL2", 1000)
    assert a != b


def test_dedup_key_none_when_nothing_to_identify_by():
    assert invoice_dedup_key("", "Kruitbosch", "", None) is None
    assert invoice_dedup_key("", "Kruitbosch", "VFNL1", None) is None


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


# Real production bug: Accell's invoice/specification footer references
# their own terms ("... gelden onze algemene voorwaarden ...") on every
# document they send, including real invoices -- that's not the same as
# the document BEING a terms-and-conditions document.
ACCELL_FOOTER = (
    "Accell Western Europe is een handelsnaam van Accell Nederland B.V.\n"
    "Op alle transacties van Accell Nederland B.V. (statutair gevestigd te Heerenveen) "
    "gelden onze algemene voorwaarden, gedeponeerd bij de Kamer van Koophandel onder "
    "nummer: 01054298. Btw-nummer: NL 008084531B01 IBAN: NL39 ABNA 0474 3252 02"
)


def test_footer_terms_reference_does_not_classify_invoice_as_other():
    text = "Factuurnummer: 251103005\nTotaal incl. BTW: EUR 204,28\n" + ACCELL_FOOTER
    assert _classify_document_kind("251103005.pdf", "Factuur Accell NL per pakbon\xa0251103005", text, 20428) == DocumentKind.INVOICE.value


def test_footer_terms_reference_does_not_block_specification_classification():
    text = "Specificatie automatische incasso\n" + ACCELL_FOOTER
    result = _classify_document_kind("3458514.pdf", "Specificatie automatische incasso\xa03458514", text, 286973)
    assert result == DocumentKind.SPECIFICATION.value


def test_footer_terms_reference_does_not_yield_kvk_number_as_invoice_number():
    assert _extract_invoice_number(ACCELL_FOOTER) == ""


def test_genuine_terms_document_still_classified_as_other():
    # The boilerplate-reference exception must not swallow a document that
    # really IS the terms and conditions.
    text = "Algemene Voorwaarden\nArtikel 1. Toepasselijkheid\nDeze voorwaarden zijn van toepassing op alle overeenkomsten."
    assert _classify_document_kind("algemene-voorwaarden.pdf", "", text, None) == DocumentKind.OTHER.value


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


# -- Due date ("Nog te betalen") --

def test_due_date_from_explicit_vervaldatum_label():
    text = "Factuurnummer: F-1\nFactuurdatum: 01-09-2026\nVervaldatum: 15-09-2026\nTotaal incl. BTW: EUR 40,00"
    due, estimated = _extract_due_date(text, date(2026, 9, 1), datetime(2026, 9, 1))
    assert due == date(2026, 9, 15)
    assert estimated is False


def test_due_date_from_te_betalen_voor_label_with_accent():
    text = "Te betalen vóór 20-10-2026\nTotaal: EUR 40,00"
    due, estimated = _extract_due_date(text, date(2026, 10, 1), datetime(2026, 10, 1))
    assert due == date(2026, 10, 20)
    assert estimated is False


def test_due_date_from_payment_term_days_adds_to_invoice_date():
    text = "Betalingstermijn: 14 dagen\nTotaal: EUR 40,00"
    due, estimated = _extract_due_date(text, date(2026, 1, 1), datetime(2026, 1, 1))
    assert due == date(2026, 1, 15)
    assert estimated is False


def test_due_date_from_binnen_n_dagen_phrasing():
    text = "Gelieve te betalen binnen 30 dagen na factuurdatum.\nTotaal: EUR 40,00"
    due, estimated = _extract_due_date(text, date(2026, 3, 1), datetime(2026, 3, 1))
    assert due == date(2026, 3, 31)
    assert estimated is False


def test_due_date_falls_back_to_plus_30_days_and_is_flagged_estimated():
    text = "Geen vervaldatum of betalingstermijn hier.\nTotaal: EUR 40,00"
    due, estimated = _extract_due_date(text, date(2026, 6, 1), datetime(2026, 6, 1))
    assert due == date(2026, 7, 1)
    assert estimated is True


def test_due_date_falls_back_to_received_at_when_no_invoice_date():
    text = "Totaal: EUR 40,00"
    due, estimated = _extract_due_date(text, None, datetime(2026, 6, 1))
    assert due == date(2026, 7, 1)
    assert estimated is True


def test_extract_fields_includes_due_date_and_incasso_hint():
    text = "Factuurnummer: F-1\nVervaldatum: 10-09-2026\nDeze factuur wordt automatisch afgeschreven.\nTotaal: EUR 40,00"
    fields = _extract_fields(text, "noreply@kruitbosch.nl", "", "", "f.pdf", datetime(2026, 9, 1))
    assert fields["due_date"] == date(2026, 9, 10)
    assert fields["due_date_estimated"] is False
    assert fields["incasso_hint"] is True


# -- Incasso text hint --

def test_text_suggests_incasso_variants():
    assert _text_suggests_incasso("Dit bedrag wordt automatisch afgeschreven van uw rekening.") is True
    assert _text_suggests_incasso("Betaling via automatische incasso.") is True
    assert _text_suggests_incasso("Machtiging tot incasso: NL68ZZZ010542980000") is True
    assert _text_suggests_incasso("Gewone factuur, graag overmaken binnen 30 dagen.") is False
