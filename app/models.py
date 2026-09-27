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


class Transaction(Base):
    """A single bank statement line, as reported by Basecone."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("basecone_id", name="uq_transactions_basecone_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    basecone_id: Mapped[str] = mapped_column(String, index=True)
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="matches")
    invoice: Mapped["Invoice"] = relationship(back_populates="matches")
