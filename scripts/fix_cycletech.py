#!/usr/bin/env python3
"""One-off (and re-runnable) fix for two related production bugs:

1. CycleSoftware B.V. (the software vendor itself, sending real
   inkoopfacturen via noreply@twsc.nl) was wrongly excluded from Basecone
   forwarding because "CycleSoftware" used to be on
   BASECONE_EXCLUDE_SUPPLIERS -- that setting exists for THIS SHOP'S OWN
   sales-export rows (document_kind SALES_INVOICE, never forwardable
   regardless of supplier name anyway, see app/basecone_forward.py), so it
   never needed to include the real vendor's name and only ended up
   blocking it by accident. Existing CycleSoftware B.V. invoices already
   have the right document_kind/supplier_name once app/email_client.py's
   KNOWN_SUPPLIER_DOMAINS fix lands -- this script just marks them
   excluded_from_auto_forward, since a config fix that suddenly makes a
   historical document forwardable must never turn into "silently
   auto-send it the moment this ships" (same reasoning as
   scripts/fix_tenways.py) -- Sacha decides when to send the backlog.

2. Cycletech invoices (via administratie@cycletech.nl) were ending up
   DocumentKind.OTHER ("Geen factuur"), and their invoice_number sometimes
   picked up the klantnummer instead of the real factuurnummer -- both
   numbers appear in the filename "Factuur_<klantnummer>-<factuurnummer>.pdf"
   (see app.email_client._classify_cycletech_document/
   _extract_cycletech_invoice_number). The same invoice sometimes arrives
   twice (once standalone, once bundled in a "CYCLETECH | Incasso
   overzicht" mail, same PDF) -- normalising the invoice_number this way
   also makes app.email_client.invoice_dedup_key recognise them as the
   same document, so it's never forwarded to Basecone twice.

DRY-RUN by default: prints what would change, changes nothing. Pass
--apply to actually update the database. Never sends anything to Basecone
or touches in_basecone -- forwarding the backlog is a decision Sacha makes
by hand, from the dashboard, whenever he's ready. New invoices from either
supplier are not excluded going forward -- only documents a run of this
script actually reclassifies or normalises.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_, select

from app.db import SessionLocal
from app.email_client import _classify_cycletech_document, _extract_cycletech_invoice_number
from app.models import BaseconeForwardStatus, DocumentKind, Invoice

CYCLESOFTWARE_DOMAIN = "twsc.nl"
CYCLESOFTWARE_SUPPLIER_NAME = "CycleSoftware B.V."
CYCLETECH_DOMAIN = "cycletech.nl"
CYCLETECH_SUPPLIER_NAME = "Cycletech"


def _find_cyclesoftware_candidates(session) -> list[Invoice]:
    return list(
        session.scalars(
            select(Invoice)
            .where(
                or_(
                    Invoice.email_from.ilike(f"%{CYCLESOFTWARE_DOMAIN}%"),
                    Invoice.supplier_name.ilike("%cyclesoftware%"),
                ),
                Invoice.document_kind != DocumentKind.SALES_INVOICE.value,
            )
            .order_by(Invoice.received_at)
        )
    )


def _find_cycletech_candidates(session) -> list[Invoice]:
    return list(
        session.scalars(
            select(Invoice)
            .where(
                or_(
                    Invoice.email_from.ilike(f"%{CYCLETECH_DOMAIN}%"),
                    Invoice.supplier_name.ilike("%cycletech%"),
                )
            )
            .order_by(Invoice.received_at)
        )
    )


def _fix_cyclesoftware(session, apply: bool) -> int:
    candidates = _find_cyclesoftware_candidates(session)
    print(f"{len(candidates)} CycleSoftware B.V.-document(en) gevonden (echte inkoopfacturen van de softwareleverancier).\n")

    changed = 0
    for invoice in candidates:
        supplier_changed = invoice.supplier_name != CYCLESOFTWARE_SUPPLIER_NAME
        needs_exclusion = (
            not invoice.excluded_from_auto_forward
            and invoice.in_basecone != BaseconeForwardStatus.YES.value
        )
        if not (supplier_changed or needs_exclusion):
            continue

        label = invoice.invoice_number or invoice.attachment_filename or f"factuur {invoice.id}"
        amount = f"{(invoice.amount_cents or 0) / 100:.2f}"
        bits = []
        if supplier_changed:
            bits.append(f"'{invoice.supplier_name}' -> '{CYCLESOFTWARE_SUPPLIER_NAME}'")
        if needs_exclusion:
            bits.append("uitgesloten van automatisch doorsturen")
        print(f"  {invoice.received_at.date()}  {label:<20.20}  EUR {amount:>10}  " + ", ".join(bits))
        changed += 1

        if apply:
            invoice.supplier_name = CYCLESOFTWARE_SUPPLIER_NAME
            if needs_exclusion:
                invoice.excluded_from_auto_forward = True

    print(f"\n{'(DRY-RUN) ' if not apply else ''}{changed} CycleSoftware-document(en) {'zouden worden' if not apply else ''} bijgewerkt.\n")
    return changed


def _fix_cycletech(session, apply: bool) -> int:
    candidates = _find_cycletech_candidates(session)
    print(f"{len(candidates)} Cycletech-document(en) gevonden.\n")

    counts = {"invoice": 0, "other": 0}
    total_amount_cents = 0
    changed = 0

    for invoice in candidates:
        new_kind = _classify_cycletech_document(invoice.attachment_filename) or invoice.document_kind
        new_number = _extract_cycletech_invoice_number(invoice.attachment_filename) or invoice.invoice_number
        counts["invoice" if new_kind == DocumentKind.INVOICE.value else "other"] += 1
        if new_kind == DocumentKind.INVOICE.value:
            total_amount_cents += invoice.amount_cents or 0

        supplier_changed = invoice.supplier_name != CYCLETECH_SUPPLIER_NAME
        kind_changed = invoice.document_kind != new_kind
        number_changed = invoice.invoice_number != new_number
        newly_forwardable = (
            kind_changed
            and invoice.document_kind == DocumentKind.OTHER.value
            and new_kind != DocumentKind.OTHER.value
        )

        if not (supplier_changed or kind_changed or number_changed):
            continue

        label = new_number or invoice.attachment_filename or f"factuur {invoice.id}"
        amount = f"{(invoice.amount_cents or 0) / 100:.2f}"
        bits = []
        if supplier_changed:
            bits.append(f"'{invoice.supplier_name}' -> '{CYCLETECH_SUPPLIER_NAME}'")
        if kind_changed:
            bits.append(f"{invoice.document_kind} -> {new_kind}")
        if number_changed:
            bits.append(f"factuurnr '{invoice.invoice_number}' -> '{new_number}'")
        print(f"  {invoice.received_at.date()}  {label:<20.20}  EUR {amount:>10}  " + ", ".join(bits))
        changed += 1

        if apply:
            invoice.supplier_name = CYCLETECH_SUPPLIER_NAME
            invoice.document_kind = new_kind
            invoice.invoice_number = new_number
            if newly_forwardable:
                invoice.excluded_from_auto_forward = True

    print(f"\nVerdeling: {counts['invoice']} factu(u)r(en), {counts['other']} overig.")
    print(f"Totaalbedrag Cycletech-facturen: EUR {total_amount_cents / 100:.2f}")
    print(f"\n{'(DRY-RUN) ' if not apply else ''}{changed} Cycletech-document(en) {'zouden worden' if not apply else ''} bijgewerkt.")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Voer de herclassificatie echt uit (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        cs_changed = _fix_cyclesoftware(session, args.apply)
        print("\n" + "-" * 60 + "\n")
        ct_changed = _fix_cycletech(session, args.apply)

        if args.apply:
            session.commit()
            print(f"\n{cs_changed + ct_changed} document(en) in totaal bijgewerkt. Oude, nu-forwardable facturen zijn")
            print("uitgesloten van automatisch doorsturen -- stuur ze zelf naar Basecone via de knop")
            print("in het dashboard wanneer je zover bent. Nieuwe facturen van beide leveranciers")
            print("gaan vanaf nu gewoon automatisch mee (als AUTO_FORWARD_BASECONE aanstaat).")
        else:
            print("\nDraai met --apply om dit echt uit te voeren.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
