"""ORM models.

Amounts are stored as integer cents to avoid floating point rounding
problems when comparing bank transactions against invoice totals.
Bank transaction amounts are signed (negative = money out of the account,
positive = money in). Invoice amounts are stored unsigned (the amount due).
"""
import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class MatchStatus(str, enum.Enum):
    UNMATCHED = "unmatched"
    SUGGESTED = "suggested"
    MATCHED = "matched"
    IGNORED = "ignored"
    # Transaction-only: the receipt for this payment has been photographed
    # into the Basecone app separately -- handled, but distinct from
    # IGNORED ("no receipt needed at all", e.g. bank costs).
    RECEIPT_ELSEWHERE = "receipt_elsewhere"
    # Transaction-only: an automatic Rule (see Rule below) resolved this --
    # kept separate from IGNORED/RECEIPT_ELSEWHERE so it has its own visible
    # "Automatisch afgehandeld" section and can be undone as a batch.
    RULE_HANDLED = "rule_handled"


class RuleAction(str, enum.Enum):
    NO_INVOICE_NEEDED = "no_invoice_needed"
    REVENUE = "revenue"  # "omzet" -- income from the shop, e.g. card terminal settlements


class Direction(str, enum.Enum):
    """Which way money moves for a document. Most suppliers collect money
    FROM the account (outgoing); a few (ENRA settlements, HelloRider) pay
    money ONTO it (incoming). A transaction's own direction isn't stored --
    it's just the sign of its amount_cents (positive = incoming)."""
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class DocumentKind(str, enum.Enum):
    INVOICE = "invoice"
    # A single document (e.g. Accell's "Specificatie automatische incasso")
    # that lists several invoice numbers settled by one bank payment.
    SPECIFICATION = "specification"
    # General terms, a "bank account changed" notice, a packing slip with
    # no amount -- not proof of a payment, so it shouldn't count as an open
    # invoice waiting to be matched.
    OTHER = "other"


