from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cyclesoftware_upload import CycleSoftwareImportError, import_sales_invoices
from app.db import Base
from app.models import Direction, DocumentKind, Invoice, MatchStatus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


CSV_HEADER = "Factuurnummer;Datum;Klant;Bedrag;Openstaand"


def test_import_creates_sales_invoice_with_expected_fields(session):
    content = (CSV_HEADER + "\nCS-001;15-09-2026;Jan de Klant;123,45;123,45\n").encode("utf-8")

    result = import_sales_invoices(session, "export.csv", content)
    session.commit()

    assert result.new_invoices == 1
    inv = session.query(Invoice).one()
    assert inv.invoice_number == "CS-001"
    assert inv.invoice_date == date(2026, 9, 15)
    assert inv.supplier_name == "Jan de Klant"
    assert inv.amount_cents == 12345
    assert inv.direction == Direction.INCOMING.value
    assert inv.document_kind == DocumentKind.SALES_INVOICE.value
    assert inv.status == MatchStatus.UNMATCHED


def test_import_uses_outstanding_amount_over_invoice_total_when_present(session):
    # A partially-settled invoice (e.g. via another channel) -- the customer
    # only ever transfers what's still outstanding, so matching must use that.
    content = (CSV_HEADER + "\nCS-002;01-09-2026;Klant B;100,00;40,00\n").encode("utf-8")

    import_sales_invoices(session, "export.csv", content)
    session.commit()

    inv = session.query(Invoice).one()
    assert inv.amount_cents == 4000


def test_import_recognizes_alternative_column_spellings(session):
    # CycleSoftware's own export template can differ -- "Factuur nr",
    # "Customer", "Totaal" should all be recognised just as well.
    header = "Factuur nr;Datum;Customer;Totaal"
    content = (header + "\nCS-010;01-09-2026;HelloRider;50,00\n").encode("utf-8")

    result = import_sales_invoices(session, "export.csv", content)
    assert result.new_invoices == 1
    inv = session.query(Invoice).one()
    assert inv.invoice_number == "CS-010"
    assert inv.supplier_name == "HelloRider"
    assert inv.amount_cents == 5000


def test_import_skips_rows_already_imported_on_reupload(session):
    content = (CSV_HEADER + "\nCS-001;15-09-2026;Jan de Klant;123,45;123,45\n").encode("utf-8")
    import_sales_invoices(session, "export.csv", content)
    session.commit()

    result = import_sales_invoices(session, "export.csv", content)
    session.commit()

    assert result.new_invoices == 0
    assert result.skipped == 1
    assert session.query(Invoice).count() == 1


def test_import_missing_required_columns_raises_clear_error(session):
    content = b"Onbekende kolom;Nog een kolom\nfoo;bar\n"
    with pytest.raises(CycleSoftwareImportError):
        import_sales_invoices(session, "export.csv", content)


def test_import_empty_file_raises_error(session):
    content = (CSV_HEADER + "\n").encode("utf-8")
    with pytest.raises(CycleSoftwareImportError):
        import_sales_invoices(session, "export.csv", content)


def test_import_unsupported_extension_raises_error(session):
    with pytest.raises(CycleSoftwareImportError):
        import_sales_invoices(session, "export.txt", b"whatever")


def test_import_xlsx_file(session):
    openpyxl = pytest.importorskip("openpyxl")
    import io

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Factuurnummer", "Datum", "Klant", "Bedrag", "Openstaand"])
    sheet.append(["CS-XL-1", "01-09-2026", "Xlsx Klant", 99.5, 99.5])
    buf = io.BytesIO()
    workbook.save(buf)

    result = import_sales_invoices(session, "export.xlsx", buf.getvalue())
    assert result.new_invoices == 1
    inv = session.query(Invoice).one()
    assert inv.invoice_number == "CS-XL-1"
    assert inv.amount_cents == 9950
