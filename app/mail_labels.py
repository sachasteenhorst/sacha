"""Outlook categories (labels) on a factuurmail, via Microsoft Graph, so
opening the e-mail itself immediately shows whether it's already in
Basecone -- without having to switch to the dashboard.

Three categories, applied to the ORIGINAL e-mail (via its
graph_mailbox/graph_message_id -- see app/email_client.py), never touching
any other category already on the message:

- "Basecone ✓" (green)      -- in_basecone == "yes"
- "Nog naar Basecone" (orange)  -- an open inkoopfactuur/specificatie that
                                    still needs forwarding (see
                                    app.basecone_forward.is_forwardable)
- "Geen factuur" (grey)         -- everything else: document_kind "other",
                                    a manually-ignored document, or one that
                                    is_forwardable already excludes (a
                                    CycleSoftware/Lease a Bike-style sales
                                    copy, our own verkoopfactuur, ...)

Needs the DELEGATED Graph permissions Mail.ReadWrite.Shared (to patch a
message's categories in facturen@/info@) and MailboxSettings.ReadWrite (to
register a category's color in masterCategories) -- see README for the
exact Entra steps. The masterCategories step is treated as best-effort:
if it fails (e.g. that scope turns out not to apply to a shared mailbox,
which is unverified as of writing -- there's no documented ".Shared"
variant of MailboxSettings.ReadWrite), the category is still applied to
the message, just without its intended color until that's sorted out
(either by that call succeeding later, or by creating the three categories
once by hand directly in Outlook).

Off entirely wherever the normal e-mail sync already is: this reuses the
very same Graph refresh token, so if that isn't set up yet
(app.email_client.GraphAuthError), labeling is skipped the same way
sending/reading already is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basecone_forward import is_forwardable
from app.email_client import (
    GRAPH_BASE_URL,
    GraphApiError,
    GraphAuthError,
    _graph_get,
    _graph_patch,
    _graph_post,
)
from app.models import BaseconeForwardStatus, Invoice

CATEGORY_BASECONE_OK = "Basecone ✓"
CATEGORY_NOG_NAAR_BASECONE = "Nog naar Basecone"
CATEGORY_GEEN_FACTUUR = "Geen factuur"

# Outlook's "preset" category colors, per Microsoft Graph's outlookCategory
# documentation: preset4=Green, preset1=Orange, preset12=Gray.
CATEGORY_COLORS = {
    CATEGORY_BASECONE_OK: "preset4",
    CATEGORY_NOG_NAAR_BASECONE: "preset1",
    CATEGORY_GEEN_FACTUUR: "preset12",
}
OUR_CATEGORIES = set(CATEGORY_COLORS)

# Per-mailbox cache of "we've already made sure our categories exist here"
# -- masterCategories essentially never change, so this is cheap to keep
# for the lifetime of the process rather than re-checking on every message.
_ensured_mailboxes: set[str] = set()


def _reset_ensured_cache() -> None:
    """Test-only: lets a test simulate a fresh process."""
    _ensured_mailboxes.clear()


def desired_category(invoice: Invoice) -> str:
    """The ONE category that belongs on this invoice's e-mail right now."""
    if invoice.in_basecone == BaseconeForwardStatus.YES.value:
        return CATEGORY_BASECONE_OK
    if invoice.manually_ignored:
        return CATEGORY_GEEN_FACTUUR
    if is_forwardable(invoice):
        return CATEGORY_NOG_NAAR_BASECONE
    return CATEGORY_GEEN_FACTUUR


def _mailbox_base(mailbox: str) -> str:
    return f"{GRAPH_BASE_URL}/me" if mailbox == "me" else f"{GRAPH_BASE_URL}/users/{mailbox}"


def ensure_categories_exist(mailbox: str) -> None:
    """Makes sure our three categories exist in `mailbox`'s masterCategories
    (creating any missing one with its assigned color) -- best-effort: a
    failure here (see module docstring on why that's a real possibility for
    a shared mailbox) is swallowed, never raised, so the message-labeling
    step below always still gets a chance to run."""
    if mailbox in _ensured_mailboxes:
        return
    try:
        base = _mailbox_base(mailbox)
        payload = _graph_get(f"{base}/outlook/masterCategories")
        existing = {c.get("displayName") for c in payload.get("value", [])}
        for name, color in CATEGORY_COLORS.items():
            if name not in existing:
                _graph_post(f"{base}/outlook/masterCategories", {"displayName": name, "color": color})
    except (GraphApiError, GraphAuthError):
        pass
    finally:
        _ensured_mailboxes.add(mailbox)


