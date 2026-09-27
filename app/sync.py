"""Pulls new bank transactions (Basecone) and invoices (mailbox), persists
them, and runs the matcher. This is what both the scheduler and the
dashboard's "Sync nu" button call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basecone_client import BaseconeClient
from app.config import settings
from app.email_client import fetch_invoice_attachments
from app.matcher import MatchingSummary, run_matching
from app.models import Invoice, Transaction


@dataclass
class SyncResult:
    new_transactions: int = 0
    new_invoices: int = 0
    errors: list[str] = None
    matching: MatchingSummary | None = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _sync_transactions(session: Session, since: date, result: SyncResult) -> None:
    try:
        client = BaseconeClient()
        fetched = client.fetch_bank_transactions(since)
    except Exception as exc:  # noqa: BLE001 -- surface any Basecone error to the dashboard
        result.errors.append(f"Basecone: {exc}")
        return

    existing_ids = set(
        session.scalars(select(Transaction.basecone_id).where(Transaction.basecone_id.in_([t.basecone_id for t in fetched])))
    )

    for t in fetched:
        if t.basecone_id in existing_ids:
            continue
        session.add(
            Transaction(
                basecone_id=t.basecone_id,
                booking_date=t.booking_date,
                amount_cents=t.amount_cents,
                currency=t.currency,
                description=t.description,
                counterparty_name=t.counterparty_name,
                counterparty_iban=t.counterparty_iban,
                reference=t.reference,
                raw_data=t.raw,
            )
        )
        result.new_transactions += 1


def _sync_invoices(session: Session, since: date, result: SyncResult) -> None:
    try:
        fetched = fetch_invoice_attachments(since)
    except Exception as exc:  # noqa: BLE001 -- surface any Graph API error to the dashboard
        result.errors.append(f"E-mail: {exc}")
        return

    existing_keys = set(
        session.execute(select(Invoice.email_message_id, Invoice.attachment_filename)).all()
    )

    for inv in fetched:
        key = (inv.email_message_id, inv.attachment_filename)
        if key in existing_keys:
            continue
        session.add(
            Invoice(
                email_message_id=inv.email_message_id,
                attachment_filename=inv.attachment_filename,
                email_subject=inv.email_subject,
                email_from=inv.email_from,
                received_at=inv.received_at,
                invoice_number=inv.invoice_number,
                invoice_date=inv.invoice_date,
                supplier_name=inv.supplier_name,
                amount_cents=inv.amount_cents,
                currency=inv.currency,
                extracted_text=inv.extracted_text,
                pdf_path=inv.pdf_path,
            )
        )
        result.new_invoices += 1


def run_sync(session: Session) -> SyncResult:
    since = date.today() - timedelta(days=settings.sync_lookback_days)
    result = SyncResult()

    _sync_transactions(session, since, result)
    _sync_invoices(session, since, result)
    session.flush()

    result.matching = run_matching(session)
    session.commit()
    return result
