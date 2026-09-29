#!/usr/bin/env python3
"""One-off remediation for a production incident: before app/bank_ponto.py's
dedupe was made robust (matching on IBAN/name + a date tolerance instead of
requiring an exact description match), every Ponto-fetched transaction that
was ALSO already present from a manually-uploaded CSV/MT940 export got
imported a second time -- 227 such pairs in production (July: 116, August:
137).

This script finds every already-imported Ponto transaction (source="ponto"
-- see app/db.py's _backfill_transaction_source for why that column, not
raw_data shape, is what reliably identifies one even for these older,
pre-fix rows) that has a matching CSV/MT940 "twin" per the same rule
app/bank_ponto.py itself now uses (see find_matching_transaction), and
merges each pair:

- the CSV/MT940 row is KEPT (it may already carry a real match/status from
  before Ponto's copy ever showed up -- the Ponto row never does, since a
  duplicate transaction is never worth matching by hand);
- the Ponto row's own Ponto id is copied onto the kept row's `external_id`,
  so a future Ponto sync recognises this booking and never re-imports it;
- the Ponto row itself, and any Match rows pointing at it, are deleted.

A Ponto row with NO matching twin (a transaction Ponto has that the CSV
export never covered, e.g. a period Sacha never uploaded) is left
completely untouched -- this script only ever removes a genuine duplicate.

DRY-RUN by default: prints exactly what it would do and changes nothing.
Pass --apply to actually perform the merge.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bank_ponto import find_matching_transaction
from app.db import SessionLocal
from app.models import Match, Transaction


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Voer de opschoning echt uit (standaard: alleen tonen, niets wijzigen).")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        ponto_rows = (
            session.query(Transaction)
            .filter(Transaction.source == "ponto")
            .order_by(Transaction.booking_date)
            .all()
        )
        print(f"{len(ponto_rows)} Ponto-transacties gevonden, op zoek naar CSV/MT940-tweelingen...\n")

        merged = 0
        untouched = 0
        for ponto_tx in ponto_rows:
            twin = find_matching_transaction(
                session,
                booking_date=ponto_tx.booking_date,
                amount_cents=ponto_tx.amount_cents,
                counterparty_iban=ponto_tx.counterparty_iban,
                counterparty_name=ponto_tx.counterparty_name,
                own_account_iban=ponto_tx.own_account_iban,
                exclude_id=ponto_tx.id,
            )
            if twin is None:
                untouched += 1
                continue

            bedrag = f"{ponto_tx.amount_cents / 100:.2f}".replace(".", ",")
            print(
                f"  {ponto_tx.booking_date}  EUR {bedrag:>10}  '{ponto_tx.counterparty_name}'  "
                f"-- Ponto #{ponto_tx.id} <-> bestaande #{twin.id} (status={twin.status.value})"
            )
            merged += 1

            if args.apply:
                twin.external_id = ponto_tx.external_id or ponto_tx.reference
                session.query(Match).filter(Match.transaction_id == ponto_tx.id).delete()
                session.delete(ponto_tx)

        if args.apply:
            session.commit()
            print(f"\n{merged} dubbele Ponto-transacties opgeruimd, {untouched} zonder tweeling ongemoeid gelaten.")
        else:
            print(f"\n(DRY-RUN) {merged} dubbele Ponto-transacties zouden opgeruimd worden, {untouched} blijven sowieso staan.")
            print("Draai met --apply om dit echt uit te voeren.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
