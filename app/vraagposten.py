"""Builds the "Vraagposten" worklist: every bank transaction that would
still make your accountant ask a question, whatever the reason -- no
document at all, a matched document that never reached Basecone, or
anything else still open -- each with a concrete suggested next step, so
working through the list is mechanical instead of a fresh judgement call
every time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.basecone_forward import is_forwardable
from app.matcher import MIN_REFERENCE_SUFFIX_LENGTH, _name_similarity, _transaction_direction
from app.models import Direction, Invoice, MatchStatus, Transaction
from app.payments import days_until_due, deduplicate_invoices, get_old_unreviewed_invoices, is_payable_invoice

# A payment this small is more likely a personal/small cash-type expense
# (parking, coffee for a supplier visit, ...) than something worth chasing a
# supplier for -- the receipt usually just needs photographing into Basecone.
SMALL_EXPENSE_CENTS = 5000

# Accell-style incasso's list the last 5 digits of every settled invoice
# between "Descr." and "Kenmerk machtiging" (real production example: "...
# Descr. 01123 03005 05120 0723 3 09087 Kenmerk machtiging / incassant ID:
# ..."), with a stray space occasionally splitting one group in two.
ACCELL_DESCR_MARKER_RE = re.compile(r"descr\.?\s*(.+?)\s*kenmerk\s*machtiging", re.IGNORECASE | re.DOTALL)


@dataclass
class Vraagpost:
    key: str
    kind: str  # "controleren" | "regel" | "bon" | "opvragen" | "doorsturen" |
    # "te_betalen_te_laat" | "betaald_niet_gekoppeld" | "oud_controleren"
    label: str
    age_days: int
    # None for "te_betalen_te_laat" -- that kind is about an invoice with no
    # matching bank transaction yet, not the other way around.
    transaction: Transaction | None = None
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


def _extract_reference_suffixes(description: str) -> list[str]:
    """Every 5-digit reference an Accell-style incasso description lists,
    reassembling a group a stray space split in two (e.g. "0723 3" for what
    should read "07233")."""
    m = ACCELL_DESCR_MARKER_RE.search(description or "")
    if not m:
        return []
    tokens = re.findall(r"\d+", m.group(1))
    groups: list[str] = []
    buffer = ""
    for token in tokens:
        buffer += token
        while len(buffer) >= MIN_REFERENCE_SUFFIX_LENGTH:
            groups.append(buffer[:MIN_REFERENCE_SUFFIX_LENGTH])
            buffer = buffer[MIN_REFERENCE_SUFFIX_LENGTH:]
    return groups


def _missing_incasso_references(transaction: Transaction, all_invoices: list[Invoice]) -> list[str]:
    """Which of the incasso's own referenced invoice numbers (see
    _extract_reference_suffixes) have NO corresponding Invoice from the same
    supplier anywhere in the database (any status -- a MATCHED one still
    proves the invoice was received). Checked against every invoice ever
    seen, not just open ones, so this only ever flags a genuinely never-
    received document."""
    suffixes = _extract_reference_suffixes(transaction.description)
    if not suffixes:
        return []
    same_supplier = [inv for inv in all_invoices if _name_similarity(transaction.counterparty_name, inv.supplier_name) > 0]
    known_suffixes = {
        inv.invoice_number[-MIN_REFERENCE_SUFFIX_LENGTH:]
        for inv in same_supplier
        if inv.invoice_number and len(inv.invoice_number) >= MIN_REFERENCE_SUFFIX_LENGTH
    }
    return [s for s in suffixes if s not in known_suffixes]


def _mailto_for_missing_references(transaction: Transaction, missing_refs: list[str]) -> str:
    amount = _format_amount(transaction.amount_cents)
    supplier = transaction.counterparty_name or "leverancier"
    refs = ", ".join(missing_refs)
    subject = f"Verzoek facturen eindigend op {refs} - {supplier}"
    body = (
        "Beste,\n\n"
        f"Bij de incasso van {amount} op {transaction.booking_date.strftime('%d-%m-%Y')} "
        f"missen wij de factu(u)r(en) die eindigen op: {refs}. Zou u die kunnen toesturen?\n\n"
        "Met vriendelijke groet,\nVan der Linden Tweewielers"
    )
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"


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
    transaction: Transaction,
    same_direction_invoices: list[Invoice],
    recurring_transaction_ids: set[int],
    all_invoices: list[Invoice],
) -> tuple[str, str, Invoice | None, str | None]:
    missing_refs = _missing_incasso_references(transaction, all_invoices)
    if missing_refs:
        label = f"Incasso mist {len(missing_refs)} factu(u)r(en) (eindigend op {', '.join(missing_refs)}) -- opvragen bij leverancier"
        return "opvragen", label, None, _mailto_for_missing_references(transaction, missing_refs)

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


def _overdue_payable_vraagposten(all_invoices: list[Invoice], today: date, latest_statement_date: date | None) -> list[Vraagpost]:
    """A "Nog te betalen" invoice (see app.payments.is_payable_invoice) that
    is actually past its due date -- distinct from merely "due soon", which
    isn't a vraagpost yet, just something to plan for. Has no transaction:
    that's exactly the point -- nobody has paid it yet.

    If the due date already falls BEFORE the latest bank statement we have
    on file, that's suspicious rather than simply "still owed": a bank
    statement covering that period exists and nothing matched, which is as
    likely to mean "paid another way and the matcher missed it" as "genuinely
    still unpaid" -- framed as "Betaald maar niet gekoppeld?" instead of a
    flat "te laat" so Sacha checks rather than assumes it's simply overdue.
    Deduplicated (see app.payments.deduplicate_invoices) so a supplier's
    resent reminder of the same invoice doesn't produce two identical rows.
    """
    payable = deduplicate_invoices([inv for inv in all_invoices if is_payable_invoice(inv)])
    items = []
    for inv in payable:
        days_overdue = -days_until_due(inv, today)
        if days_overdue <= 0:
            continue
        if latest_statement_date is not None and inv.due_date < latest_statement_date:
            items.append(Vraagpost(
                key=f"inv-{inv.id}-niet-gekoppeld",
                kind="betaald_niet_gekoppeld",
                label="Betaald maar niet gekoppeld? -- vervaldatum ligt al voor het laatste bankafschrift, controleer handmatig",
                age_days=days_overdue,
                invoice=inv,
            ))
        else:
            items.append(Vraagpost(
                key=f"inv-{inv.id}-te-laat",
                kind="te_betalen_te_laat",
                label=f"Te betalen, over termijn -- {days_overdue} dagen te laat",
                age_days=days_overdue,
                invoice=inv,
            ))
    return items


def _old_unreviewed_vraagposten(session: Session, today: date) -> list[Vraagpost]:
    """Invoices old enough to predate PAY_FROM_DATE that would otherwise
    belong on "Nog te betalen" -- too old to trust blindly after a database
    upgrade (see app.payments.get_old_unreviewed_invoices), so they surface
    here instead for a one-time manual check rather than silently vanishing
    or flooding "Nog te betalen" with years-old backlog."""
    items = []
    for inv in get_old_unreviewed_invoices(session):
        age_days = (today - inv.received_at.date()).days if inv.received_at else 0
        items.append(Vraagpost(
            key=f"inv-{inv.id}-oud",
            kind="oud_controleren",
            label="Oud, geen betaling gevonden -- controleren",
            age_days=age_days,
            invoice=inv,
        ))
    return items


def _vraagpost_amount_cents(item: Vraagpost) -> int:
    if item.transaction is not None:
        return abs(item.transaction.amount_cents)
    if item.invoice is not None and item.invoice.amount_cents is not None:
        return abs(item.invoice.amount_cents)
    return 0


def build_vraagposten(session: Session) -> list[Vraagpost]:
    unmatched = list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED)))
    matched = list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.MATCHED)))
    open_invoices = list(
        session.scalars(select(Invoice).where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED])))
    )
    all_invoices = list(session.scalars(select(Invoice)))
    recurring = _recurring_transaction_ids(unmatched)
    today = date.today()
    latest_statement_date = session.scalar(select(func.max(Transaction.booking_date)))

    items: list[Vraagpost] = []

    for t in unmatched:
        kind, label, invoice, mailto = _suggest_for_unmatched(t, open_invoices, recurring, all_invoices)
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

    items.extend(_overdue_payable_vraagposten(all_invoices, today, latest_statement_date))
    items.extend(_old_unreviewed_vraagposten(session, today))

    items.sort(key=lambda v: (-v.age_days, -_vraagpost_amount_cents(v)))
    return items
