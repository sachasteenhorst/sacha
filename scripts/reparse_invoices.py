#!/usr/bin/env python3
"""Re-extracts invoice_number/invoice_date/amount_cents/supplier_name,
richting (direction), documentsoort (document_kind) and content_hash for
every invoice already in the database, straight from its stored PDF
(pdf_path). Run this after improving the parsing rules in
app/email_client.py so existing invoices benefit without needing a fresh
mail sync. Also cleans up invoices that turn out to be duplicates (the same
document fetched from two mailboxes, e.g. facturen@ and info@).

Never touches a document you (or the matcher) already made a real decision
about -- MATCHED, manually IGNORED, SUGGESTED or RECEIPT_ELSEWHERE stay
exactly as they are. A document still sitting as UNMATCHED and newly
recognised as "other" (general terms, a bank-account change notice, an
amount-less packing slip, ...) gets set to IGNORED, since that's the
classification this script exists to (re)apply -- not a status you set by
hand. Never touches in_basecone/basecone_forwarded_at either -- that's a
real-world fact (was this ever forwarded?) this offline, PDF-only script has
no way to check or un-know.
"""
import hashlib
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber

from app.db import SessionLocal
from app.email_client import _extract_fields, invoice_dedup_key
from app.models import DocumentKind, Invoice, MatchStatus


def _reparse_fields(session) -> tuple[int, int, int, int]:
    invoices = session.query(Invoice).all()
    total = len(invoices)
    print(f"{total} facturen gevonden, opnieuw verwerken...")

    updated = 0
    missing_pdf = 0
    unreadable = 0
    newly_other = 0

    for inv in invoices:
        if not inv.pdf_path or not os.path.isfile(inv.pdf_path):
            missing_pdf += 1
            continue

        with open(inv.pdf_path, "rb") as fh:
            content_bytes = fh.read()
        inv.content_hash = hashlib.sha256(content_bytes).hexdigest()

        try:
            with pdfplumber.open(inv.pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as exc:  # noqa: BLE001 -- corrupt/unreadable PDF, skip it
            print(f"  kon {inv.pdf_path} niet lezen: {exc}")
            unreadable += 1
            continue

        fields = _extract_fields(text, inv.email_from, "", inv.email_subject, inv.attachment_filename)
        inv.extracted_text = text
        inv.invoice_number = fields["invoice_number"]
        inv.invoice_date = fields["invoice_date"]
        inv.amount_cents = fields["amount_cents"]
        inv.supplier_name = fields["supplier_name"]
        inv.direction = fields["direction"]
        inv.document_kind = fields["document_kind"]
        inv.referenced_invoice_numbers = fields["referenced_invoice_numbers"]

        if fields["document_kind"] == DocumentKind.OTHER.value and inv.status == MatchStatus.UNMATCHED:
            inv.status = MatchStatus.IGNORED
            newly_other += 1

        updated += 1

    session.commit()
    return total, updated, missing_pdf, unreadable, newly_other


def _dedupe_invoices(session) -> int:
    """Groups every invoice by document identity (content_hash, falling back
    to leverancier+factuurnummer+bedrag) and removes every duplicate beyond
    the one worth keeping: a MATCHED one first, else the oldest."""
    groups: dict[str, list[Invoice]] = defaultdict(list)
    for inv in session.query(Invoice).all():
        key = invoice_dedup_key(inv.content_hash, inv.supplier_name, inv.invoice_number, inv.amount_cents)
        if key is not None:
            groups[key].append(inv)

    removed = 0
    for dupes in groups.values():
        if len(dupes) < 2:
            continue
        dupes.sort(key=lambda i: (i.status != MatchStatus.MATCHED, i.received_at, i.id))
        keeper, *rest = dupes
        for dupe in rest:
            if dupe.status == MatchStatus.MATCHED:
                # Two genuinely matched copies of the same document -- both
                # made a real decision, too risky to silently pick one; skip.
                continue
            if dupe.pdf_path and dupe.pdf_path != keeper.pdf_path and os.path.isfile(dupe.pdf_path):
                os.remove(dupe.pdf_path)
            session.delete(dupe)
            removed += 1
    if removed:
        session.commit()
    return removed


def main() -> None:
    session = SessionLocal()

    total, updated, missing_pdf, unreadable, newly_other = _reparse_fields(session)
    removed = _dedupe_invoices(session)

    open_invoices = session.query(Invoice).filter(Invoice.status == MatchStatus.UNMATCHED)
    zonder_bedrag = open_invoices.filter(Invoice.amount_cents.is_(None)).count()
    zonder_nummer = open_invoices.filter(
        (Invoice.invoice_number.is_(None)) | (Invoice.invoice_number == "")
    ).count()
    nog_open = open_invoices.count()

    print(f"\n{updated} facturen bijgewerkt.")
    if missing_pdf:
        print(f"{missing_pdf} zonder vindbaar PDF-bestand (overgeslagen).")
    if unreadable:
        print(f"{unreadable} PDF's niet leesbaar (overgeslagen).")
    if newly_other:
        print(f"{newly_other} herkend als geen factuur (algemene voorwaarden e.d.) en op genegeerd gezet.")
    if removed:
        print(f"{removed} dubbele facturen verwijderd (zelfde document uit meerdere mailboxen).")
    print(f"\nNog openstaand (niet genegeerd/gekoppeld): {nog_open} van {total - removed}")
    print(f"Daarvan zonder bedrag:        {zonder_bedrag}")
    print(f"Daarvan zonder factuurnummer: {zonder_nummer}")


if __name__ == "__main__":
    main()
