#!/usr/bin/env python3
"""One-off (and re-runnable) fix for Tenways documents that were
misclassified before app.email_client learned Tenways' own filename/subject
conventions (see _classify_tenways_document and _derive_supplier_name's
known-domain-wins-over-display-name fix):

1. Normalises supplier_name to "Tenways" for every document from
   notifications@tenways.com, however it was stored before (a real
   production case: a later batch of Tenways' notifications switched their
   display name to "Derk ten Kleij", a person at Tenways, same address --
   which became the stored supplier_name instead of "Tenways").
2. Reclassifies document_kind: an invoice or credit note (INV_*/RINV_*
   filename, or "Invoice"/"Credit" in the subject) becomes DocumentKind
   INVOICE so it shows up as a real vraagpost and can go to Basecone; a
   quote/offerte (S0*.pdf filename, or "Quotation"/"Offerte" in the
   subject) becomes DocumentKind OTHER ("Geen factuur") -- never something
   to pay or forward.
3. Marks every document newly reclassified from OTHER to INVOICE/
   SPECIFICATION as excluded_from_auto_forward, so the next sync's
   AUTO_FORWARD_BASECONE step never silently mass-sends months of old
   invoices to the accountant the moment this fix ships -- forwarding the
   backlog is a decision Sacha makes by hand, from the dashboard, whenever
   he's ready.

DRY-RUN by default: prints what would change and the invoice/credit/quote
breakdown, changes nothing. Pass --apply to actually update the database.
Never sends anything to Basecone or touches in_basecone -- that's a
separate, manual step.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_, select

from app.db import SessionLocal
from app.email_client import _classify_tenways_document
from app.models import DocumentKind, Invoice

TENWAYS_DOMAIN = "tenways.com"
KNOWN_SUPPLIER_NAME = "Tenways"


def _find_candidates(session) -> list[Invoice]:
    return list(
        session.scalars(
            select(Invoice).where(
                or_(
                    Invoice.email_from.ilike(f"%{TENWAYS_DOMAIN}%"),
                    Invoice.supplier_name.ilike("%tenways%"),
                    Invoice.supplier_name.ilike("%derk ten kleij%"),
                )
            ).order_by(Invoice.received_at)
        )
    )


def _bucket_for(invoice: Invoice, new_kind: str) -> str:
    """Invoice/credit/quote, for the summary only -- document_kind itself
    only distinguishes forwardable (INVOICE) from not (OTHER)."""
    if new_kind == DocumentKind.OTHER.value:
        return "quote"
    filename = (invoice.attachment_filename or "").upper()
    subject = (invoice.email_subject or "").lower()
    if filename.startswith("RINV") or "credit" in subject:
        return "credit"
    return "invoice"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Voer de herclassificatie echt uit (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        candidates = _find_candidates(session)
        print(f"{len(candidates)} Tenways-document(en) gevonden (op afzenderadres of leveranciernaam).\n")

        counts = {"invoice": 0, "credit": 0, "quote": 0}
        changed_rows: list[tuple[Invoice, str]] = []
        total_amount_cents = 0

        for invoice in candidates:
            new_kind = _classify_tenways_document(
                invoice.attachment_filename, invoice.email_subject, invoice.extracted_text
            ) or invoice.document_kind
            bucket = _bucket_for(invoice, new_kind)
            counts[bucket] += 1

            supplier_changed = invoice.supplier_name != KNOWN_SUPPLIER_NAME
            kind_changed = invoice.document_kind != new_kind
            newly_forwardable = kind_changed and invoice.document_kind == DocumentKind.OTHER.value and new_kind != DocumentKind.OTHER.value

            if bucket in ("invoice", "credit"):
                total_amount_cents += invoice.amount_cents or 0

            if not (supplier_changed or kind_changed):
                continue

            label = invoice.invoice_number or invoice.attachment_filename or f"factuur {invoice.id}"
            amount = f"{(invoice.amount_cents or 0) / 100:.2f}"
            change_bits = []
            if supplier_changed:
                change_bits.append(f"'{invoice.supplier_name}' -> '{KNOWN_SUPPLIER_NAME}'")
            if kind_changed:
                change_bits.append(f"{invoice.document_kind} -> {new_kind}")
            print(
                f"  {invoice.received_at.date()}  [{bucket:<7}]  {label:<28.28}  EUR {amount:>10}  "
                + ", ".join(change_bits)
            )
            changed_rows.append((invoice, new_kind))

            if args.apply:
                invoice.supplier_name = KNOWN_SUPPLIER_NAME
                invoice.document_kind = new_kind
                if newly_forwardable:
                    invoice.excluded_from_auto_forward = True

        print(
            f"\nVerdeling (alle {len(candidates)} gevonden Tenways-documenten): "
            f"{counts['invoice']} factu(u)r(en), {counts['credit']} creditnota('s), {counts['quote']} offerte(s)/quote(s)."
        )
        print(f"Totaalbedrag facturen + creditnota's: EUR {total_amount_cents / 100:.2f}")

        if args.apply:
            session.commit()
            print(f"\n{len(changed_rows)} document(en) bijgewerkt. Deze zijn uitgesloten van automatisch doorsturen --")
            print("stuur ze zelf naar Basecone via de knop in het dashboard wanneer je zover bent.")
        else:
            print(f"\n(DRY-RUN) {len(changed_rows)} document(en) zouden worden bijgewerkt.")
            print("Draai met --apply om dit echt uit te voeren.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
