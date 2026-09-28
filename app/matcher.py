"""Matches bank transactions to invoices.

Matching methods, tried in order for each transaction:

1. REFERENCE (high confidence, auto-confirmed): the invoice number is found
   verbatim in the transaction's description/reference text.

2. AMOUNT_DATE (medium confidence, needs confirmation): the amount matches
   within tolerance and the invoice date falls within the configured
   window, scored against how well the counterparty name matches the
   supplier name when more than one candidate ties on amount+date.

3. Combination match (medium confidence, needs confirmation): when no
   single document matches, a SET of open documents from the same
   supplier within the date window whose amounts sum exactly to the
   transaction -- e.g. one bank payment settling several invoices at once.

A transaction only ever matches documents moving money the same direction
it does (a deposit never matches a bill you owe, and vice versa). A
SPECIFICATION document (e.g. Accell's "Specificatie automatische incasso")
that matches also pulls in every other open invoice it mentions by number,
all sharing one Match.group_id so the dashboard can show -- and
confirm/reject -- them as one payment covering several documents.

Anything left over stays UNMATCHED -- these are exactly the "vraagposten"
you want to resolve before your accountant asks about them.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Direction, DocumentKind, Invoice, Match, MatchMethod, MatchStatus, Transaction

MAX_COMBINATION_CANDIDATES = 12
MAX_COMBINATION_SIZE = 5


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


def _transaction_direction(transaction: Transaction) -> str:
    return Direction.INCOMING.value if transaction.amount_cents > 0 else Direction.OUTGOING.value


def _same_direction_invoices(transaction: Transaction, invoices: list[Invoice]) -> list[Invoice]:
    direction = _transaction_direction(transaction)
    return [inv for inv in invoices if (inv.direction or Direction.OUTGOING.value) == direction]


def _name_similarity(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    if set(a.split()) & set(b.split()):
        return 0.5
    return 0.0


def _discount_tolerance_cents(amount_cents: int) -> int:
    """How much less than invoiced a REFERENCE match may still accept as
    betalingskorting (early-payment discount) -- capped at whichever is
    SMALLER of a percentage of the amount or a flat ceiling, so a big
    invoice can't wave away an implausibly large gap."""
    percent_based = round(abs(amount_cents) * settings.payment_discount_percent / 100)
    return min(percent_based, settings.payment_discount_max_cents)


def _reference_match_group(transaction: Transaction, invoices: list[Invoice]) -> tuple[list[Invoice], int] | None:
    """Every open invoice whose number is found verbatim in the transaction's
    description/reference -- a single incasso payment often settles several
    invoices at once (e.g. Kruitbosch/Accell direct debits listing multiple
    comma-separated invoice numbers). Accepts the combined total being
    slightly less than the sum invoiced (see _discount_tolerance_cents);
    returns the matched invoices plus the (positive = discount) difference."""
    haystack = f"{transaction.description} {transaction.reference}".lower()
    matches = [
        invoice for invoice in invoices
        if invoice.amount_cents is not None
        and invoice.invoice_number
        and invoice.invoice_number.lower() in haystack
    ]
    if not matches:
        return None
    total = sum(invoice.amount_cents for invoice in matches)
    diff = total - abs(transaction.amount_cents)  # positive = paid less than invoiced (discount)
    tolerance = max(settings.match_amount_tolerance_cents, _discount_tolerance_cents(total))
    if abs(diff) > tolerance:
        return None
    return matches, diff


def _date_diff(transaction: Transaction, invoice: Invoice) -> int:
    if invoice.invoice_date is None:
        return 10 ** 6  # push invoices without a date to the back
    return abs((transaction.booking_date - invoice.invoice_date).days)


def _amount_date_match(transaction: Transaction, invoices: list[Invoice]) -> Invoice | None:
    best: Invoice | None = None
    best_score: float | None = None
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
        # A good counterparty-name match can outweigh a slightly worse date
        # difference when several candidates tie on amount alone.
        score = diff - (_name_similarity(transaction.counterparty_name, invoice.supplier_name) * 5)
        if best_score is None or score < best_score:
            best, best_score = invoice, score
    return best


