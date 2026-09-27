"""Fetches invoice PDFs from a mailbox over IMAP and extracts the fields we
need for matching (amount, invoice date, invoice number, supplier).

PDF text extraction is heuristic (regex over the extracted text). It will
not get every invoice layout right -- fields it can't find are left empty
and the invoice still shows up in the dashboard for you to fill in by hand.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

import pdfplumber

from app.config import settings

INVOICE_DIR = "./data/invoices"

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}

AMOUNT_LABEL_RE = re.compile(
    r"(?:totaal(?:bedrag)?|te betalen|eindtotaal|invoice total|amount due)\D{0,15}"
    r"(?:€|eur)?\s*([\d.,]+)",
    re.IGNORECASE,
)
ANY_AMOUNT_RE = re.compile(r"€\s*([\d]{1,3}(?:[.,]\d{3})*[.,]\d{2})")
INVOICE_NUMBER_RE = re.compile(
    r"(?:factuurnummer|factuur\s*nr\.?|invoice\s*(?:number|no)\.?)\s*[:#]?\s*([A-Za-z0-9\-\/]+)",
    re.IGNORECASE,
)
DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
DATE_DUTCH_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(DUTCH_MONTHS.keys()) + r")\s+(\d{4})\b", re.IGNORECASE
)
INVOICE_DATE_LABEL_RE = re.compile(r"factuurdatum\D{0,10}", re.IGNORECASE)


@dataclass
class InvoiceAttachment:
    email_message_id: str
    attachment_filename: str
    email_subject: str
    email_from: str
    received_at: datetime
    pdf_path: str
    extracted_text: str
    invoice_number: str = ""
    invoice_date: date | None = None
    supplier_name: str = ""
    amount_cents: int | None = None
    currency: str = "EUR"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _parse_amount_to_cents(text: str) -> int | None:
    match = AMOUNT_LABEL_RE.search(text)
    candidates = []
    if match:
        candidates.append(match.group(1))
    all_amounts = ANY_AMOUNT_RE.findall(text)
    candidates.extend(all_amounts)

    best_cents = None
    for raw in candidates:
        normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
        try:
            value = float(normalized)
        except ValueError:
            continue
        cents = round(value * 100)
        # Prefer the label-matched amount; otherwise take the largest amount
        # found (invoice totals are usually the largest number on the page).
        if best_cents is None or cents > best_cents:
            best_cents = cents
    return best_cents


def _parse_invoice_date(text: str) -> date | None:
    label_match = INVOICE_DATE_LABEL_RE.search(text)
    search_window = text[label_match.end():label_match.end() + 30] if label_match else text

    numeric = DATE_NUMERIC_RE.search(search_window)
    if numeric:
        day, month, year = (int(g) for g in numeric.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    dutch = DATE_DUTCH_RE.search(search_window)
    if dutch:
        day = int(dutch.group(1))
        month = DUTCH_MONTHS[dutch.group(2).lower()]
        year = int(dutch.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


def _extract_fields(text: str, email_from: str) -> dict:
    invoice_number_match = INVOICE_NUMBER_RE.search(text)
    return {
        "invoice_number": invoice_number_match.group(1) if invoice_number_match else "",
        "invoice_date": _parse_invoice_date(text),
        "amount_cents": _parse_amount_to_cents(text),
        # Best-effort: the sender's display name/domain is usually the supplier.
        "supplier_name": email_from,
    }


def fetch_invoice_attachments(since: date) -> list[InvoiceAttachment]:
    if not settings.imap_host or not settings.imap_username:
        raise RuntimeError(
            "IMAP is niet geconfigureerd. Vul IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD "
            "in via .env (gebruik een app-wachtwoord, niet je gewone mailwachtwoord)."
        )

    os.makedirs(INVOICE_DIR, exist_ok=True)

    imap_cls = imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4
    conn = imap_cls(settings.imap_host, settings.imap_port)
    results: list[InvoiceAttachment] = []
    try:
        conn.login(settings.imap_username, settings.imap_password)
        conn.select(settings.imap_folder)

        since_str = since.strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since_str}")')
        if status != "OK":
            raise RuntimeError(f"IMAP-zoekopdracht mislukt: {status}")

        message_ids = data[0].split()
        for msg_id in message_ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue

            raw_email = msg_data[0][1]
            message = email.message_from_bytes(raw_email)
            message_id = message.get("Message-ID", f"<no-id-{msg_id.decode()}>")
            subject = _decode(message.get("Subject"))
            from_addr = _decode(message.get("From"))
            try:
                received_at = parsedate_to_datetime(message.get("Date"))
            except (TypeError, ValueError):
                received_at = datetime.utcnow()

            for part in message.walk():
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode(filename)
                if not filename.lower().endswith(settings.invoice_attachment_extension):
                    continue

                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", filename)
                pdf_path = os.path.join(
                    INVOICE_DIR, f"{msg_id.decode()}_{safe_name}"
                )
                with open(pdf_path, "wb") as fh:
                    fh.write(payload)

                extracted_text = ""
                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        extracted_text = "\n".join(
                            page.extract_text() or "" for page in pdf.pages
                        )
                except Exception:
                    # Corrupt/unreadable PDF -- keep the file, leave fields empty.
                    extracted_text = ""

                fields = _extract_fields(extracted_text, from_addr)
                results.append(
                    InvoiceAttachment(
                        email_message_id=message_id,
                        attachment_filename=filename,
                        email_subject=subject,
                        email_from=from_addr,
                        received_at=received_at,
                        pdf_path=pdf_path,
                        extracted_text=extracted_text,
                        **fields,
                    )
                )
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()

    return results
