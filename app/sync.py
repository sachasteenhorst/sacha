"""Pulls new bank transactions (Basecone) and invoices (mailbox), persists
them, and runs the matcher. This is what both the scheduler and the
dashboard's "Sync nu" button call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank_import import BankImportError, parse_bank_file
from app.basecone_client import BaseconeClient
from app.basecone_forward import auto_forward_new_invoices, check_backlog_against_sent_items
from app.config import settings
from app.email_client import fetch_invoice_attachments, invoice_dedup_key
from app.matcher import MatchingSummary, run_matching
from app.models import DocumentKind, Invoice, MatchStatus, Transaction
from app.rules import apply_rules
from app import sync_state


@dataclass
class SyncResult:
    new_transactions: int = 0
    new_invoices: int = 0
    basecone_auto_forwarded: int = 0
    errors: list[str] = None
    matching: MatchingSummary | None = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


@dataclass
class BankImportResult:
    new_transactions: int = 0
    skipped: int = 0
    errors: list[str] = None
    matching: MatchingSummary | None = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _sync_transactions(session: Session, since: date, result: SyncResult) -> None:
    # Basecone has purchase/sales bookings and documents, not bank statement
    # lines -- it's not a usable source for the account's transactions.
    # Off by default so a sync never reports a spurious error; only runs if
    # someone deliberately turns it on for something Basecone-specific.
    if not settings.basecone_enabled:
        return

    try:
        client = BaseconeClient()
        fetched = client.fetch_bank_transactions(since)
    except Exception as exc:  # noqa: BLE001 -- surface any Basecone error to the dashboard
        result.errors.append(f"Basecone: {exc}")
        return

    existing_ids = set(
        session.scalars(select(Transaction.external_ref).where(Transaction.external_ref.in_([t.basecone_id for t in fetched])))
    )

    for t in fetched:
        if t.basecone_id in existing_ids:
            continue
        session.add(
            Transaction(
                external_ref=t.basecone_id,
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
    # Cross-mailbox/cross-run dedup by document identity, not just message
    # identity -- facturen@ and info@ often receive the exact same invoice
    # as two genuinely separate e-mails (see invoice_dedup_key).
    existing_dedup_keys = {
        key for key in (
            invoice_dedup_key(content_hash, supplier_name, invoice_number, amount_cents)
            for content_hash, supplier_name, invoice_number, amount_cents in session.execute(
                select(Invoice.content_hash, Invoice.supplier_name, Invoice.invoice_number, Invoice.amount_cents)
            )
        )
        if key is not None
    }

    for inv in fetched:
        key = (inv.email_message_id, inv.attachment_filename)
        if key in existing_keys:
            continue
        dedup_key = invoice_dedup_key(inv.content_hash, inv.supplier_name, inv.invoice_number, inv.amount_cents)
        if dedup_key is not None and dedup_key in existing_dedup_keys:
            continue
        if dedup_key is not None:
            existing_dedup_keys.add(dedup_key)
        # A document classified as "other" (general terms, a bank-account
        # change notice, an amount-less packing slip, ...) is never proof of
        # a payment -- filed straight to IGNORED so it doesn't clutter the
        # open-invoices list.
        status = MatchStatus.IGNORED if inv.document_kind == DocumentKind.OTHER.value else MatchStatus.UNMATCHED
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
                direction=inv.direction,
                document_kind=inv.document_kind,
                referenced_invoice_numbers=inv.referenced_invoice_numbers,
                content_hash=inv.content_hash,
                graph_mailbox=inv.graph_mailbox,
                graph_message_id=inv.graph_message_id,
                in_basecone=inv.in_basecone,
                basecone_forward_method=inv.basecone_forward_method,
                basecone_forwarded_at=datetime.utcnow() if inv.basecone_forward_method == "original" else None,
                basecone_forwarded_to=settings.basecone_forward_address if inv.basecone_forward_method == "original" else "",
                status=status,
            )
        )
        result.new_invoices += 1


def import_bank_file(session: Session, filename: str, content: bytes) -> BankImportResult:
    """Parses an uploaded Rabobank export (CSV/CAMT.053/MT940), stores the
    new statement lines, and runs the matcher. Called from the dashboard's
    upload route."""
    result = BankImportResult()
    try:
        rows = parse_bank_file(filename, content)
    except BankImportError as exc:
        result.errors.append(str(exc))
        return result

    if not rows:
        result.errors.append("Geen transacties gevonden in dit bestand.")
        return result

    existing_refs = set(
        session.scalars(
            select(Transaction.external_ref).where(
                Transaction.external_ref.in_([r.external_ref for r in rows])
            )
        )
    )

    for row in rows:
        if row.external_ref in existing_refs:
            result.skipped += 1
            continue
        session.add(
            Transaction(
                external_ref=row.external_ref,
                booking_date=row.booking_date,
                amount_cents=row.amount_cents,
                currency=row.currency,
                description=row.description,
                counterparty_name=row.counterparty_name,
                counterparty_iban=row.counterparty_iban,
                reference=row.reference,
                bank_code=row.bank_code,
                raw_data=row.raw,
            )
        )
        existing_refs.add(row.external_ref)  # guard duplicate rows within the same file
        result.new_transactions += 1

    session.flush()
    # A real invoice match always wins over a rule (e.g. a "geen factuur
    # nodig"/"omzet" rule someone made for a counterparty that ALSO happens
    # to send real invoices, like a leasing platform) -- run the matcher
    # first, and let rules mop up only what's still open afterwards.
    result.matching = run_matching(session)
    apply_rules(session)
    session.commit()
    return result


def run_sync(session: Session) -> SyncResult:
    since = date.today() - timedelta(days=settings.sync_lookback_days)
    result = SyncResult()

    _sync_transactions(session, since, result)
    _sync_invoices(session, since, result)
    session.flush()

    result.matching = run_matching(session)
    apply_rules(session)
    session.commit()

    # Independent of matching -- purely about getting documents to the
    # accountant's Basecone inbox. Never let a Graph/permission problem here
    # abort the rest of the sync; errors are just appended and shown.
    try:
        check_backlog_against_sent_items(session)
        forward_result = auto_forward_new_invoices(session)
        result.basecone_auto_forwarded = forward_result.sent
        result.errors.extend(forward_result.errors)
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- surface any Basecone-forward error to the dashboard
        session.rollback()
        result.errors.append(f"Basecone doorsturen: {exc}")

    sync_state.record(result)
    return result
