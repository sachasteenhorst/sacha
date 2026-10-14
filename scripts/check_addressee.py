#!/usr/bin/env python3
"""One-off (and re-runnable) pass that checks every existing inkoopfactuur/
specificatie for whether it's actually addressed to this shop at all (see
app.email_client._check_addressee/app.payments.mentions_own_company) --
needed once after this feature shipped (addressee_status defaults to "ok"
for every row that existed before this migration, so nothing is flagged
until this backfill runs), and safe to re-run any time afterwards: a
document Sacha already reviewed ("Toch voor ons"/"Klopt, negeren") is never
touched again.

Prints a count of "aan ons gericht" / "niet aan ons gericht" / "onbekend"
(no extracted text to check) and, for the "niet"-cases, a list (leverancier,
factuurnummer, bedrag, en de geadresseerde als die herkenbaar is -- puur
informatief, nooit gebruikt om te beslissen).

DRY-RUN by default: prints what it would find/change, changes nothing.
Pass --apply to actually update Invoice.addressee_status. Never touches
in_basecone, forwarding, or payment_method -- the existing
not_applicable_reason/is_tracked_purchase_invoice checks already respond
to addressee_status automatically once it's set.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.db import SessionLocal
from app.email_client import _check_addressee
from app.models import DocumentKind, Invoice

_ADDRESSEE_CANDIDATE_RE = re.compile(
    r"(?:factuur\s*(?:voor|aan)|t\.?a\.?v\.?|attn\.?|ter\s*attentie\s*van)\s*[:\-]?\s*([A-Za-zÀ-ÿ&.\- ]{3,60})",
    re.IGNORECASE,
)


def _guess_addressee(text: str) -> str:
    """Best-effort, display-only guess at who a document WAS addressed to
    -- never used to decide addressee_status itself, just so the printed
    "niet"-list gives Sacha a head start on checking it by hand."""
    m = _ADDRESSEE_CANDIDATE_RE.search(text or "")
    if not m:
        return "-"
    return m.group(1).strip().splitlines()[0][:60]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Werk Invoice.addressee_status echt bij (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        candidates = list(
            session.scalars(
                select(Invoice).where(
                    Invoice.document_kind.in_((DocumentKind.INVOICE.value, DocumentKind.SPECIFICATION.value)),
                    Invoice.direction == "outgoing",
                )
                .order_by(Invoice.received_at)
            )
        )
        print(f"{len(candidates)} inkoopfactu(u)r(en)/specificatie(s) gevonden om te controleren.\n")

        counts = {"ok": 0, "not_for_us": 0, "unknown": 0}
        not_for_us_rows = []
        changed = 0

        for invoice in candidates:
            new_status = _check_addressee(invoice.extracted_text)
            counts[new_status] += 1
            if new_status == "not_for_us":
                not_for_us_rows.append((invoice, _guess_addressee(invoice.extracted_text)))

            if invoice.addressee_reviewed or invoice.addressee_status == new_status:
                continue
            changed += 1
            if args.apply:
                invoice.addressee_status = new_status

        print(f"Aan ons gericht (ok):     {counts['ok']}")
        print(f"Niet aan ons gericht:     {counts['not_for_us']}")
        print(f"Geadresseerde onbekend:   {counts['unknown']}")

        if not_for_us_rows:
            print("\nNiet aan ons gericht -- leverancier, factuurnr, bedrag, gevonden geadresseerde (indien herkenbaar):")
            for invoice, guessed in not_for_us_rows:
                label = invoice.supplier_name or "?"
                amount = f"{(invoice.amount_cents or 0) / 100:.2f}"
                print(f"  {invoice.received_at.date()}  {label:<28.28}  {invoice.invoice_number or '?':<16.16}  EUR {amount:>10}  -> {guessed}")

        if args.apply:
            session.commit()
            print(f"\n{changed} factu(u)r(en) bijgewerkt.")
        else:
            print(f"\n(DRY-RUN) {changed} factu(u)r(en) zouden worden bijgewerkt.")
            print("Draai met --apply om dit echt uit te voeren.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
