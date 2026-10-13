#!/usr/bin/env python3
"""One-off (and re-runnable) pass that labels every existing factuurmail of
the last 12 months with the right Outlook category -- see
app/mail_labels.py for the three categories and how the right one is
picked. Needed once after this feature shipped (there's a backlog of
already-ingested invoices that were never labelled), and safe to re-run any
time afterwards: app.mail_labels.set_invoice_category always skips an
invoice whose label is already correct, so a second run only touches what
actually changed since the first.

DRY-RUN by default: prints exactly what it would label and changes nothing
on Graph or in the database. Pass --apply to actually send the label
updates.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.email_client import GraphApiError, GraphAuthError
from app.mail_labels import desired_category, record_label_errors, set_invoice_category
from app.models import Invoice

MONTHS_BACK = 12


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Voer het labelen echt uit (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    since = datetime.utcnow() - timedelta(days=MONTHS_BACK * 30)

    session = SessionLocal()
    try:
        invoices = (
            session.query(Invoice)
            .filter(Invoice.received_at >= since, Invoice.graph_message_id != "")
            .order_by(Invoice.received_at)
            .all()
        )
        print(f"{len(invoices)} factuurmails van de laatste {MONTHS_BACK} maanden gevonden met een gekoppelde e-mail.\n")

        counts: dict[str, int] = {}
        would_change = 0
        errors: list[str] = []
        stopped_early = False

        for invoice in invoices:
            target = desired_category(invoice)
            counts[target] = counts.get(target, 0) + 1
            if invoice.graph_category == target:
                continue
            would_change += 1
            label = invoice.supplier_name or invoice.attachment_filename or f"factuur {invoice.id}"
            print(f"  {invoice.received_at.date()}  {label:<40.40}  {invoice.graph_category or '(geen)':<20} -> {target}")

            if args.apply:
                try:
                    set_invoice_category(invoice)
                except GraphAuthError as exc:
                    errors.append(str(exc))
                    stopped_early = True
                    print(f"\nGestopt: {exc}")
                    break
                except GraphApiError as exc:
                    errors.append(f"{label}: {exc}")
                    continue

        print("\nVerdeling (gewenste categorie, ongeacht of 'm al klopte):")
        for name, count in counts.items():
            print(f"  {name}: {count}")

        if args.apply:
            session.commit()
            record_label_errors(errors)
            if stopped_early:
                print(f"\n{would_change} zouden gelabeld worden; gestopt na een inlog-/permissiefout, zie hierboven.")
            else:
                print(f"\n{would_change - len(errors)} factu(u)r(en) gelabeld, {len(errors)} fout(en).")
                if errors:
                    print("Fouten:")
                    for err in errors:
                        print(f"  - {err}")
        else:
            print(f"\n(DRY-RUN) {would_change} factu(u)r(en) zouden een ander label krijgen.")
            print("Draai met --apply om dit echt uit te voeren.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
