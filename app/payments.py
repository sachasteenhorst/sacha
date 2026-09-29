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

# Subjects that mean "this document is itself a direct-debit notification",
# even when the document text didn't trip _text_suggests_incasso and/or
# document_kind wasn't classified as SPECIFICATION -- real production case:
# Giant's English-language "Advance Notification of Direct Debit" isn't
# matched by app.email_client.SPECIFICATION_RE (Dutch-only), so it landed
# on "Nog te betalen" as a plain invoice despite being, by definition, an
# announcement of money that's ABOUT to be collected automatically.
_INCASSO_SPECIFICATION_SUBJECT_RE_PARTS = (
    "advance notification of direct debit",
    "specificatie automatische incasso",
)


def _is_incasso_specification_subject(invoice: Invoice) -> bool:
    subject_lower = (invoice.email_subject or "").lower()
    return any(part in subject_lower for part in _INCASSO_SPECIFICATION_SUBJECT_RE_PARTS)


def _has_own_vat_number_as_invoice_number(invoice: Invoice) -> bool:
    """Real production bug: a document where OUR OWN BTW-nummer got read as
    the "factuurnummer" is, definitionally, a document about us as the
    seller -- one of our own sales invoices, not a bill to pay."""
    own_vat = settings.own_vat_number.replace(" ", "").lower()
    if not own_vat:
        return False
    invoice_number = (invoice.invoice_number or "").replace(" ", "").lower()
    return own_vat in invoice_number


def _is_consumer_domain_supplier(invoice: Invoice) -> bool:
    """A document from a personal gmail/hotmail/ziggo/... address is almost
    never a genuine business-to-business inkoopfactuur."""
    email_from = (invoice.email_from or "").lower()
    if "@" not in email_from:
        return False
    domain = email_from.rsplit("@", 1)[1]
    labels = domain.split(".")
    return any(consumer_domain in labels for consumer_domain in settings.consumer_email_domain_list)


def _not_addressed_to_own_company(invoice: Invoice) -> bool:
    """True when NONE of our own company names appear anywhere in the
    document's text -- a real inkoopfactuur addressed to us always mentions
    our name/address somewhere; one that doesn't is more likely addressed to
    a customer or a private individual (e.g. their own receipt, forwarded or
    CC'd into a scanned mailbox by mistake) than a bill we owe. Skipped
    entirely (never excludes) when OWN_COMPANY_NAMES itself is empty, since
    then there's nothing to check against."""
    own_names = settings.own_company_name_list
    if not own_names:
        return False
    text_lower = (invoice.extracted_text or "").lower()
    if not text_lower:
        return False  # no text to check at all -- not enough signal to exclude on this basis
    return not any(name in text_lower for name in own_names)


def _is_payable_ignoring_receipt_date(invoice: Invoice) -> bool:
    """Every is_payable_invoice check EXCEPT the PAY_FROM_DATE cutoff --
    used both by is_payable_invoice itself and by
    app.vraagposten to recognise an invoice that would be payable except
    that it's simply too old to trust (see PAY_FROM_DATE)."""
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
    if _has_own_vat_number_as_invoice_number(invoice):
        return False
    if _is_consumer_domain_supplier(invoice):
        return False
    if _not_addressed_to_own_company(invoice):
        return False
    if _is_incasso_specification_subject(invoice):
        return False
    return True


def is_payable_invoice(invoice: Invoice) -> bool:
    """Whether this invoice belongs on "Nog te betalen": an open, outgoing,
    non-incasso inkoopfactuur/creditnota with a positive amount that Sacha
    hasn't already marked paid by hand, actually addressed to this shop (not
    a private individual/consumer domain/our own VAT number as its
    "factuurnummer"), not a direct-debit-notification specification, and
    received on/after PAY_FROM_DATE (see _is_payable_ignoring_receipt_date
    for everything except that last check, and app.vraagposten for what
    happens to an invoice excluded ONLY because it's older than that)."""
    if not _is_payable_ignoring_receipt_date(invoice):
        return False
    received_date = invoice.received_at.date() if invoice.received_at else None
    if received_date is None or received_date < settings.pay_from_date_value:
        return False
    return True


def days_until_due(invoice: Invoice, today: date | None = None) -> int:
    """Negative = already overdue by that many days."""
    today = today or date.today()
    return (invoice.due_date - today).days


def mark_paid(invoice: Invoice) -> None:
    invoice.paid_at = datetime.utcnow()


def dedupe_key(invoice: Invoice) -> tuple | None:
    """Identity for "the same invoice, sent again" (a supplier's reminder
    e-mail, or the same invoice landing in two configured mailboxes without
    a byte-identical PDF so content_hash-based dedup didn't already catch
    it) -- None when there's no invoice_number to key on at all, since an
    empty number would otherwise group every such invoice together."""
    if not (invoice.invoice_number or "").strip():
        return None
    return (
        _supplier_name_lower(invoice.supplier_name),
        invoice.invoice_number.strip().lower(),
        invoice.amount_cents,
    )


def deduplicate_invoices(invoices: list[Invoice]) -> list[Invoice]:
    """Keeps only the earliest-received invoice per dedupe_key -- a supplier
    resending the same invoice (a payment reminder, the same document from
    two mailboxes) must only ever show up / get pushed once. An invoice
    with no usable dedupe_key (no invoice_number) is never deduplicated
    away, since there's nothing safe to group it by."""
    best_by_key: dict[tuple, Invoice] = {}
    passthrough: list[Invoice] = []
    for invoice in invoices:
        key = dedupe_key(invoice)
        if key is None:
            passthrough.append(invoice)
            continue
        current = best_by_key.get(key)
        if current is None or invoice.received_at < current.received_at:
            best_by_key[key] = invoice
    return [*best_by_key.values(), *passthrough]


def get_payable_invoices(session: Session) -> list[Invoice]:
    """The single source of truth for "Nog te betalen": every payable
    invoice, deduplicated, sorted by due date. Used by the dashboard, the
    push-notification code, and (for its "ignoring receipt date" sibling
    queries) app.vraagposten."""
    candidates = [inv for inv in session.scalars(select(Invoice)) if is_payable_invoice(inv)]
    return sorted(deduplicate_invoices(candidates), key=lambda inv: inv.due_date)


def is_duplicate_of_existing(session: Session, invoice: Invoice) -> bool:
    """True when some OTHER invoice with the same dedupe_key and an earlier
    received_at already exists anywhere in the database (any status,
    already notified or not) -- used to decide whether a "new" invoice is
    really just a supplier's reminder e-mail of one Sacha has already seen,
    so it can be marked notified without ever sending a duplicate push."""
    key = dedupe_key(invoice)
    if key is None:
        return False
    for other in session.scalars(select(Invoice)):
        if other.id == invoice.id:
            continue
        if dedupe_key(other) == key and other.received_at < invoice.received_at:
            return True
    return False


def get_old_unreviewed_invoices(session: Session) -> list[Invoice]:
    """Invoices that would otherwise belong on "Nog te betalen" but were
    received before PAY_FROM_DATE -- too old to trust blindly on a database
    upgrade, so app.vraagposten surfaces them instead as "oud, geen betaling
    gevonden -- controleren" rather than silently dropping them."""
    candidates = [
        inv for inv in session.scalars(select(Invoice))
        if _is_payable_ignoring_receipt_date(inv)
        and (inv.received_at is None or inv.received_at.date() < settings.pay_from_date_value)
    ]
    return deduplicate_invoices(candidates)
