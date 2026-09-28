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

    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.UNMATCHED, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    matches: Mapped[list["Match"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="matches")
    invoice: Mapped["Invoice"] = relationship(back_populates="matches")