class BaseconeForwardStatus(str, enum.Enum):
    """Whether an invoice's original e-mail was ever forwarded to the
    accountant's Basecone inbox (BASECONE_FORWARD_ADDRESS) -- the boekhouder
    never sees a document until it lands there, so a "matched" invoice that's
    still UNKNOWN/NO is effectively still a vraagpost."""
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Transaction(Base):
    """A single bank statement line, imported from a Rabobank export
    (CSV/CAMT.053/MT940)."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("basecone_id", name="uq_transactions_basecone_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Column name kept as "basecone_id" (its original purpose) to avoid an
    # ALTER TABLE RENAME on the production database; it's now a generic
    # unique key for a statement line from whatever source produced it
    # (Rabobank CSV/CAMT.053/MT940 upload).
    external_ref: Mapped[str] = mapped_column("basecone_id", String, index=True)
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    description: Mapped[str] = mapped_column(Text, default="")
    counterparty_name: Mapped[str] = mapped_column(String, default="")
    counterparty_iban: Mapped[str] = mapped_column(String, default="")
    reference: Mapped[str] = mapped_column(String, default="")
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)

    # Rabobank's own two-letter transaction code from the CSV "Code" column
    # (e.g. "tb" = transfer between your own accounts, "ba" = card
    # payment). Empty for CAMT.053/MT940 uploads, which don't carry a
    # directly equivalent code. Added after the first release -- see
    # app/db.py's startup migration.
    bank_code: Mapped[str] = mapped_column(String, default="")
    # Which Rule (if any) auto-resolved this transaction -- see Rule below.
    # Added after the first release -- see app/db.py's startup migration.
    applied_rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id"), nullable=True)

    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.UNMATCHED, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    matches: Mapped[list["Match"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")
    applied_rule: Mapped["Rule | None"] = relationship(back_populates="transactions")


class Invoice(Base):
    """An invoice PDF found as an email attachment."""

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("email_message_id", "attachment_filename", name="uq_invoice_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_message_id: Mapped[str] = mapped_column(String, index=True)
    attachment_filename: Mapped[str] = mapped_column(String)
    email_subject: Mapped[str] = mapped_column(String, default="")
    email_from: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime)

    invoice_number: Mapped[str] = mapped_column(String, default="")
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier_name: Mapped[str] = mapped_column(String, default="")
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")

    extracted_text: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String, default="")

    # Added after the first release -- see app/db.py's startup migration.
    direction: Mapped[str] = mapped_column(String, default=Direction.OUTGOING.value)
    document_kind: Mapped[str] = mapped_column(String, default=DocumentKind.INVOICE.value)
    # For a SPECIFICATION document: the invoice numbers mentioned inside it
    # (e.g. Accell's "Specificatie automatische incasso" listing several
    # invoices settled by one direct debit).
    referenced_invoice_numbers: Mapped[list] = mapped_column(JSON, default=list)

    # SHA-256 of the raw PDF bytes -- the same invoice often lands twice (once
    # per scanned mailbox); this is what dedup keys on across mailboxes/runs,
    # since the message ids genuinely differ but the attached file doesn't.
    # Added after the first release -- see app/db.py's startup migration.
    content_hash: Mapped[str] = mapped_column(String, default="", index=True)
    # Which mailbox + Graph-internal message id (NOT internetMessageId) this
    # came from -- needed to call the Graph "forward" endpoint later without
    # having to re-search for the message. Added after the first release.
    graph_mailbox: Mapped[str] = mapped_column(String, default="")
    graph_message_id: Mapped[str] = mapped_column(String, default="")
    # Whether the original e-mail was ever forwarded to the accountant's
    # Basecone inbox (see BaseconeForwardStatus). Added after the first
    # release -- see app/db.py's startup migration.
    in_basecone: Mapped[str] = mapped_column(String, default=BaseconeForwardStatus.UNKNOWN.value)
    basecone_forwarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "original" (supplier already cc'd/bcc'd Basecone directly), "auto"
    # (this app's own AUTO_FORWARD_BASECONE sync sent it), "manual" (sent by
    # clicking the button), "detected" (found already sitting in Sent Items
    # from before this feature existed).
    basecone_forward_method: Mapped[str] = mapped_column(String, default="")
    basecone_forwarded_to: Mapped[str] = mapped_column(String, default="")
    # True only when Sacha clicked "Negeren" in the dashboard -- distinct
    # from the matcher/reparse script auto-setting IGNORED because
    # document_kind turned out to be "other". reparse_invoices.py may
    # revert the LATTER back to UNMATCHED if better extraction later
    # recognises the document as a real invoice after all; it must never
    # touch a document Sacha ignored by hand. Added after the first
    # release -- see app/db.py's startup migration.
    manually_ignored: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.UNMATCHED, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    matches: Mapped[list["Match"]] = relationship(back_populates="invoice", cascade="all, delete-orphan")


class MatchMethod(str, enum.Enum):
    REFERENCE = "reference"       # invoice number found verbatim in transaction description
    AMOUNT_DATE = "amount_date"   # amount matches exactly, date within tolerance window
    MANUAL = "manual"             # confirmed/created by hand in the dashboard


class Match(Base):
    """A (candidate) link between a bank transaction and an invoice."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))

    method: Mapped[MatchMethod] = mapped_column(Enum(MatchMethod))
    confidence: Mapped[str] = mapped_column(String, default="medium")  # high | medium | low
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # One payment can settle several documents (an Accell direct-debit
    # specification listing multiple invoices, or a combination-of-invoices
    # match). Every Match row created together for one transaction shares a
    # group_id, so confirming/rejecting acts on the whole group at once.
    # Added after the first release -- see app/db.py's startup migration.
    group_id: Mapped[str] = mapped_column(String, index=True, default="")
    # A REFERENCE match doesn't require the amount to line up exactly -- a
    # small shortfall is accepted as betalingskorting (early-payment
    # discount). Positive = paid less than invoiced. Added after the first
    # release -- see app/db.py's startup migration.
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="matches")
    invoice: Mapped["Invoice"] = relationship(back_populates="matches")


class Rule(Base):
    """Auto-resolves a bank transaction that will never have an invoice or
    receipt (a bank fee, a transfer between your own accounts, a card
    terminal settlement that's just shop revenue, ...) -- applied to every
    still-open transaction before the matcher runs. Each criterion left
    blank is ignored; all the ones that are set must match (AND)."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    action: Mapped[str] = mapped_column(String)  # RuleAction value

    counterparty_contains: Mapped[str] = mapped_column(String, default="")
    counterparty_iban: Mapped[str] = mapped_column(String, default="")
    description_contains: Mapped[str] = mapped_column(String, default="")
    transaction_code: Mapped[str] = mapped_column(String, default="")  # Rabobank CSV "Code" column
    direction: Mapped[str] = mapped_column(String, default="")  # "" = either direction

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="applied_rule")
