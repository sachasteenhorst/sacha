"""Imports a CycleSoftware (kassasysteem) verkoopfacturen-export (CSV or
XLSX) as Invoice rows with document_kind=SALES_INVOICE -- so a bijschrijving
from a customer, HelloRider, Lease a Bike/Mobility Services or VWPFS can be
matched to the actual sales invoice it settles instead of only falling into
a generic "omzet" Rule.

Column names aren't fixed -- CycleSoftware's own export can differ per
export template/version, so every column is matched flexibly (several
candidate header spellings, case-insensitive) the same way
app/bank_import.py already does for the Rabobank CSV.

This is a manual, one-off (or repeated-by-hand) upload -- for a future live
API integration, see app/cyclesoftware_api.py.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Direction, DocumentKind, Invoice, MatchStatus

SALES_INVOICE_ATTACHMENT_NAME = "cyclesoftware-export"

_INVOICE_NUMBER_COLS = ["factuurnummer", "factuur nr", "factuur", "nummer", "invoice", "invoice number", "invoicenumber"]
_DATE_COLS = ["datum", "factuurdatum", "date"]
_CUSTOMER_COLS = ["klant", "klantnaam", "customer", "naam", "debiteur"]
_AMOUNT_COLS = ["bedrag", "totaal", "factuurbedrag", "amount", "total"]
_OUTSTANDING_COLS = ["openstaand", "openstaand bedrag", "outstanding", "te ontvangen", "restant"]


class CycleSoftwareImportError(RuntimeError):
    pass


@dataclass
class CycleSoftwareImportResult:
    new_invoices: int = 0
    skipped: int = 0


@dataclass
class SalesInvoiceRow:
    invoice_number: str
    invoice_date: date | None
    customer_name: str
    amount_cents: int | None


def _find_column(fieldnames: list[str], candidates: list[str]) -> str | None:
    lower_map = {f.strip().lower(): f for f in fieldnames}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw) -> int | None:
    """Handles a bare integer euro amount ("500", from a numeric XLSX cell
    or a CSV column with no decimals at all) as well as Dutch/plain decimal
    notation ("1.234,56" / "1,234.56") -- unlike
    app.email_client._parse_amount_literal, which assumes its input already
    matched a "always has 2 decimals" regex, this is fed raw spreadsheet
    cells that may have neither."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw) * 100)
    raw = str(raw).strip().replace("€", "").replace("EUR", "").strip()
    if not raw:
        return None
    last_sep = max(raw.rfind("."), raw.rfind(","))
    try:
        if last_sep == -1:
            return round(float(raw) * 100)
        integer_part = re.sub(r"[.,]", "", raw[:last_sep]) or "0"
        decimal_part = (raw[last_sep + 1:] + "00")[:2]
        return round(float(f"{integer_part}.{decimal_part}") * 100)
    except ValueError:
        return None


def _rows_from_csv(content: bytes) -> tuple[list[str], list[dict]]:
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise CycleSoftwareImportError("Kon de tekencodering van het bestand niet herkennen.")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise CycleSoftwareImportError("Geen kolommen gevonden in het bestand.")
    return list(reader.fieldnames), list(reader)


def _rows_from_xlsx(content: bytes) -> tuple[list[str], list[dict]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise CycleSoftwareImportError(
            "XLSX-ondersteuning ontbreekt (openpyxl niet geinstalleerd) -- exporteer als CSV, "
            "of installeer openpyxl."
        ) from exc

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    except StopIteration:
        raise CycleSoftwareImportError("Geen rijen gevonden in het Excel-bestand.")
    rows = [dict(zip(header, row)) for row in rows_iter if any(v is not None for v in row)]
    return header, rows


def _parse_rows(filename: str, content: bytes) -> list[SalesInvoiceRow]:
    name = filename.lower()
    if name.endswith(".csv"):
        fieldnames, raw_rows = _rows_from_csv(content)
    elif name.endswith((".xlsx", ".xlsm")):
        fieldnames, raw_rows = _rows_from_xlsx(content)
    else:
        raise CycleSoftwareImportError(f"Onbekend bestandstype voor '{filename}'. Ondersteund: .csv, .xlsx.")

    number_col = _find_column(fieldnames, _INVOICE_NUMBER_COLS)
    date_col = _find_column(fieldnames, _DATE_COLS)
    customer_col = _find_column(fieldnames, _CUSTOMER_COLS)
    amount_col = _find_column(fieldnames, _AMOUNT_COLS)
    outstanding_col = _find_column(fieldnames, _OUTSTANDING_COLS)
    if not number_col or not amount_col:
        raise CycleSoftwareImportError(
            "Verwachte kolommen 'Factuurnummer' en/of 'Bedrag' niet gevonden. "
            f"Gevonden kolommen: {', '.join(fieldnames)}"
        )

    rows: list[SalesInvoiceRow] = []
    for raw_row in raw_rows:
        invoice_number = str(raw_row.get(number_col) or "").strip()
        if not invoice_number:
            continue
        outstanding_cents = _parse_amount(raw_row.get(outstanding_col)) if outstanding_col else None
        amount_cents = _parse_amount(raw_row.get(amount_col))
        # A customer only ever transfers what's still outstanding -- match on
        # that when the export gives it, falling back to the invoice total.
        effective_amount = outstanding_cents if outstanding_cents else amount_cents
        rows.append(
            SalesInvoiceRow(
                invoice_number=invoice_number,
                invoice_date=_parse_date(str(raw_row.get(date_col) or "")) if date_col else None,
                customer_name=str(raw_row.get(customer_col) or "").strip() if customer_col else "",
                amount_cents=effective_amount,
            )
        )
    return rows


def import_sales_invoices(session: Session, filename: str, content: bytes) -> CycleSoftwareImportResult:
    rows = _parse_rows(filename, content)
    if not rows:
        raise CycleSoftwareImportError("Geen verkoopfacturen gevonden in dit bestand.")

    result = CycleSoftwareImportResult()
    existing_keys = set(
        session.execute(select(Invoice.email_message_id, Invoice.attachment_filename)).all()
    )

    for row in rows:
        message_id = f"cyclesoftware:{row.invoice_number}"
        key = (message_id, SALES_INVOICE_ATTACHMENT_NAME)
        if key in existing_keys:
            result.skipped += 1
            continue
        existing_keys.add(key)

        content_hash = hashlib.sha256(
            f"{row.invoice_number}|{row.customer_name}|{row.amount_cents}".encode("utf-8")
        ).hexdigest()

        session.add(
            Invoice(
                email_message_id=message_id,
                attachment_filename=SALES_INVOICE_ATTACHMENT_NAME,
                email_subject=f"CycleSoftware verkoopfactuur {row.invoice_number}",
                email_from="cyclesoftware-export",
                received_at=datetime.combine(row.invoice_date, datetime.min.time()) if row.invoice_date else datetime.utcnow(),
                invoice_number=row.invoice_number,
                invoice_date=row.invoice_date,
                supplier_name=row.customer_name or "Onbekende klant",
                amount_cents=row.amount_cents,
                direction=Direction.INCOMING.value,
                document_kind=DocumentKind.SALES_INVOICE.value,
                content_hash=content_hash,
                status=MatchStatus.UNMATCHED,
            )
        )
        result.new_invoices += 1

    return result