def _combination_match(transaction: Transaction, invoices: list[Invoice]) -> list[Invoice] | None:
    """No single document matches -- look for a SET of open documents from
    the same supplier, within the date window, whose amounts sum exactly to
    the transaction. Bounded in both candidate pool size and combination
    size so it can't blow up on a supplier with many open invoices."""
    target = abs(transaction.amount_cents)
    pool = [
        inv for inv in invoices
        if inv.amount_cents is not None
        and _name_similarity(transaction.counterparty_name, inv.supplier_name) > 0
        and _date_diff(transaction, inv) <= settings.match_date_window_days
    ]
    if len(pool) < 2:
        return None
    pool.sort(key=lambda inv: _date_diff(transaction, inv))
    pool = pool[:MAX_COMBINATION_CANDIDATES]

    for size in range(2, min(MAX_COMBINATION_SIZE, len(pool)) + 1):
        for combo in itertools.combinations(pool, size):
            if sum(inv.amount_cents for inv in combo) == target:
                return list(combo)
    return None


def _linked_specification_invoices(spec_invoice: Invoice, all_candidates: list[Invoice]) -> list[Invoice]:
    """A SPECIFICATION document (e.g. Accell's incasso specification) lists
    invoice numbers settled by the same payment -- pull in any of those
    that exist as their own open Invoice record too, so confirming the
    specification also resolves them."""
    if spec_invoice.document_kind != DocumentKind.SPECIFICATION.value:
        return []
    referenced = {n.lower() for n in (spec_invoice.referenced_invoice_numbers or [])}
    if not referenced:
        return []
    return [
        inv for inv in all_candidates
        if inv.id != spec_invoice.id and inv.invoice_number and inv.invoice_number.lower() in referenced
    ]


def _clear_unconfirmed_suggestions(session: Session, transaction: Transaction) -> None:
    for match in list(transaction.matches):
        if not match.confirmed:
            match.invoice.status = MatchStatus.UNMATCHED
            session.delete(match)


def _create_match_group(
    session: Session,
    transaction: Transaction,
    invoices: list[Invoice],
    method: MatchMethod,
    confidence: str,
    confirmed: bool,
    discount_cents: int = 0,
) -> None:
    group_id = uuid.uuid4().hex[:12]
    for invoice in invoices:
        session.add(
            Match(
                transaction_id=transaction.id,
                invoice_id=invoice.id,
                method=method,
                confidence=confidence,
                confirmed=confirmed,
                group_id=group_id,
                discount_cents=discount_cents,
            )
        )
        invoice.status = MatchStatus.MATCHED if confirmed else MatchStatus.SUGGESTED
    transaction.status = MatchStatus.MATCHED if confirmed else MatchStatus.SUGGESTED


def run_matching(session: Session) -> MatchingSummary:
    summary = MatchingSummary()

    # Re-evaluate every unresolved transaction against every unresolved
    # invoice each run, so a newly-arrived invoice can clear an old
    # unmatched transaction (and vice versa).
    transactions = _candidate_transactions(session)

    for transaction in transactions:
        all_candidates = _candidate_invoices(session)  # re-fetch: earlier iterations may have consumed one
        invoices = _same_direction_invoices(transaction, all_candidates)

        reference_group = _reference_match_group(transaction, invoices)
        if reference_group is not None:
            matched_invoices, discount_cents = reference_group
            _clear_unconfirmed_suggestions(session, transaction)
            linked: list[Invoice] = []
            for invoice in matched_invoices:
                for candidate in _linked_specification_invoices(invoice, all_candidates):
                    if candidate not in matched_invoices and candidate not in linked:
                        linked.append(candidate)
            _create_match_group(
                session, transaction, [*matched_invoices, *linked],
                MatchMethod.REFERENCE, "high", confirmed=True, discount_cents=discount_cents,
            )
            summary.auto_matched += 1
            continue

        amount_date_hit = _amount_date_match(transaction, invoices)
        if amount_date_hit is not None:
            _clear_unconfirmed_suggestions(session, transaction)
            linked = _linked_specification_invoices(amount_date_hit, all_candidates)
            _create_match_group(session, transaction, [amount_date_hit, *linked], MatchMethod.AMOUNT_DATE, "medium", confirmed=False)
            summary.suggested += 1
            continue

        combo_hit = _combination_match(transaction, invoices)
        if combo_hit is not None:
            _clear_unconfirmed_suggestions(session, transaction)
            _create_match_group(session, transaction, combo_hit, MatchMethod.AMOUNT_DATE, "medium", confirmed=False)
            summary.suggested += 1
            continue

        # No candidate at all: a previously SUGGESTED transaction whose
        # invoice(s) disappeared (rejected, or consumed by another match)
        # goes back to UNMATCHED. Anything else that reaches here already
        # has its final status (RULE_HANDLED, MATCHED, ...) and must be left
        # alone -- this must never be a bare "!= UNMATCHED" check, since a
        # status change made earlier in the same session (e.g. by
        # apply_rules) but not yet flushed could otherwise get clobbered
        # back to UNMATCHED here.
        if transaction.status == MatchStatus.SUGGESTED:
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
