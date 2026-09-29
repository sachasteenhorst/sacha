""""Nog te betalen": which open, outgoing inkoopfacturen actually need Sacha
to transfer money by hand -- as opposed to invoices that settle themselves
via automatic incasso, sales invoices, or documents from a supplier that
never needs a bill paid at all (see is_payable_invoice).

Incasso is determined once, when an invoice first comes in (see
determine_payment_method, called from app/sync.py), from four signals,
tried in this order:

1. PAY_MANUAL_SUPPLIERS always wins -- an explicit "no, this one is NOT
   incasso" override for a supplier that would otherwise match one of the
   signals below.
2. A supplier Sacha already told the dashboard about via "Loopt via
   incasso" (see LearnedIncassoSupplier) -- learned once, remembered
   forever.
3. The invoice's own PDF text says so (see
   app.email_client._text_suggests_incasso).
4. An EXISTING bank transaction from a name-similar counterparty already
   looks like a direct debit (Rabobank CSV code "ei"/"id", or a filled-in
   Machtigingskenmerk/Incassant ID) -- proof this supplier is already being
   collected from automatically, even if this particular invoice's own text
   didn't say so.
5. PAY_INCASSO_SUPPLIERS -- a configured allowlist of suppliers known to
   collect by incasso.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.matcher import _name_similarity
from app.models import DocumentKind, Invoice, LearnedIncassoSupplier, MatchStatus, PaymentMethod, Transaction

# Rabobank's own two-letter codes for a direct debit: "ei" (Europese
# incasso) and "id" (incasso doorlopend) -- see the CSV "Code" column.
INCASSO_BANK_CODES = {"ei", "id"}
# Column names as Rabobank's own CSV export spells them (case preserved in
# raw_data as parsed) -- checked case-insensitively below since a CAMT.053/
# MT940 import never has these at all.
INCASSO_RAW_DATA_KEYS = {"machtigingskenmerk", "incassant id", "incassant-id"}


def _supplier_name_lower(supplier_name: str) -> str:
    return (supplier_name or "").strip().lower()


def _is_manually_overridden(supplier_name: str) -> bool:
    name_lower = _supplier_name_lower(supplier_name)
    return any(name and name in name_lower for name in settings.pay_manual_supplier_names)


def _is_learned_incasso_supplier(session: Session, supplier_name: str) -> bool:
    name_lower = _supplier_name_lower(supplier_name)
    if not name_lower:
        return False
    learned = session.scalars(select(LearnedIncassoSupplier.supplier_name_lower)).all()
    return any(learned_name and learned_name in name_lower for learned_name in learned)


def _is_configured_incasso_supplier(supplier_name: str) -> bool:
    name_lower = _supplier_name_lower(supplier_name)
    return any(name and name in name_lower for name in settings.pay_incasso_supplier_names)


def _transaction_looks_like_incasso(transaction: Transaction) -> bool:
    if (transaction.bank_code or "").strip().lower() in INCASSO_BANK_CODES:
        return True
    raw = transaction.raw_data or {}
    for key, value in raw.items():
        if key.strip().lower() in INCASSO_RAW_DATA_KEYS and (value or "").strip():
            return True
    return False


def _supplier_has_incasso_transaction(session: Session, supplier_name: str) -> bool:
    if not (supplier_name or "").strip():
        return False
    for transaction in session.scalars(select(Transaction)):
        if _name_similarity(supplier_name, transaction.counterparty_name) <= 0:
            continue
        if _transaction_looks_like_incasso(transaction):
            return True
    return False


def determine_payment_method(session: Session, supplier_name: str, incasso_hint: bool) -> str:
    """The payment_method to store on a newly-created Invoice. incasso_hint
    is the pure-text signal from app.email_client._text_suggests_incasso."""
    if _is_manually_overridden(supplier_name):
        return PaymentMethod.ONBEKEND.value
    is_incasso = (
        incasso_hint
        or _is_learned_incasso_supplier(session, supplier_name)
        or _is_configured_incasso_supplier(supplier_name)
        or _supplier_has_incasso_transaction(session, supplier_name)
    )
    return PaymentMethod.INCASSO.value if is_incasso else PaymentMethod.ONBEKEND.value


def learn_incasso_supplier(session: Session, supplier_name: str) -> None:
    """Records that this supplier is paid by incasso (the dashboard's
    "Loopt via incasso" button) and immediately reclassifies every one of
    its OTHER still-open invoices too, so the whole backlog drops off "Nog
    te betalen" at once instead of one row at a time."""
    name_lower = _supplier_name_lower(supplier_name)
    if not name_lower:
        return
    existing = session.scalar(
        select(LearnedIncassoSupplier).where(LearnedIncassoSupplier.supplier_name_lower == name_lower)
    )
    if existing is None:
        session.add(LearnedIncassoSupplier(supplier_name_lower=name_lower))

    for invoice in session.scalars(select(Invoice)):
        if _name_similarity(supplier_name, invoice.supplier_name) > 0:
            invoice.payment_method = PaymentMethod.INCASSO.value


# Document kinds that could ever represent a bill THIS shop has to pay --
# never a sales invoice (own revenue) or a non-invoice document.
_PAYABLE_DOCUMENT_KINDS = {DocumentKind.INVOICE.value, DocumentKind.SPECIFICATION.value}
_OPEN_STATUSES = {MatchStatus.UNMATCHED, MatchStatus.SUGGESTED}


def is_payable_invoice(invoice: Invoice) -> bool:
    """Whether this invoice belongs on "Nog te betalen": an open, outgoing,
    non-incasso inkoopfactuur/creditnota with a positive amount that Sacha
    hasn't already marked paid by hand. Explicitly excludes: incoming
    documents (ENRA/HelloRider/CycleSoftware sales invoices/...),
    specificaties that settle via incasso, credit notes/zero amounts, and
    anything already MATCHED, IGNORED or otherwise resolved."""
    if invoice.document_kind not in _PAYABLE_DOCUMENT_KINDS:
        return False
    if invoice.direction != "outgoing":
        return False
    if invoice.status not in _OPEN_STATUSES:
        return False
    if invoice.paid_at is not None:
        return False
    if invoice.payment_method == PaymentMethod.INCASSO.value:
        return False
    if invoice.amount_cents is None or invoice.amount_cents <= 0:
        return False
    if invoice.due_date is None:
        # Every invoice created via the normal ingestion path always gets at
        # least an estimated due_date (see app.email_client._extract_due_date)
        # -- None only happens for legacy rows from before this column
        # existed that haven't been reparsed yet, or hand-built test data.
        return False
    return True


def days_until_due(invoice: Invoice, today: date | None = None) -> int:
    """Negative = already overdue by that many days."""
    today = today or date.today()
    return (invoice.due_date - today).days


def mark_paid(invoice: Invoice) -> None:
    invoice.paid_at = datetime.utcnow()
