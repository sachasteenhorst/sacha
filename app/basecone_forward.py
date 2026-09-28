"""Gets every inkoopfactuur to the accountant's Basecone inbox
(BASECONE_FORWARD_ADDRESS) -- a "matched" invoice that never actually
reached that address is still, from the boekhouder's point of view, a
vraagpost. Three ways a document ends up marked in_basecone="yes":

1. "original" -- the supplier already put the Basecone address on to/cc/bcc
   (checked at fetch time, see app/email_client.py's
   message_was_sent_to_basecone).
2. "auto" -- AUTO_FORWARD_BASECONE is on and this document arrived on/after
   the cutover date (see resolve_auto_forward_since): sent automatically as
   part of the sync, with only the invoice PDF attached.
3. "manual" -- you clicked "Stuur naar Basecone" in the dashboard.
4. "detected" -- a backlog document (received before the cutover): found
   already sitting in Sent Items from a manual Outlook forward you did
   before this feature existed.
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.email_client import (
    GraphApiError,
    GraphAuthError,
    list_message_attachment_names,
    list_sent_items_since,
    send_mail,
)
from app.models import BaseconeForwardStatus, DocumentKind, Invoice

AUTO_FORWARD_MARKER_FILE = "./data/auto_forward_since.txt"

# A supplier's own subject stays the identifying part even after being
# forwarded/replied to a few times ("RE: RE: Factuur 123" -> "Factuur 123").
_FORWARD_PREFIX_RE = re.compile(r"^(re|fw|fwd|doorst(?:uren)?)\.?\s*:\s*", re.IGNORECASE)

# Document kinds that are ever worth sending to Basecone at all -- general
# terms, packing slips, our own sales invoices etc. (DocumentKind.OTHER)
# never are.
FORWARDABLE_DOCUMENT_KINDS = {DocumentKind.INVOICE.value, DocumentKind.SPECIFICATION.value}


def normalize_subject(subject: str) -> str:
    subject = subject or ""
    while True:
        new = _FORWARD_PREFIX_RE.sub("", subject).strip()
        if new == subject:
            return subject
        subject = new


def is_confident(invoice: Invoice) -> bool:
    """Whether extraction found enough (a number AND an amount) that
    auto-forwarding it makes sense -- otherwise it's not this document's
    fault, but sending an unverified guess to the accountant isn't either;
    it goes in the "Controleer eerst" list for you to check by hand."""
    return bool(invoice.invoice_number) and invoice.amount_cents is not None


def is_forwardable(invoice: Invoice) -> bool:
    return invoice.document_kind in FORWARDABLE_DOCUMENT_KINDS and invoice.in_basecone != BaseconeForwardStatus.YES.value


def resolve_auto_forward_since() -> date:
    """The cutover date for auto-forwarding: an explicit AUTO_FORWARD_FROM_DATE
    always wins; otherwise the first moment AUTO_FORWARD_BASECONE was ever
    turned on gets recorded to disk and reused from then on, so it never
    silently drifts to "today" again on a later restart."""
    if settings.auto_forward_from_date:
        return date.fromisoformat(settings.auto_forward_from_date)
    if os.path.exists(AUTO_FORWARD_MARKER_FILE):
        with open(AUTO_FORWARD_MARKER_FILE) as fh:
            stored = fh.read().strip()
            if stored:
                return date.fromisoformat(stored)
    today = date.today()
    os.makedirs(os.path.dirname(AUTO_FORWARD_MARKER_FILE), exist_ok=True)
    with open(AUTO_FORWARD_MARKER_FILE, "w") as fh:
        fh.write(today.isoformat())
    return today


def _build_message(invoice: Invoice) -> dict:
    with open(invoice.pdf_path, "rb") as fh:
        content_b64 = base64.b64encode(fh.read()).decode("ascii")
    received_label = invoice.received_at.strftime("%d-%m-%Y %H:%M") if invoice.received_at else "?"
    body_text = (
        "Automatisch doorgestuurd vanuit het administratie-dashboard.\n\n"
        f"Oorspronkelijke afzender: {invoice.email_from}\n"
        f"Oorspronkelijke datum: {received_label}\n"
        f"Oorspronkelijk onderwerp: {invoice.email_subject}\n"
    )
    return {
        "subject": invoice.email_subject or invoice.attachment_filename,
        "body": {"contentType": "Text", "content": body_text},
        "toRecipients": [{"emailAddress": {"address": settings.basecone_forward_address}}],
        "attachments": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": invoice.attachment_filename or os.path.basename(invoice.pdf_path),
                "contentBytes": content_b64,
            }
        ],
    }


def send_invoice_to_basecone(invoice: Invoice, method: str) -> None:
    """Sends just the invoice PDF (never the rest of the original e-mail's
    attachments) to BASECONE_FORWARD_ADDRESS, trying the mailbox it arrived
    in first (needs Mail.Send.Shared) and falling back to "me" (Mail.Send)
    -- then records when/how/where. Raises GraphAuthError/GraphApiError on
    failure; the caller decides how to surface that."""
    if not settings.basecone_forward_address:
        raise GraphAuthError("BASECONE_FORWARD_ADDRESS is niet ingesteld.")
    if not invoice.pdf_path or not os.path.isfile(invoice.pdf_path):
        raise GraphApiError("PDF-bestand niet gevonden op schijf -- kan niet doorsturen.")

    message = _build_message(invoice)
    mailboxes_to_try = list(dict.fromkeys([b for b in (invoice.graph_mailbox, "me") if b]))
    if not mailboxes_to_try:
        mailboxes_to_try = ["me"]

    last_error: Exception | None = None
    for mailbox in mailboxes_to_try:
        try:
            send_mail(mailbox, message)
        except GraphApiError as exc:
            last_error = exc
            continue
        invoice.in_basecone = BaseconeForwardStatus.YES.value
        invoice.basecone_forwarded_at = datetime.utcnow()
        invoice.basecone_forward_method = method
        invoice.basecone_forwarded_to = settings.basecone_forward_address
        return
    raise last_error or GraphApiError("Versturen mislukt.")


@dataclass
class AutoForwardResult:
    sent: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def auto_forward_new_invoices(session: Session) -> AutoForwardResult:
    """Sends every eligible NEW (received on/after the cutover date)
    invoice/specification not yet in Basecone, up to
    AUTO_FORWARD_MAX_PER_SYNC per call. Never touches backlog documents
    (older than the cutover) or ones extraction wasn't confident about --
    see is_confident/is_forwardable."""
    result = AutoForwardResult()
    if not settings.auto_forward_basecone or not settings.basecone_forward_address:
        return result

    since = resolve_auto_forward_since()
    candidates = session.scalars(
        select(Invoice).where(Invoice.in_basecone != BaseconeForwardStatus.YES.value)
    ).all()

    eligible = [
        inv for inv in candidates
        if is_forwardable(inv) and is_confident(inv) and inv.received_at and inv.received_at.date() >= since
    ]
    for invoice in eligible[: settings.auto_forward_max_per_sync]:
        try:
            send_invoice_to_basecone(invoice, method="auto")
            result.sent += 1
        except (GraphAuthError, GraphApiError) as exc:
            result.errors.append(f"Basecone doorsturen ({invoice.attachment_filename}): {exc}")
    return result


def check_backlog_against_sent_items(session: Session) -> int:
    """For backlog documents (older than the auto-forward cutover, or when
    AUTO_FORWARD_BASECONE has never been on) still marked "unknown": scans
    Sent Items of every configured mailbox plus "me" for a message to
    BASECONE_FORWARD_ADDRESS with the same (RE/FW-stripped) subject or the
    same attachment filename -- i.e. a manual forward done before this
    feature existed. Returns how many got marked in_basecone="yes"."""
    if not settings.basecone_forward_address:
        return 0

    since = resolve_auto_forward_since()
    candidates = session.scalars(
        select(Invoice).where(
            Invoice.in_basecone == BaseconeForwardStatus.UNKNOWN.value,
            Invoice.document_kind.in_(FORWARDABLE_DOCUMENT_KINDS),
        )
    ).all()
    backlog = [inv for inv in candidates if not inv.received_at or inv.received_at.date() < since]
    if not backlog:
        return 0

    oldest = min(inv.received_at for inv in backlog if inv.received_at).date()
    mailboxes = list(dict.fromkeys([*settings.graph_mailboxes, "me"]))

    forwarded_subjects: set[str] = set()
    forwarded_filenames: set[str] = set()
    for mailbox in mailboxes:
        try:
            sent = list_sent_items_since(mailbox, oldest)
        except (GraphAuthError, GraphApiError):
            continue
        for message in sent:
            address = settings.basecone_forward_address.lower()
            recipients = [
                (r.get("emailAddress", {}).get("address") or "").lower()
                for field in ("toRecipients", "ccRecipients", "bccRecipients")
                for r in message.get(field) or []
            ]
            if address not in recipients:
                continue
            forwarded_subjects.add(normalize_subject(message.get("subject", "")))
            # Only fetched for messages that already matched on recipient --
            # keeps this to one extra call per real candidate, not per
            # Sent Items message.
            forwarded_filenames.update(list_message_attachment_names(mailbox, message["id"]))

    updated = 0
    for invoice in backlog:
        matched_subject = normalize_subject(invoice.email_subject) in forwarded_subjects
        matched_filename = invoice.attachment_filename in forwarded_filenames
        if matched_subject or matched_filename:
            invoice.in_basecone = BaseconeForwardStatus.YES.value
            invoice.basecone_forward_method = "detected"
            updated += 1
    return updated