def set_invoice_category(invoice: Invoice) -> bool:
    """Sets the one category this invoice's e-mail should have, keeping
    every other category already on the message untouched. Returns False
    (no Graph call made) when there's nothing to address the message by, or
    when it's already labelled correctly; True when a PATCH was actually
    sent. Raises GraphAuthError/GraphApiError on failure -- callers decide
    how to surface that (see app/sync.py, scripts/label_mails.py)."""
    if not invoice.graph_mailbox or not invoice.graph_message_id:
        return False
    target = desired_category(invoice)
    if invoice.graph_category == target:
        return False

    ensure_categories_exist(invoice.graph_mailbox)
    base = _mailbox_base(invoice.graph_mailbox)
    message_url = f"{base}/messages/{invoice.graph_message_id}"
    current = _graph_get(message_url, {"$select": "categories"})
    kept = [c for c in (current.get("categories") or []) if c not in OUR_CATEGORIES]
    _graph_patch(message_url, {"categories": [*kept, target]})
    invoice.graph_category = target
    return True


@dataclass
class LabelResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class LabelSyncState:
    """In-memory, like app/bank_ponto.py's PontoSyncState -- only needed for
    the dashboard's status bar; resets harmlessly on a restart."""
    last_error: str | None = None


_state = LabelSyncState()


def get_state() -> LabelSyncState:
    return _state


def record_label_errors(errors: list[str]) -> None:
    """Authoritative -- call once per full batch (the sync's periodic
    relabel pass, scripts/label_mails.py). Clears the status bar's error
    when a batch runs clean, same as app.bank_ponto.run_ponto_sync does for
    _state.last_error there."""
    _state.last_error = errors[0] if errors else None


def note_single_label_error(error: str) -> None:
    """For a one-off single-invoice labeling attempt (right after a manual
    Basecone forward, say) -- only ever SETS an error, never clears one a
    fuller batch already recorded: one invoice labeling successfully says
    nothing about whether others are still failing."""
    _state.last_error = error


def relabel_invoices(invoices: list[Invoice]) -> LabelResult:
    """Applies set_invoice_category to each invoice, never letting one
    failure stop the rest -- except a GraphAuthError (not logged in /
    Graph not configured at all), which stops the whole batch immediately
    since every subsequent call would fail identically for the same
    reason."""
    result = LabelResult()
    for invoice in invoices:
        try:
            changed = set_invoice_category(invoice)
        except GraphAuthError as exc:
            result.errors.append(str(exc))
            break
        except GraphApiError as exc:
            label = invoice.supplier_name or invoice.attachment_filename or f"factuur {invoice.id}"
            result.errors.append(f"E-mail labelen ({label}): {exc}")
            continue
        if changed:
            result.updated += 1
        else:
            result.skipped += 1
    return result


def invoices_needing_relabel(session: Session, limit: int | None = None) -> list[Invoice]:
    """Every invoice whose stored graph_category doesn't match what
    desired_category would assign today -- the whole reason a periodic
    relabel pass can stay cheap: this is a Python-side filter (the category
    logic isn't expressible as SQL), but set_invoice_category itself would
    reach the identical "nothing to do" conclusion per-invoice anyway; doing
    it here first just avoids importing that per-invoice decision into the
    caller and lets it cap how many it processes in one go."""
    candidates = session.scalars(select(Invoice).where(Invoice.graph_message_id != "")).all()
    needing = [inv for inv in candidates if inv.graph_category != desired_category(inv)]
    if limit is not None:
        needing = needing[:limit]
    return needing


def label_error_hint(error: str | None) -> str | None:
    """Turns a raw e-mail-labeling error into one short, actionable Dutch
    sentence -- mirrors app.bank_ponto.ponto_error_hint."""
    if not error:
        return None
    lowered = error.lower()
    if "403" in lowered or "mail.readwrite" in lowered or "mailboxsettings" in lowered:
        return (
            "Rechten voor e-maillabels ontbreken -- voeg Mail.ReadWrite.Shared "
            "(en MailboxSettings.ReadWrite) toe in Entra en log opnieuw in met "
            "scripts/graph_login.py (zie README)."
        )
    if "niet ingelogd" in lowered or "nog niet ingelogd" in lowered or "graph_login" in lowered:
        return "Nog niet ingelogd bij Microsoft Graph -- draai scripts/graph_login.py."
    return "E-maillabels bijwerken mislukt -- zie de logs voor het volledige foutbericht."
