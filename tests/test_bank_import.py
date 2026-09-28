from datetime import date

import pytest

from app.bank_import import (
    BankImportError,
    parse_bank_file,
    parse_camt053,
    parse_mt940,
    parse_rabobank_csv,
)

RABO_CSV_HEADER = (
    "IBAN/BBAN;Munt;BIC;Volgnr;Datum;Rentedatum;Bedrag;Saldo na trn;"
    "Tegenrekening IBAN/BBAN;Naam tegenpartij;Naam uiteindelijke partij;"
    "Naam initiërende partij;BIC tegenpartij;Code;Batch ID;Transactiereferentie;"
    "Machtigingskenmerk;Incassant ID;Betalingskenmerk;Omschrijving-1;"
    "Omschrijving-2;Omschrijving-3;Reden retour;Oorspr bedrag;Oorspr munt;Koers"
)


def _rabo_csv_row(**overrides) -> str:
    defaults = {
        "iban": "NL00RABO0123456789", "munt": "EUR", "bic": "RABONL2U", "volgnr": "1",
        "datum": "15-08-2026", "rentedatum": "15-08-2026", "bedrag": "-39,60",
        "saldo": "1000,00", "tegen_iban": "NL11ABCD0123456789", "naam": "Kruitbosch",
        "naam2": "", "naam3": "", "bic2": "", "code": "", "batch": "", "txref": "",
        "machtiging": "", "incassant": "", "betalingskenmerk": "",
        "oms1": "Factuur 12345", "oms2": "", "oms3": "", "retour": "",
        "oorspr_bedrag": "", "oorspr_munt": "", "koers": "",
    }
    defaults.update(overrides)
    return ";".join([
        defaults["iban"], defaults["munt"], defaults["bic"], defaults["volgnr"],
        defaults["datum"], defaults["rentedatum"], defaults["bedrag"], defaults["saldo"],
        defaults["tegen_iban"], defaults["naam"], defaults["naam2"], defaults["naam3"],
        defaults["bic2"], defaults["code"], defaults["batch"], defaults["txref"],
        defaults["machtiging"], defaults["incassant"], defaults["betalingskenmerk"],
        defaults["oms1"], defaults["oms2"], defaults["oms3"], defaults["retour"],
        defaults["oorspr_bedrag"], defaults["oorspr_munt"], defaults["koers"],
    ])


def test_rabobank_csv_parses_outgoing_payment():
    content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row() + "\n").encode("utf-8-sig")
    rows = parse_rabobank_csv(content)
    assert len(rows) == 1
    row = rows[0]
    assert row.booking_date == date(2026, 8, 15)
    assert row.amount_cents == -3960
    assert row.counterparty_name == "Kruitbosch"
    assert "Factuur 12345" in row.description


def test_rabobank_csv_parses_incoming_payment():
    content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row(bedrag="250,00", naam="ENRA") + "\n").encode("utf-8-sig")
    rows = parse_rabobank_csv(content)
    assert rows[0].amount_cents == 25000


def test_rabobank_csv_skips_duplicate_free_dedup_ref_is_stable():
    content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row() + "\n" + _rabo_csv_row() + "\n").encode("utf-8-sig")
    rows = parse_rabobank_csv(content)
    assert len(rows) == 2
    assert rows[0].external_ref == rows[1].external_ref  # identical rows -> identical dedup key


def test_rabobank_csv_missing_columns_raises_clear_error():
    content = b"Foo;Bar\n1;2\n"
    with pytest.raises(BankImportError, match="Datum"):
        parse_rabobank_csv(content)


