"""Sends the "oude facturen" backlog -- every invoice excluded from
automatic forwarding (see Invoice.excluded_from_auto_forward, set by
scripts/fix_tenways.py/fix_cycletech.py/reparse_invoices.py's "reopened"
branch) -- to Basecone, slowly, in a background thread: 1 invoice every
BACKLOG_SEND_DELAY_SECONDS, with progress the dashboard can poll. Doing
this inside a single HTTP request would mean over a hundred invoices times
several seconds each is a request that can never finish before the
browser (or a reverse proxy) gives up on it.

Sacha reviews and confirms the list first (the dashboard's "Oude facturen
naar Basecone" page) -- this module only ever sends invoices it's
explicitly given, never decides on its own which ones to send.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basecone_forward import FORWARDABLE_DOCUMENT_KINDS, not_applicable_reason, send_invoice_to_basecone
from app.db import SessionLocal
from app.email_client import GraphApiError, GraphAuthError
from app.models import BaseconeForwardStatus, Invoice

SEND_DELAY_SECONDS = 2.0


def _backlog_eligible(invoice: Invoice) -> bool:
    """Same as basecone_forward.is_forwardable, except an invoice flagged
    "niet aan ons gericht" still appears here -- unchecked by default (see
    templates/backlog.html) -- so Sacha can override it per-invoice by
    ticking its box and sending anyway. Every other not_applicable_reason
    (own sales invoice, via CycleSoftware/Twinfield, incoming document) is
    a real "never send this" and stays hidden, same as everywhere else."""
    if invoice.document_kind not in FORWARDABLE_DOCUMENT_KINDS:
        return False
    reason = not_applicable_reason(invoice)
    return reason is None or reason == "niet aan ons gericht"


def backlog_candidates(session: Session) -> list[Invoice]:
    """Every invoice the dashboard's "Oude facturen naar Basecone" button
    offers: excluded from auto-forward, still forwardable (or only blocked
    by the addressee check), not yet sent."""
    candidates = session.scalars(
        select(Invoice).where(
            Invoice.excluded_from_auto_forward.is_(True),
            Invoice.in_basecone != BaseconeForwardStatus.YES.value,
        ).order_by(Invoice.supplier_name, Invoice.received_at)
    ).all()
    return [inv for inv in candidates if _backlog_eligible(inv)]


@dataclass
class BacklogSendState:
    running: bool = False
    total: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)
    done: bool = False


_state = BacklogSendState()
_lock = threading.Lock()


def get_state() -> BacklogSendState:
    return _state


def start_backlog_send(invoice_ids: list[int]) -> bool:
    """Starts the background send if nothing is already running. Returns
    False (starts nothing) if a send is already in progress -- never two
    overlapping sends at once."""
    with _lock:
        if _state.running:
            return False
        _state.running = True
        _state.total = len(invoice_ids)
        _state.sent = 0
        _state.errors = []
        _state.done = False
    thread = threading.Thread(target=_run_send, args=(invoice_ids,), daemon=True)
    thread.start()
    return True


def _run_send(invoice_ids: list[int]) -> None:
    # A background thread needs its OWN session -- SQLAlchemy sessions
    # aren't safe to share across threads.
    session = SessionLocal()
    try:
        for idx, invoice_id in enumerate(invoice_ids):
            invoice = session.get(Invoice, invoice_id)
            if invoice is not None and invoice.in_basecone != BaseconeForwardStatus.YES.value:
                try:
                    send_invoice_to_basecone(invoice, method="manual")
                    session.commit()
                    _state.sent += 1
                except (GraphAuthError, GraphApiError) as exc:
                    session.rollback()
                    label = invoice.supplier_name or invoice.attachment_filename or f"factuur {invoice.id}"
                    _state.errors.append(f"{label}: {exc}")
                    if isinstance(exc, GraphAuthError):
                        # Not logged in at all -- every subsequent send
                        # would fail identically, so stop the whole batch.
                        break
            if idx + 1 < len(invoice_ids):
                time.sleep(SEND_DELAY_SECONDS)
    finally:
        _state.running = False
        _state.done = True
        session.close()
