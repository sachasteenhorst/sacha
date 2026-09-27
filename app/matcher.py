"""Matches bank transactions to invoices.

Two matching methods, in order of confidence:

1. REFERENCE (high confidence, auto-confirmed): the invoice number is found
   verbatim in the transaction's description/reference text. This is
   reliable enough to mark both sides as MATCHED without manual review.

2. AMOUNT_DATE (medium confidence, needs confirmation): the absolute amount
   matches within tolerance and the invoice date falls within the
   configured window before/after the booking date. Recorded as a
   SUGGESTED match for you to confirm or reject in the dashboard, since
   amount coincidences do happen (e.g. two invoices for the same round
   amount).

Anything left over stays UNMATCHED -- these are exactly the "vraagposten"
you want to resolve before your accountant asks about them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Match, MatchMethod, MatchStatus, Transaction


@dataclass
class MatchingSummary:
    auto_matched: int = 0
    suggested: int = 0
    still_unmatched_transactions: int = 0
    still_unmatched_invoices: int = 0


def _candidate_invoices(session: Session) -> list[Invoice]:
    stmt = select(Invoice).where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED]))
    return list(session.scalars(stmt))


def _candidate_transactions(session: Session) -> list[Transaction]:
    stmt = select(Transaction).where(Transaction.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED]))
    return list(session.scalars(stmt))


def _reference_match(transaction: Transaction, invoices: list[Invoice]) -> Invoice | None:
    haystack = f"{transaction.description} {transaction.reference}".lower()
    best: Invoice | None = None
    for invoice in invoices:
        if invoice.amount_cents is None:
            continue
        if abs(abs(transaction.amount_cents) - invoice.amount_cents) > settings.match_amount_tolerance_cents:
            continue
        if invoice.invoice_number and invoice.invoice_number.lower() in haystack:
            if best is None or _date_diff(transaction, invoice) < _date_diff(transaction, best):
                best = invoice
    return best


def _date_diff(transaction: Transaction, invoice: Invoice) -> int:
    if invoice.invoice_date is None:
        return 10 ** 6  # push invoices without a date to the back
    return abs((transaction.booking_date - invoice.invoice_date).days)


def _amount_date_match(transaction: Transaction, invoices: list[Invoice]) -> Invoice | None:
    best: Invoice | None = None
    best_diff = None
    for invoice in invoices:
        if invoice.amount_cents is None:
            continue
        if abs(abs(transaction.amount_cents) - invoice.amount_cents) > settings.match_amount_tolerance_cents:
            continue
        if invoice.invoice_date is not None:
            diff = abs((transaction.booking_date - invoice.invoice_date).days)
            if diff > settings.match_date_window_days:
                continue
        else:
            diff = settings.match_date_window_days  # no date on invoice: accept but as a weak candidate
        if best_diff is None or diff < best_diff:
            best, best_diff = invoice, diff
    return best


def _clear_unconfirmed_suggestions(session: Session, transaction: Transaction) -> None:
    for match in list(transaction.matches):
        if not match.confirmed:
            session.delete(match)


def run_matching(session: Session) -> MatchingSummary:
    summary = MatchingSummary()

    # Re-evaluate every unresolved transaction against every unresolved
    # invoice each run, so a newly-arrived invoice can clear an old
    # unmatched transaction (and vice versa).
    transactions = _candidate_transactions(session)

    for transaction in transactions:
        invoices = _candidate_invoices(session)  # re-fetch: earlier iterations may have consumed one

        reference_hit = _reference_match(transaction, invoices)
        if reference_hit is not None:
            _clear_unconfirmed_suggestions(session, transaction)
            match = Match(
                transaction_id=transaction.id,
                invoice_id=reference_hit.id,
                method=MatchMethod.REFERENCE,
                confidence="high",
                confirmed=True,
            )
            session.add(match)
            transaction.status = MatchStatus.MATCHED
            reference_hit.status = MatchStatus.MATCHED
            summary.auto_matched += 1
            continue

        amount_date_hit = _amount_date_match(transaction, invoices)
        if amount_date_hit is not None:
            _clear_unconfirmed_suggestions(session, transaction)
            match = Match(
                transaction_id=transaction.id,
                invoice_id=amount_date_hit.id,
                method=MatchMethod.AMOUNT_DATE,
                confidence="medium",
                confirmed=False,
            )
            session.add(match)
            transaction.status = MatchStatus.SUGGESTED
            amount_date_hit.status = MatchStatus.SUGGESTED
            summary.suggested += 1
            continue

        # No candidate at all: leave/mark as unmatched.
        if transaction.status != MatchStatus.UNMATCHED:
            _clear_unconfirmed_suggestions(session, transaction)
            transaction.status = MatchStatus.UNMATCHED

    session.flush()

    summary.still_unmatched_transactions = len(
        list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED)))
    )
    summary.still_unmatched_invoices = len(
        list(session.scalars(select(Invoice).where(Invoice.status == MatchStatus.UNMATCHED)))
    )
    return summary
