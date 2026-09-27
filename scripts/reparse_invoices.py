#!/usr/bin/env python3
"""Re-extracts invoice_number/invoice_date/amount_cents/supplier_name for
every invoice already in the database, straight from its stored PDF
(pdf_path). Doesn't touch status or matches -- run this after improving
the parsing rules in app/email_client.py so existing invoices benefit
without needing a fresh mail sync.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber

from app.db import SessionLocal
from app.email_client import _extract_fields
from app.models import Invoice


def main() -> None:
    session = SessionLocal()
    invoices = session.query(Invoice).all()
    total = len(invoices)
    print(f"{total} facturen gevonden, opnieuw verwerken...")

    updated = 0
    missing_pdf = 0
    unreadable = 0

    for inv in invoices:
        if not inv.pdf_path or not os.path.isfile(inv.pdf_path):
            missing_pdf += 1
            continue

        try:
            with pdfplumber.open(inv.pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as exc:  # noqa: BLE001 -- corrupt/unreadable PDF, skip it
            print(f"  kon {inv.pdf_path} niet lezen: {exc}")
            unreadable += 1
            continue

        fields = _extract_fields(text, inv.email_from)
        inv.extracted_text = text
        inv.invoice_number = fields["invoice_number"]
        inv.invoice_date = fields["invoice_date"]
        inv.amount_cents = fields["amount_cents"]
        inv.supplier_name = fields["supplier_name"]
        updated += 1

    session.commit()

    zonder_bedrag = session.query(Invoice).filter(Invoice.amount_cents.is_(None)).count()
    zonder_nummer = session.query(Invoice).filter(
        (Invoice.invoice_number.is_(None)) | (Invoice.invoice_number == "")
    ).count()

    print(f"\n{updated} facturen bijgewerkt.")
    if missing_pdf:
        print(f"{missing_pdf} zonder vindbaar PDF-bestand (overgeslagen).")
    if unreadable:
        print(f"{unreadable} PDF's niet leesbaar (overgeslagen).")
    print(f"\nNog zonder bedrag:        {zonder_bedrag} van {total}")
    print(f"Nog zonder factuurnummer: {zonder_nummer} van {total}")


if __name__ == "__main__":
    main()