CAMT053_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Ntry>
        <Amt Ccy="EUR">39.60</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-08-15</Dt></BookgDt>
        <AcctSvcrRef>REF001</AcctSvcrRef>
        <NtryDtls>
          <TxDtls>
            <RmtInf><Ustrd>Factuur 12345</Ustrd></RmtInf>
            <RltdPties>
              <Cdtr><Nm>Kruitbosch</Nm></Cdtr>
              <CdtrAcct><Id><IBAN>NL11ABCD0123456789</IBAN></Id></CdtrAcct>
            </RltdPties>
          </TxDtls>
        </NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">250.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-08-16</Dt></BookgDt>
        <AcctSvcrRef>REF002</AcctSvcrRef>
        <NtryDtls>
          <TxDtls>
            <RmtInf><Ustrd>Rekening-courant</Ustrd></RmtInf>
            <RltdPties>
              <Dbtr><Nm>ENRA</Nm></Dbtr>
              <DbtrAcct><Id><IBAN>NL22WXYZ0987654321</IBAN></Id></DbtrAcct>
            </RltdPties>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_camt053_parses_outgoing_debit_entry():
    rows = parse_camt053(CAMT053_SAMPLE.encode("utf-8"))
    outgoing = next(r for r in rows if r.amount_cents < 0)
    assert outgoing.amount_cents == -3960
    assert outgoing.booking_date == date(2026, 8, 15)
    assert outgoing.counterparty_name == "Kruitbosch"
    assert outgoing.counterparty_iban == "NL11ABCD0123456789"
    assert outgoing.reference == "REF001"


def test_camt053_parses_incoming_credit_entry():
    rows = parse_camt053(CAMT053_SAMPLE.encode("utf-8"))
    incoming = next(r for r in rows if r.amount_cents > 0)
    assert incoming.amount_cents == 25000
    assert incoming.counterparty_name == "ENRA"


def test_camt053_invalid_xml_raises_clear_error():
    with pytest.raises(BankImportError):
        parse_camt053(b"not xml at all")


def test_camt053_no_entries_raises_clear_error():
    empty = b'<?xml version="1.0"?><Document><BkToCstmrStmt><Stmt></Stmt></BkToCstmrStmt></Document>'
    with pytest.raises(BankImportError, match="Ntry"):
        parse_camt053(empty)


MT940_SAMPLE = (
    ":20:STARTREF\r\n"
    ":25:NL00RABO0123456789\r\n"
    ":28C:00001/001\r\n"
    ":60F:C260814EUR1000,00\r\n"
    ":61:2608150815D39,60NTRFNONREF\r\n"
    ":86:/TRCD/00100/NAME/Kruitbosch/REMI/Factuur 12345\r\n"
    ":61:2608160816C250,00NTRFNONREF\r\n"
    ":86:/TRCD/00200/NAME/ENRA/REMI/Rekening-courant\r\n"
    ":62F:C260816EUR1210,40\r\n"
)


def test_mt940_parses_debit_and_credit_lines():
    rows = parse_mt940(MT940_SAMPLE.encode("utf-8"))
    assert len(rows) == 2
    debit, credit = rows[0], rows[1]
    assert debit.amount_cents == -3960
    assert debit.booking_date == date(2026, 8, 15)
    assert credit.amount_cents == 25000


def test_mt940_extracts_name_subfield_from_86_line():
    rows = parse_mt940(MT940_SAMPLE.encode("utf-8"))
    assert rows[0].counterparty_name == "Kruitbosch"
    assert rows[1].counterparty_name == "ENRA"


def test_mt940_no_entries_raises_clear_error():
    with pytest.raises(BankImportError):
        parse_mt940(b":20:REF\r\n:25:NL00RABO0123456789\r\n")


def test_dispatch_by_extension():
    csv_content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row() + "\n").encode("utf-8-sig")
    assert len(parse_bank_file("export.csv", csv_content)) == 1
    assert len(parse_bank_file("statement.XML", CAMT053_SAMPLE.encode("utf-8"))) == 2
    assert len(parse_bank_file("statement.swi", MT940_SAMPLE.encode("utf-8"))) == 2


def test_dispatch_unknown_extension_raises():
    with pytest.raises(BankImportError):
        parse_bank_file("statement.txt", b"whatever")
