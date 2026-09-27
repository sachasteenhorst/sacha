from app.email_client import (
    _derive_supplier_name,
    _domain_to_supplier_name,
    _extract_fields,
    _extract_invoice_number,
    _parse_amount_literal,
    _parse_amount_to_cents,
)


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
