"""Files Basecone's "successfully processed" confirmation mails into a
dedicated Outlook folder (and marks them read) in each configured mailbox --
so facturen@/info@'s inbox doesn't fill up with confirmations nobody needs
to act on, while keeping proof they arrived.

UNVERIFIED sender/subject pattern (see app.config.Settings): this app has
never seen a real Basecone confirmation mail, so the defaults are a
best-effort guess -- the accountant's own mail-processing domain (the same
one BASECONE_FORWARD_ADDRESS sends to, which very likely sends the
confirmation back too) plus a generic "basecone.com", combined with a few
plausible Dutch/English subject phrases. Check a real confirmation mail's
sender and subject once you have one, and adjust
BASECONE_CONFIRMATION_SENDER_DOMAINS/_SUBJECT_KEYWORDS if they don't match
-- requiring BOTH a known domain AND a known subject phrase is deliberate,
so a looser pattern never silently files away unrelated mail from the same
domain.

Off entirely wherever the normal e-mail sync already is: a Graph/permission
problem here never breaks the sync (see file_basecone_confirmations) and is
surfaced in the dashboard's status bar the same way app.mail_labels is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config import settings
from app.email_client import (
    GraphApiError,
    GraphAuthError,
    create_mail_folder,
    find_mail_folder_id,
    list_inbox_messages_since,
    mark_message_read,
    move_message,
)

# Per-mailbox cache of the confirmation folder's id -- a folder basically
# never changes for the life of the process, same reasoning as
# app.mail_labels._ensured_mailboxes.
_folder_ids: dict[str, str] = {}


def _reset_folder_cache() -> None:
    """Test-only: lets a test simulate a fresh process."""
    _folder_ids.clear()


def ensure_confirmation_folder(mailbox: str) -> str:
    """Returns the confirmation folder's id in `mailbox`, creating it first
    if it doesn't exist yet. Unlike app.mail_labels' category setup, a
    failure here is never swallowed -- there's nowhere to file a message
    without a folder, so GraphApiError/GraphAuthError propagates and the
    caller decides how to surface it."""
    if mailbox in _folder_ids:
        return _folder_ids[mailbox]
    folder_id = find_mail_folder_id(mailbox, settings.basecone_confirmation_folder)
    if folder_id is None:
        folder_id = create_mail_folder(mailbox, settings.basecone_confirmation_folder)
    _folder_ids[mailbox] = folder_id
    return folder_id


def is_basecone_confirmation(message: dict) -> bool:
    """A confirmation mail: the sender's domain is one of the configured
    ones AND the subject contains one of the configured keywords --
    requiring both avoids silently filing away other, unrelated mail from
    the same domain (see module docstring on why these are a best-effort
    guess)."""
    address = ((message.get("from") or {}).get("emailAddress", {}).get("address") or "").lower()
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1]
    if not any(configured in domain for configured in settings.basecone_confirmation_sender_domain_list):
        return False
    subject = (message.get("subject") or "").lower()
    return any(keyword in subject for keyword in settings.basecone_confirmation_subject_keyword_list)


@dataclass
class FilingResult:
    moved: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class FilingSyncState:
    """In-memory, like app/bank_ponto.py's PontoSyncState -- only needed for
    the dashboard's status bar; resets harmlessly on a restart."""
    last_error: str | None = None


_state = FilingSyncState()


def get_state() -> FilingSyncState:
    return _state


def record_filing_errors(errors: list[str]) -> None:
    """Authoritative -- call once per full batch (the sync's periodic
    filing pass). Clears the status bar's error when a run goes clean."""
    _state.last_error = errors[0] if errors else None


def file_basecone_confirmations(since: date, limit: int | None = None) -> FilingResult:
    """Moves every matching confirmation mail (see is_basecone_confirmation)
    found in each configured mailbox's inbox, received on/after `since`,
    into that mailbox's confirmation folder, and marks it read. Stops the
    whole run immediately on a GraphAuthError (not logged in at all, so
    every subsequent call would fail identically); continues past a single
    mailbox's GraphApiError so one mailbox's permission problem never
    blocks the other."""
    result = FilingResult()
    limit = settings.basecone_confirmation_max_per_sync if limit is None else limit

    for mailbox in settings.graph_mailboxes:
        try:
            folder_id = ensure_confirmation_folder(mailbox)
            messages = [m for m in list_inbox_messages_since(mailbox, since) if is_basecone_confirmation(m)]
        except GraphAuthError as exc:
            result.errors.append(str(exc))
            break
        except GraphApiError as exc:
            result.errors.append(f"Basecone-bevestigingsmails ({mailbox}): {exc}")
            continue

        for message in messages[:limit]:
            try:
                if not message.get("isRead"):
                    mark_message_read(mailbox, message["id"])
                move_message(mailbox, message["id"], folder_id)
                result.moved += 1
            except GraphAuthError as exc:
                result.errors.append(str(exc))
                return result
            except GraphApiError as exc:
                result.errors.append(f"Basecone-bevestigingsmail verplaatsen ({mailbox}): {exc}")
                continue
    return result


def filing_error_hint(error: str | None) -> str | None:
    """Turns a raw filing error into one short, actionable Dutch sentence --
    mirrors app.mail_labels.label_error_hint."""
    if not error:
        return None
    lowered = error.lower()
    if "403" in lowered or "mail.readwrite" in lowered:
        return (
            "Rechten om Basecone-bevestigingsmails te verplaatsen ontbreken -- "
            "voeg Mail.ReadWrite.Shared toe in Entra en log opnieuw in met "
            "scripts/graph_login.py (zie README)."
        )
    if "niet ingelogd" in lowered or "nog niet ingelogd" in lowered or "graph_login" in lowered:
        return "Nog niet ingelogd bij Microsoft Graph -- draai scripts/graph_login.py."
    return "Basecone-bevestigingsmails verplaatsen mislukt -- zie de logs voor het volledige foutbericht."
