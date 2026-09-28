"""Builds the "Vraagposten" worklist: every bank transaction that would
still make your accountant ask a question, whatever the reason -- no
document at all, a matched document that never reached Basecone, or
anything else still open -- each with a concrete suggested next step, so
working through the list is mechanical instead of a fresh judgement call
every time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basecone_forward import is_forwardable
from app.matcher import _name_similarity, _transaction_direction
from app.models import Direction, Invoice, MatchStatus, Transaction

# A payment this small is more likely a personal/small cash-type expense
# (parking, coffee for a supplier visit, ...) than something worth chasing a
# supplier for -- the receipt usually just needs photographing into Basecone.
SMALL_EXPENSE_CENTS = 5000


@dataclass
class Vraagpost:
    key: str
    transaction: Transaction
    kind: str  # "controleren" | "regel" | "bon" | "opvragen" | "doorsturen"
    label: str
    age_days: int
    invoice: Invoice | None = None
    mailto: str | None = None


def _format_amount(cents: int) -> str:
    return f"€{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _recurring_transaction_ids(transactions: list[Transaction]) -> set[int]:
    """Which transactions have at least one OTHER transaction from what's
    plausibly the same supplier. Uses _name_similarity rather than exact (or
    even normalized-exact) string equality: Rabobank truncates the
    counterparty field differently per transaction, so the same supplier's
    name can show up as several distinct clipped variants (real example:
    "Tenways Technovation Europe B.", "... BV", "... Ke...") that never
    match each other by plain string equality, even after stripping a legal
    suffix off just one of them."""
    named = [t for t in transactions if (t.counterparty_name or "").strip()]
    recurring: set[int] = set()
    for i, t in enumerate(named):
        for other in named[i + 1:]:
            if _name_similarity(t.counterparty_name, other.counterparty_name) >= 0.9:
                recurring.add(t.id)
                recurring.add(other.id)
    return recurring


def _mailto_for_request(transaction: Transaction) -> str:
    amount = _format_amount(transaction.amount_cents)
    supplier = transaction.counterparty_name or "leverancier"
    subject = f"Verzoek kopie factuur - {supplier} - {amount}"
    body = (
        "Beste,\n\n"
        f"Zou u ons de factuur kunnen toesturen die hoort bij de betaling van {amount} "
        f"op {transaction.booking_date.strftime('%d-%m-%Y')}"
        + (f" (omschrijving: {transaction.description})" if transaction.description else "")
        + "?\n\nMet vriendelijke groet,\nVan der Linden Tweewielers"
    )
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"


def _suggest_for_unmatched(
    transaction: Transaction, same_direction_invoices: list[Invoice], recurring_transaction_ids: set[int]
) -> tuple[str, str, Invoice | None, str | None]:
    direction = _transaction_direction(transaction)
    candidates = [
        inv for inv in same_direction_invoices
        if (inv.direction or Direction.OUTGOING.value) == direction
        and _name_similarity(transaction.counterparty_name, inv.supplier_name) >= 0.5
    ]
    if candidates:
        return "controleren", "Mogelijke factuur (zelfde leverancier, bedrag wijkt af) -- controleren", candidates[0], None

    if transaction.id in recurring_transaction_ids:
        return "regel", "Terugkerende betaling zonder factuur -- regel maken", None, None

    if abs(transaction.amount_cents) <= SMALL_EXPENSE_CENTS:
        return "bon", "Privé/kleine uitgave -- bon in Basecone", None, None

    return "opvragen", "Geen factuur in mail -- opvragen bij leverancier", None, _mailto_for_request(transaction)


def build_vraagposten(session: Session) -> list[Vraagpost]:
    unmatched = list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED)))
    matched = list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.MATCHED)))
    open_invoices = list(
        session.scalars(select(Invoice).where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED])))
    )
    recurring = _recurring_transaction_ids(unmatched)
    today = date.today()

    items: list[Vraagpost] = []

    for t in unmatched:
        kind, label, invoice, mailto = _suggest_for_unmatched(t, open_invoices, recurring)
        items.append(Vraagpost(
            key=f"tx-{t.id}", transaction=t, kind=kind, label=label,
            age_days=(today - t.booking_date).days, invoice=invoice, mailto=mailto,
        ))

    for t in matched:
        pending_invoice = next((m.invoice for m in t.matches if is_forwardable(m.invoice)), None)
        if pending_invoice is None:
            continue  # every matched invoice already reached Basecone -- not a vraagpost
        items.append(Vraagpost(
            key=f"tx-{t.id}-forward", transaction=t, kind="doorsturen",
            label="Factuur gevonden maar nog niet in Basecone -- doorsturen",
            age_days=(today - t.booking_date).days, invoice=pending_invoice,
        ))

    items.sort(key=lambda v: (-v.age_days, -abs(v.transaction.amount_cents)))
    return items
