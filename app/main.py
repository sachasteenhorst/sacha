"""FastAPI dashboard: shows which bank transactions do/don't have a
matching invoice (or receipt) yet, lets you confirm suggested matches
(possibly covering several documents at once), upload bank statement
exports, and mark items resolved in ways other than a straight match.
"""
from __future__ import annotations

import os
import re
import secrets
import uuid
from collections import OrderedDict
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import sync_state
from app.bank_ponto import PontoError, get_state as get_ponto_state
from app.bank_ponto import ponto_configured, ponto_error_hint, run_manual_ponto_refresh
from app.basecone_backlog import backlog_candidates, get_state as get_backlog_state, start_backlog_send
from app.mail_filing import filing_error_hint
from app.mail_filing import get_state as get_filing_state
from app.mail_labels import get_state as get_label_state
from app.mail_labels import label_error_hint
from app.basecone_forward import basecone_status_label, is_confident, is_forwardable, send_invoice_to_basecone
from app.config import settings
from app.cyclesoftware_upload import CycleSoftwareImportError, import_sales_invoices
from app.db import get_session, init_db
from app.email_client import INVOICE_DIR, REFRESH_TOKEN_FILE, GraphApiError, GraphAuthError
from app.matcher import _transaction_direction, run_matching
from app.models import (
    BaseconeForwardStatus,
    Invoice,
    LearnedIncassoSupplier,
    Match,
    MatchMethod,
    MatchStatus,
    Rule,
    RuleAction,
    Transaction,
)
from app.notify import send_test_notification
from app.payments import days_until_due, get_payable_invoices, learn_incasso_supplier, mark_paid
from app.rules import apply_rules
from app.scheduler import start_scheduler
from app.sync import import_bank_file, run_sync
from app.vandaag import VandaagOverzicht
from app.vraagposten import build_vraagposten

app = FastAPI(title="Fietsenwinkel administratie")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

MONTHS_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

METHOD_LABELS = {
    MatchMethod.REFERENCE: "factuurnummer",
    MatchMethod.AMOUNT_DATE: "bedrag + datum",
    MatchMethod.MANUAL: "handmatig",
}

AMOUNT_QUERY_RE = re.compile(r"^-?\d+([.,]\d{1,2})?$")

INVOICE_DIR_ABS = os.path.abspath(INVOICE_DIR)


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    correct_username = secrets.compare_digest(credentials.username, settings.dashboard_username)
    correct_password = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Onjuiste gebruikersnaam of wachtwoord",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/logout")
def logout():
    """No real server-side session to end (HTTP Basic Auth) -- returning a
    fresh 401 is the standard trick to make the browser forget the cached
    credentials, so the sidebar's "Uitloggen" link actually does something."""
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Uitgelogd",
        headers={"WWW-Authenticate": "Basic"},
    )


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    start_scheduler()


def _euros(cents: int | None) -> str:
    if cents is None:
        return "?"
    sign = "-" if cents < 0 else ""
    return f"{sign}€{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dutch_date(value) -> str:
    if value is None:
        return "?"
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d-%m-%Y")


def _method_label(method: MatchMethod) -> str:
    return METHOD_LABELS.get(method, getattr(method, "value", str(method)))


RULE_ACTION_LABELS = {
    RuleAction.NO_INVOICE_NEEDED.value: "geen factuur nodig",
    RuleAction.REVENUE.value: "omzet",
}


def _rule_action_label(action: str) -> str:
    return RULE_ACTION_LABELS.get(action, action)


templates.env.filters["euros"] = _euros
templates.env.filters["dutch_date"] = _dutch_date
templates.env.filters["method_label"] = _method_label
templates.env.filters["transaction_direction"] = _transaction_direction
templates.env.filters["rule_action_label"] = _rule_action_label
templates.env.filters["basecone_status_label"] = basecone_status_label
templates.env.filters["days_until_due"] = days_until_due


PAGE_SIZE = 50


def _paginate(items: list, page_param: str, request: Request) -> tuple[list, dict]:
    """Slices a long worklist to PAGE_SIZE items -- these lists only ever
    grow (every unresolved bank transaction/invoice stays on it until
    handled), so without this a backlog of hundreds renders every single
    row's HTML on every request. Returns (page_items, pagination) where
    pagination carries what the template needs to render prev/next links
    without losing the OTHER list's page or the active q/maand filter."""
    try:
        page = max(1, int(request.query_params.get(page_param, "1")))
    except ValueError:
        page = 1
    total = len(items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * PAGE_SIZE
    pagination = {
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "param": page_param,
    }
    return items[start:start + PAGE_SIZE], pagination


def _dashboard_redirect(q: str = "", maand: str = "", anchor: str | None = None, **extra: str) -> RedirectResponse:
    params = {k: v for k, v in {"q": q, "maand": maand, **extra}.items() if v}
    url = "/dashboard" + (f"?{urlencode(params)}" if params else "")
    if anchor:
        url += anchor
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _is_ajax(request: Request) -> bool:
    """Set by static/app.js on every list-action form submit it intercepts
    -- lets a mutating route skip the redirect (and the full-page reload
    that comes with it, which is what used to throw you back to the top of
    the page) and just confirm success instead."""
    return request.headers.get("x-requested-with") == "fetch"


def _next_anchor(ids: list[int], current_id: int, prefix: str, section_id: str) -> str:
    """Where a list-action's redirect should land: the id of the row right
    after the one just acted on (so clicking an action and landing back in
    the same spot, ready for the next one, works even with JavaScript
    turned off) -- or the section itself if this was the last row, or the
    row was already gone from the list by the time this ran."""
    try:
        idx = ids.index(current_id)
    except ValueError:
        return f"#{section_id}"
    if idx + 1 < len(ids):
        return f"#row-{prefix}-{ids[idx + 1]}"
    return f"#{section_id}"


def _action_response(
    request: Request, q: str = "", maand: str = "", *, anchor: str | None = None, **extra: str
):
    """What every list-row action route returns: a tiny JSON ack for the
    JS-driven path (see static/app.js), or the normal redirect-with-anchor
    otherwise -- same anchor-to-the-next-row behaviour either way, so this
    degrades gracefully with JavaScript off."""
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return _dashboard_redirect(q, maand, anchor=anchor, **extra)


def _parse_amount_query(q: str) -> int | None:
    q = q.strip()
    if not AMOUNT_QUERY_RE.match(q):
        return None
    return round(float(q.replace(",", ".")) * 100)


def _matches_query(q: str, *, texts: list[str], amount_cents: int | None) -> bool:
    q_lower = q.lower()
    if any(q_lower in (t or "").lower() for t in texts):
        return True
    target = _parse_amount_query(q)
    if target is not None and amount_cents is not None:
        return abs(abs(amount_cents) - target) <= 1
    return False


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _transaction_month(t: Transaction) -> str:
    return _month_key(t.booking_date)


def _invoice_month(inv: Invoice) -> str:
    eff = inv.invoice_date or inv.received_at.date()
    return _month_key(eff)


def _filter_transactions(items: list[Transaction], q: str, maand: str) -> list[Transaction]:
    out = items
    if maand:
        out = [t for t in out if _transaction_month(t) == maand]
    if q:
        out = [
            t for t in out
            if _matches_query(q, texts=[t.counterparty_name, t.description, t.reference], amount_cents=t.amount_cents)
        ]
    return out


def _filter_invoices(items: list[Invoice], q: str, maand: str) -> list[Invoice]:
    out = items
    if maand:
        out = [i for i in out if _invoice_month(i) == maand]
    if q:
        out = [
            i for i in out
            if _matches_query(q, texts=[i.supplier_name, i.email_subject, i.invoice_number], amount_cents=i.amount_cents)
        ]
    return out


def _match_matches_query(m: Match, q: str) -> bool:
    return _matches_query(
        q,
        texts=[m.transaction.counterparty_name, m.transaction.description, m.transaction.reference],
        amount_cents=m.transaction.amount_cents,
    ) or _matches_query(
        q, texts=[m.invoice.supplier_name, m.invoice.email_subject, m.invoice.invoice_number], amount_cents=m.invoice.amount_cents
    )


def _group_and_filter_matches(matches: list[Match], q: str, maand: str) -> list[list[Match]]:
    """Groups Match rows by group_id (one payment can cover several
    documents) and keeps a group if ANY of its rows satisfy the filter --
    a group is one decision, so it's never shown half-filtered."""
    grouped: "OrderedDict[str, list[Match]]" = OrderedDict()
    for m in matches:
        grouped.setdefault(m.group_id, []).append(m)

    result = []
    for group in grouped.values():
        if maand and not any(_transaction_month(m.transaction) == maand or _invoice_month(m.invoice) == maand for m in group):
            continue
        if q and not any(_match_matches_query(m, q) for m in group):
            continue
        result.append(group)
    return result


_RESOLVED_STATUSES = {
    MatchStatus.MATCHED,
    MatchStatus.RULE_HANDLED,
    MatchStatus.RECEIPT_ELSEWHERE,
    MatchStatus.IGNORED,
}


def _monthly_progress(session: Session) -> list[dict]:
    """Per calendar month: what percentage of that month's transactions are
    "afgehandeld" (matched, rule-handled, receipt filed in Basecone, or
    marked no-invoice-needed) -- so Sacha can see progress without having to
    count rows by hand."""
    buckets: "OrderedDict[str, dict[str, int]]" = OrderedDict()
    rows = session.execute(select(Transaction.booking_date, Transaction.status)).all()
    for booking_date, tx_status in rows:
        key = _month_key(booking_date)
        bucket = buckets.setdefault(key, {"total": 0, "resolved": 0})
        bucket["total"] += 1
        if tx_status in _RESOLVED_STATUSES:
            bucket["resolved"] += 1

    result = []
    for key in sorted(buckets.keys(), reverse=True):
        bucket = buckets[key]
        year, month = (int(part) for part in key.split("-"))
        pct = round(100 * bucket["resolved"] / bucket["total"]) if bucket["total"] else 0
        result.append(
            {
                "_key": key,
                "label": f"{MONTHS_NL[month - 1]} {year}",
                "total": bucket["total"],
                "resolved": bucket["resolved"],
                "pct": pct,
            }
        )
    return result


def _relabel_invoice_best_effort(invoice: Invoice) -> None:
    """Best-effort Outlook-category update after a manual dashboard action
    that changes an invoice's desired label (Negeren/Al betaald/Loopt via
    incasso/Koppelen/Klopt/Klopt niet) -- mirrors
    app.basecone_forward._relabel_after_basecone_change. Never raises: a
    label-permission problem must never block the dashboard action that
    triggered it, only show up in the status bar."""
    from app.mail_labels import GraphApiError, GraphAuthError, note_single_label_error, set_invoice_category

    try:
        set_invoice_category(invoice)
    except (GraphAuthError, GraphApiError) as exc:
        note_single_label_error(str(exc))


def _relabel_invoices_best_effort(invoices: list[Invoice]) -> None:
    """Same as _relabel_invoice_best_effort, for every invoice a single
    dashboard action affects at once (a match group can cover several
    documents; "Loopt via incasso" reclassifies a supplier's whole
    backlog)."""
    for invoice in invoices:
        _relabel_invoice_best_effort(invoice)


def _dot_status(configured: bool, error: str | None) -> str:
    if error:
        return "red"
    if configured:
        return "green"
    return "grey"


def _greeting(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        return "Goedemorgen"
    if hour < 18:
        return "Goedemiddag"
    return "Goedenavond"


MONTH_CLOSING_STEP_LABELS = [
    "Afschrift binnen",
    "Transacties gekoppeld",
    "Facturen naar Basecone",
    "Klaar voor boekhouder",
]


def _month_closing_progress(monthly_progress: list[dict], now_ams: datetime, niet_naar_basecone_count: int) -> list[dict]:
    """A simple, visible checklist for "is deze maand klaar voor de
    boekhouder" -- not a precise accounting close, just the four things
    this tool itself tracks, in the order they naturally happen. Step 3/4
    deliberately look at the GLOBAL "niet naar Basecone" count rather than
    only this month's -- a document from an earlier month still blocks
    "klaar voor de boekhouder" just as much as one from the current month."""
    this_month_key = f"{now_ams.year:04d}-{now_ams.month:02d}"
    this_month = next((m for m in monthly_progress if m.get("_key") == this_month_key), None)
    statement_in = this_month is not None and this_month["total"] > 0
    all_linked = this_month is not None and this_month["pct"] == 100
    basecone_done = niet_naar_basecone_count == 0
    done_flags = [statement_in, statement_in and all_linked, basecone_done, statement_in and all_linked and basecone_done]
    steps = []
    current_set = False
    for label, done in zip(MONTH_CLOSING_STEP_LABELS, done_flags):
        is_current = not done and not current_set
        if is_current:
            current_set = True
        steps.append({"label": label, "done": done, "current": is_current})
    if not current_set:
        steps[-1]["current"] = True
    return steps


def _client_ip(request: Request) -> str:
    """The real end user's IP behind the reverse proxy (Caddy sets
    X-Forwarded-For) -- needed for a manual Ponto synchronization request,
    which Ponto only allows while attached to the actual user's own IP (see
    app/bank_ponto.py's module docstring). Falls back to the direct
    connection's address when there's no proxy in front (e.g. local dev)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.get("/")
def index(user: str = Depends(require_auth)):
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    q = request.query_params.get("q", "").strip()
    maand = request.query_params.get("maand", "").strip()
    upload_new = request.query_params.get("upload_new")
    upload_skipped = request.query_params.get("upload_skipped")
    upload_error = request.query_params.get("upload_error")

    unmatched_transactions_all = list(
        session.scalars(
            select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED).order_by(Transaction.booking_date.desc())
        )
    )
    unmatched_invoices_all = list(
        session.scalars(
            select(Invoice).where(Invoice.status == MatchStatus.UNMATCHED).order_by(Invoice.received_at.desc())
        )
    )
    suggested_matches_all = list(
        session.scalars(
            select(Match).where(Match.confirmed.is_(False)).order_by(Match.created_at.desc())
        )
    )
    recent_matches_all = list(
        session.scalars(
            select(Match)
            .where(Match.confirmed.is_(True))
            .order_by(Match.created_at.desc())
            .limit(60)
        )
    )
    rule_handled_all = list(
        session.scalars(
            select(Transaction).where(Transaction.status == MatchStatus.RULE_HANDLED).order_by(Transaction.booking_date.desc())
        )
    )

    # -- Basecone forward status: any invoice/specification not yet sent,
    # split into "confident enough to send" vs "check first" (see
    # app/basecone_forward.py's is_confident/is_forwardable). Never filtered
    # by q/maand -- this is its own worklist, not part of the search.
    not_yet_in_basecone = [
        inv for inv in session.scalars(
            select(Invoice).where(Invoice.in_basecone != BaseconeForwardStatus.YES.value).order_by(Invoice.received_at.asc())
        )
        if is_forwardable(inv)
    ]
    basecone_todo = [inv for inv in not_yet_in_basecone if is_confident(inv)]
    basecone_review = [inv for inv in not_yet_in_basecone if not is_confident(inv)]

    # -- Geadresseerde-check: never filtered by q/maand, its own worklist --
    addressee_review_all = list(
        session.scalars(
            select(Invoice)
            .where(Invoice.addressee_status.in_(("not_for_us", "unknown")), Invoice.addressee_reviewed.is_(False))
            .order_by(Invoice.received_at.desc())
        )
    )

    oldest_unmatched_days = None
    if unmatched_transactions_all:
        oldest = min(t.booking_date for t in unmatched_transactions_all)
        oldest_unmatched_days = (date.today() - oldest).days

    # -- Nog te betalen: never filtered by q/maand, its own worklist --
    te_betalen = get_payable_invoices(session)

    # -- "Vandaag voor jou": reuses the lists already fetched above, so this
    # costs no extra queries -- see app/vandaag.py for why these three and
    # nothing else.
    vandaag = VandaagOverzicht(
        facturen_te_betalen=te_betalen,
        banktransacties_zonder_factuur=unmatched_transactions_all,
        niet_naar_basecone=not_yet_in_basecone,
    )

    # -- Counters (always totals, unaffected by the active filter) --
    now_ams = datetime.now(ZoneInfo("Europe/Amsterdam"))
    confirmed_dates = session.scalars(select(Match.created_at).where(Match.confirmed.is_(True))).all()
    matched_this_month = sum(1 for dt in confirmed_dates if dt.year == now_ams.year and dt.month == now_ams.month)

    counters = {
        "te_controleren": len({m.group_id for m in suggested_matches_all}),
        "betalingen_zonder_factuur": len(unmatched_transactions_all),
        "facturen_niet_betaald": len(unmatched_invoices_all),
        "nog_te_betalen": len(te_betalen),
        "gekoppeld_deze_maand": matched_this_month,
        "automatisch_afgehandeld": len(rule_handled_all),
        "niet_naar_basecone": len(basecone_todo),
        "niet_aan_ons_gericht": len(addressee_review_all),
        "maand_label": MONTHS_NL[now_ams.month - 1],
    }

    # -- Status bar --
    email_configured = bool(
        settings.graph_tenant_id and settings.graph_client_id and settings.graph_mailbox
        and os.path.exists(REFRESH_TOKEN_FILE)
    )
    last = sync_state.get()
    last_sync_label = None
    if last.ran_at:
        last_sync_label = last.ran_at.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")

    transaction_count = session.scalar(select(func.count()).select_from(Transaction)) or 0
    last_upload_at = session.scalar(select(func.max(Transaction.created_at)))
    latest_statement_date = session.scalar(select(func.max(Transaction.booking_date)))
    bank_error = last.basecone_error if settings.basecone_enabled else None

    ponto_state = get_ponto_state()
    ponto_synced_label = (
        ponto_state.last_ponto_synchronized_at.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")
        if ponto_state.last_ponto_synchronized_at else None
    )
    label_state = get_label_state()
    filing_state = get_filing_state()

    status_bar = {
        "email_status": _dot_status(email_configured, last.email_error),
        "last_sync_label": last_sync_label,
        "last_sync_new_invoices": last.new_invoices,
        "bank_status": _dot_status(transaction_count > 0, bank_error),
        "last_upload_label": (
            last_upload_at.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")
            if last_upload_at else None
        ),
        "latest_statement_label": _dutch_date(latest_statement_date) if latest_statement_date else None,
        "ponto_enabled": ponto_configured(),
        "ponto_status": _dot_status(ponto_state.last_read_at is not None, ponto_state.last_error),
        "ponto_synced_label": ponto_synced_label,
        "ponto_error_hint": ponto_error_hint(ponto_state.last_error),
        "label_status": _dot_status(email_configured, label_state.last_error),
        "label_error_hint": label_error_hint(label_state.last_error),
        "filing_status": _dot_status(email_configured, filing_state.last_error),
        "filing_error_hint": filing_error_hint(filing_state.last_error),
    }

    # -- Month dropdown options --
    month_set: set[tuple[int, int]] = set()
    for (d,) in session.execute(select(Transaction.booking_date)):
        month_set.add((d.year, d.month))
    for inv_date, received_at in session.execute(select(Invoice.invoice_date, Invoice.received_at)):
        eff = inv_date or (received_at.date() if received_at else None)
        if eff:
            month_set.add((eff.year, eff.month))
    month_options = [
        (f"{y:04d}-{m:02d}", f"{MONTHS_NL[m - 1]} {y}")
        for (y, m) in sorted(month_set, reverse=True)
    ]

    unmatched_transactions_page, tx_pagination = _paginate(
        _filter_transactions(unmatched_transactions_all, q, maand), "tx_page", request
    )
    unmatched_invoices_page, inv_pagination = _paginate(
        _filter_invoices(unmatched_invoices_all, q, maand), "inv_page", request
    )
    te_betalen_page, betalen_pagination = _paginate(te_betalen, "betalen_page", request)
    basecone_todo_page, basecone_pagination = _paginate(basecone_todo, "basecone_page", request)

    # -- "Vandaag voor jou" header: greeting, 4 tiles, month-closing steps --
    monthly_progress = _monthly_progress(session)
    greeting = f"{_greeting(now_ams)}, Sacha."
    now_label = f"{DAYS_NL[now_ams.weekday()]} {now_ams.day} {MONTHS_NL[now_ams.month - 1]}".upper()
    today = date.today()
    eerstvolgende_vervaldatum = te_betalen[0].due_date if te_betalen else None
    eerstvolgende_dagen = days_until_due(te_betalen[0], today) if te_betalen else None
    tiles = {
        "zelf_betalen_aantal": len(te_betalen),
        "zelf_betalen_bedrag": sum(inv.amount_cents or 0 for inv in te_betalen),
        "zonder_bon_aantal": len(unmatched_transactions_all),
        "niet_naar_basecone_aantal": len(not_yet_in_basecone),
        "eerstvolgende_vervaldatum": eerstvolgende_vervaldatum,
        "eerstvolgende_dagen": eerstvolgende_dagen,
    }
    month_closing_steps = _month_closing_progress(monthly_progress, now_ams, len(not_yet_in_basecone))

    # -- "Actie nodig": the same prioritized worklist as the Vraagposten
    # page (oldest/biggest first), just the top handful -- see
    # app/vraagposten.py for how each item's suggested action is picked.
    actie_nodig = build_vraagposten(session)[:8]

    # -- "Binnenkort te betalen": a 14-day agenda, already sorted by
    # due_date via get_payable_invoices.
    binnenkort_te_betalen = [inv for inv in te_betalen if 0 <= days_until_due(inv, today) <= 14]

    if last.email_error:
        email_line = f"E-mail: {last.email_error}"
    elif not email_configured:
        email_line = "E-mail: nog niet ingesteld"
    elif last_sync_label:
        email_line = f"E-mail: laatst opgehaald {last_sync_label}"
    else:
        email_line = "E-mail: nog niet opgehaald"

    if bank_error:
        bank_line = f"Bank: {bank_error}"
    elif status_bar["latest_statement_label"]:
        bank_line = f"Bank: {transaction_count} transacties, laatste afschrift {status_bar['latest_statement_label']}"
    else:
        bank_line = "Bank: nog geen afschrift geupload"

    status_lines = [
        {"ok": not last.email_error, "text": email_line},
        {"ok": bank_error is None, "text": bank_line},
    ]
    if status_bar["ponto_enabled"]:
        status_lines.append({"ok": status_bar["ponto_error_hint"] is None, "text": (
            f"Ponto: laatst gesynchroniseerd {ponto_synced_label}" if ponto_synced_label and status_bar["ponto_error_hint"] is None
            else status_bar["ponto_error_hint"] or "Ponto: nog niet gesynchroniseerd"
        )})

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "active_nav": "vandaag",
            "q": q,
            "maand": maand,
            "upload_new": upload_new,
            "upload_skipped": upload_skipped,
            "upload_error": upload_error,
            "month_options": month_options,
            "counters": counters,
            "status_bar": status_bar,
            "unmatched_transactions": unmatched_transactions_page,
            "tx_pagination": tx_pagination,
            "unmatched_invoices": unmatched_invoices_page,
            "inv_pagination": inv_pagination,
            "suggested_groups": _group_and_filter_matches(suggested_matches_all, q, maand),
            "recent_groups": _group_and_filter_matches(recent_matches_all, q, maand),
            "oldest_unmatched_days": oldest_unmatched_days,
            "rule_handled_transactions": _filter_transactions(rule_handled_all, q, maand),
            "monthly_progress": monthly_progress,
            "basecone_todo": basecone_todo_page,
            "basecone_pagination": basecone_pagination,
            "basecone_review": basecone_review,
            "addressee_review": addressee_review_all,
            "backlog_count": len(backlog_candidates(session)),
            "forward_error": request.query_params.get("forward_error"),
            "forward_ok": request.query_params.get("forward_ok"),
            "te_betalen": te_betalen_page,
            "betalen_pagination": betalen_pagination,
            "vandaag": vandaag,
            "greeting": greeting,
            "now_label": now_label,
            "tiles": tiles,
            "month_closing_steps": month_closing_steps,
            "actie_nodig": actie_nodig,
            "binnenkort_te_betalen": binnenkort_te_betalen,
            "status_lines": status_lines,
            "cs_upload_new": request.query_params.get("cs_upload_new"),
            "cs_upload_matched": request.query_params.get("cs_upload_matched"),
            "cs_upload_error": request.query_params.get("cs_upload_error"),
        },
    )


INVOICE_SEARCH_LIMIT = 20
# "In de buurt" voor het koppel-zoekveld -- ruim genoeg voor een kleine
# betalingskorting, krap genoeg om niet elke factuur van elk bedrag te tonen.
INVOICE_SEARCH_AMOUNT_TOLERANCE_CENTS = 2000


@app.get("/invoices/search-options")
def search_invoice_options(
    q: str = "",
    amount_cents: int | None = None,
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    """Backs the dashboard's koppel-zoekveld: used to be a single <select>
    with EVERY open factuur as an <option>, rendered once per onopgeloste
    transactie -- 565 rijen x ~4000 facturen = 2,3 miljoen <option>-tags en
    een 235MB dashboardpagina op productie. Now it's a small on-demand
    search instead: typed tekst matcht op leverancier/factuurnummer, geen
    tekst valt terug op facturen met een bedrag dicht bij de transactie
    (INVOICE_SEARCH_AMOUNT_TOLERANCE_CENTS), en zonder allebei gewoon de
    meest recente open facturen -- altijd afgekapt op INVOICE_SEARCH_LIMIT."""
    base = select(Invoice).where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED]))
    q = q.strip()
    if q:
        like = f"%{q}%"
        candidates = list(
            session.scalars(
                base.where(or_(Invoice.supplier_name.ilike(like), Invoice.invoice_number.ilike(like)))
                .order_by(Invoice.received_at.desc())
                .limit(INVOICE_SEARCH_LIMIT)
            )
        )
    elif amount_cents is not None:
        low, high = amount_cents - INVOICE_SEARCH_AMOUNT_TOLERANCE_CENTS, amount_cents + INVOICE_SEARCH_AMOUNT_TOLERANCE_CENTS
        candidates = list(session.scalars(base.where(Invoice.amount_cents.between(low, high))))
        candidates.sort(key=lambda inv: abs((inv.amount_cents or 0) - amount_cents))
        candidates = candidates[:INVOICE_SEARCH_LIMIT]
    else:
        candidates = list(session.scalars(base.order_by(Invoice.received_at.desc()).limit(INVOICE_SEARCH_LIMIT)))

    return [
        {"id": inv.id, "label": f"{inv.supplier_name or '?'} – {inv.invoice_number or 'geen nr'} ({_euros(inv.amount_cents)})"}
        for inv in candidates
    ]


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or not invoice.pdf_path:
        raise HTTPException(404, "Factuur (PDF) niet gevonden")

    resolved = os.path.abspath(invoice.pdf_path)
    if resolved != INVOICE_DIR_ABS and not resolved.startswith(INVOICE_DIR_ABS + os.sep):
        raise HTTPException(403, "Ongeldig bestandspad")
    if not os.path.isfile(resolved):
        raise HTTPException(404, "PDF-bestand niet gevonden op schijf")

    return FileResponse(
        resolved,
        media_type="application/pdf",
        filename=invoice.attachment_filename,
        content_disposition_type="inline",
    )


@app.post("/sync")
def trigger_sync(
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    run_sync(session)
    return _dashboard_redirect(q, maand)


@app.post("/bank/upload")
async def upload_bank_file(
    file: UploadFile,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    content = await file.read()
    result = import_bank_file(session, file.filename or "upload", content)
    if result.errors:
        return _dashboard_redirect(q, maand, upload_error=result.errors[0])
    return _dashboard_redirect(q, maand, upload_new=str(result.new_transactions), upload_skipped=str(result.skipped))


@app.post("/bank/ponto/refresh")
def refresh_ponto(
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    """The ONLY code path allowed to trigger a Ponto synchronization (see
    app/bank_ponto.py's module docstring) -- Ponto only permits a manual
    synchronization request while attached to the real end user's own IP,
    so this must only ever run from an actual dashboard click, never a
    background job."""
    try:
        result = run_manual_ponto_refresh(session, _client_ip(request))
    except PontoError as exc:
        session.rollback()
        return _dashboard_redirect(q, maand, upload_error=ponto_error_hint(str(exc)) or str(exc))
    except Exception as exc:
        # Same safety net as the scheduler's _ponto_job -- an unexpected bug
        # here must show up as a clear banner, never a bare 500.
        session.rollback()
        return _dashboard_redirect(q, maand, upload_error=ponto_error_hint(str(exc)) or str(exc))
    session.commit()
    if result.errors:
        return _dashboard_redirect(q, maand, upload_error=result.errors[0])
    return _dashboard_redirect(q, maand, upload_new=str(result.new_transactions))


def _suggested_group_ids(session: Session, q: str, maand: str) -> list[str]:
    suggested_matches_all = list(
        session.scalars(select(Match).where(Match.confirmed.is_(False)).order_by(Match.created_at.desc()))
    )
    return [group[0].group_id for group in _group_and_filter_matches(suggested_matches_all, q, maand)]


@app.post("/match-groups/{group_id}/confirm")
def confirm_match_group(
    group_id: str,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    matches = list(session.scalars(select(Match).where(Match.group_id == group_id)))
    if not matches:
        raise HTTPException(404, "Match niet gevonden")
    anchor = _next_anchor(_suggested_group_ids(session, q, maand), group_id, "suggested", "te-controleren")
    for m in matches:
        m.confirmed = True
        m.invoice.status = MatchStatus.MATCHED
    matches[0].transaction.status = MatchStatus.MATCHED
    _relabel_invoices_best_effort([m.invoice for m in matches])
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/match-groups/{group_id}/reject")
def reject_match_group(
    group_id: str,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    matches = list(session.scalars(select(Match).where(Match.group_id == group_id)))
    if not matches:
        raise HTTPException(404, "Match niet gevonden")
    anchor = _next_anchor(_suggested_group_ids(session, q, maand), group_id, "suggested", "te-controleren")
    transaction = matches[0].transaction
    invoices = [m.invoice for m in matches]
    for m in matches:
        m.invoice.status = MatchStatus.UNMATCHED
        session.delete(m)
    transaction.status = MatchStatus.UNMATCHED
    _relabel_invoices_best_effort(invoices)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


def _unmatched_transaction_ids(session: Session, q: str, maand: str) -> list[int]:
    all_unmatched = list(
        session.scalars(
            select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED).order_by(Transaction.booking_date.desc())
        )
    )
    return [t.id for t in _filter_transactions(all_unmatched, q, maand)]


@app.post("/transactions/{transaction_id}/match")
def manual_match(
    transaction_id: int,
    request: Request,
    invoice_ids: list[int] = Form(...),
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    invoices = [session.get(Invoice, invoice_id) for invoice_id in invoice_ids]
    if not invoices or any(inv is None for inv in invoices):
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_unmatched_transaction_ids(session, q, maand), transaction_id, "tx-unmatched", "betalingen-zonder-factuur")

    for m in list(transaction.matches):
        if not m.confirmed:
            m.invoice.status = MatchStatus.UNMATCHED
            session.delete(m)

    group_id = uuid.uuid4().hex[:12]
    for invoice in invoices:
        session.add(
            Match(
                transaction_id=transaction.id,
                invoice_id=invoice.id,
                method=MatchMethod.MANUAL,
                confidence="high",
                confirmed=True,
                group_id=group_id,
            )
        )
        invoice.status = MatchStatus.MATCHED
    transaction.status = MatchStatus.MATCHED
    _relabel_invoices_best_effort(invoices)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/transactions/{transaction_id}/ignore")
def ignore_transaction(
    transaction_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    anchor = _next_anchor(_unmatched_transaction_ids(session, q, maand), transaction_id, "tx-unmatched", "betalingen-zonder-factuur")
    transaction.status = MatchStatus.IGNORED
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/transactions/{transaction_id}/receipt-in-basecone")
def receipt_in_basecone(
    transaction_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    anchor = _next_anchor(_unmatched_transaction_ids(session, q, maand), transaction_id, "tx-unmatched", "betalingen-zonder-factuur")
    transaction.status = MatchStatus.RECEIPT_ELSEWHERE
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


def _unmatched_invoice_ids(session: Session, q: str, maand: str) -> list[int]:
    all_unmatched = list(
        session.scalars(select(Invoice).where(Invoice.status == MatchStatus.UNMATCHED).order_by(Invoice.received_at.desc()))
    )
    return [i.id for i in _filter_invoices(all_unmatched, q, maand)]


def _te_betalen_ids(session: Session) -> list[int]:
    return [inv.id for inv in get_payable_invoices(session)]


@app.post("/invoices/{invoice_id}/ignore")
def ignore_invoice(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_unmatched_invoice_ids(session, q, maand), invoice_id, "inv-unmatched", "facturen-niet-betaald")
    invoice.status = MatchStatus.IGNORED
    invoice.manually_ignored = True
    _relabel_invoice_best_effort(invoice)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/invoices/{invoice_id}/mark-paid")
def mark_invoice_paid(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_te_betalen_ids(session), invoice_id, "te-betalen", "nog-te-betalen")
    mark_paid(invoice)
    _relabel_invoice_best_effort(invoice)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/invoices/{invoice_id}/mark-incasso")
def mark_invoice_incasso(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_te_betalen_ids(session), invoice_id, "te-betalen", "nog-te-betalen")
    changed_invoices = learn_incasso_supplier(session, invoice.supplier_name)
    _relabel_invoices_best_effort(changed_invoices)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/notify/test")
def notify_test(user: str = Depends(require_auth)):
    sent = send_test_notification()
    return RedirectResponse(
        url=f"/regels?notify_test={'ok' if sent else 'error'}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/cyclesoftware/upload")
async def upload_cyclesoftware_export(
    file: UploadFile,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    content = await file.read()
    try:
        result = import_sales_invoices(session, file.filename or "upload", content)
    except CycleSoftwareImportError as exc:
        return _dashboard_redirect(q, maand, cs_upload_error=str(exc))
    session.flush()
    run_matching(session)
    session.commit()
    return _dashboard_redirect(q, maand, cs_upload_new=str(result.new_invoices))


def _rule_handled_ids(session: Session, q: str, maand: str) -> list[int]:
    all_handled = list(
        session.scalars(select(Transaction).where(Transaction.status == MatchStatus.RULE_HANDLED).order_by(Transaction.booking_date.desc()))
    )
    return [t.id for t in _filter_transactions(all_handled, q, maand)]


@app.post("/transactions/{transaction_id}/undo-rule")
def undo_rule(
    transaction_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    anchor = _next_anchor(_rule_handled_ids(session, q, maand), transaction_id, "rule-handled", "automatisch-afgehandeld")
    transaction.status = MatchStatus.UNMATCHED
    transaction.applied_rule_id = None
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.get("/regels")
def rules_page(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    prefill = {
        "counterparty": request.query_params.get("counterparty", ""),
        "iban": request.query_params.get("iban", ""),
        "code": request.query_params.get("code", ""),
        "direction": request.query_params.get("direction", ""),
    }
    rules = list(session.scalars(select(Rule).order_by(Rule.created_at.desc())))
    rule_counts = dict(
        session.execute(
            select(Transaction.applied_rule_id, func.count())
            .where(Transaction.applied_rule_id.is_not(None))
            .group_by(Transaction.applied_rule_id)
        ).all()
    )
    learned_incasso = list(session.scalars(select(LearnedIncassoSupplier).order_by(LearnedIncassoSupplier.supplier_name_lower)))
    return templates.TemplateResponse(
        "rules.html",
        {
            "request": request,
            "user": user,
            "active_nav": "regels",
            "rules": rules,
            "rule_counts": rule_counts,
            "prefill": prefill,
            "notify_test": request.query_params.get("notify_test"),
            "ntfy_configured": bool(settings.ntfy_topic),
            "pay_incasso_suppliers": settings.pay_incasso_suppliers,
            "learned_incasso": learned_incasso,
            "incoming_suppliers": settings.incoming_suppliers,
        },
    )


@app.post("/leveranciers/incasso/{supplier_id}/delete")
def delete_learned_incasso_supplier(
    supplier_id: int,
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    """Removes a supplier from LearnedIncassoSupplier (the "Loopt via
    incasso" dashboard button's persistent list) -- never touches
    PAY_INCASSO_SUPPLIERS (that's an env/deploy-time setting, not a runtime
    one). A supplier removed here is only recognised as incasso again by a
    future "Loopt via incasso" click, or via its own PAY_INCASSO_SUPPLIERS
    entry if it has one."""
    supplier = session.get(LearnedIncassoSupplier, supplier_id)
    if supplier is not None:
        session.delete(supplier)
        session.commit()
    return RedirectResponse(url="/regels", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/regels")
def create_rule(
    name: str = Form(""),
    action: str = Form(...),
    counterparty_contains: str = Form(""),
    counterparty_iban: str = Form(""),
    description_contains: str = Form(""),
    transaction_code: str = Form(""),
    direction: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    rule = Rule(
        name=name.strip() or "Regel",
        action=action,
        counterparty_contains=counterparty_contains.strip(),
        counterparty_iban=counterparty_iban.strip(),
        description_contains=description_contains.strip(),
        transaction_code=transaction_code.strip(),
        direction=direction.strip(),
    )
    session.add(rule)
    session.flush()
    apply_rules(session)
    session.commit()
    return RedirectResponse(url="/regels", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/regels/{rule_id}/toggle")
def toggle_rule(rule_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "Regel niet gevonden")
    rule.enabled = not rule.enabled
    session.commit()
    return RedirectResponse(url="/regels", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/regels/{rule_id}/delete")
def delete_rule(rule_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "Regel niet gevonden")
    for transaction in list(rule.transactions):
        transaction.status = MatchStatus.UNMATCHED
        transaction.applied_rule_id = None
    session.delete(rule)
    session.commit()
    return RedirectResponse(url="/regels", status_code=status.HTTP_303_SEE_OTHER)


def _basecone_pending_ids(session: Session) -> list[int]:
    return [
        inv.id for inv in session.scalars(
            select(Invoice).where(Invoice.in_basecone != BaseconeForwardStatus.YES.value).order_by(Invoice.received_at.asc())
        )
        if is_forwardable(inv)
    ]


@app.post("/invoices/{invoice_id}/forward-to-basecone")
def forward_to_basecone(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    # Both "Niet naar Basecone" (confident) and "Controleer eerst" post to
    # this same route -- the pending list covers both, so this works as the
    # next-anchor regardless of which section the click came from.
    anchor = _next_anchor(_basecone_pending_ids(session), invoice_id, "basecone-pending", "niet-naar-basecone")
    try:
        send_invoice_to_basecone(invoice, method="manual")
    except (GraphAuthError, GraphApiError) as exc:
        session.rollback()
        if _is_ajax(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _dashboard_redirect(q, maand, forward_error=str(exc))
    session.commit()
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return _dashboard_redirect(q, maand, anchor=anchor, forward_ok="1")


@app.post("/invoices/forward-to-basecone-bulk")
def forward_to_basecone_bulk(
    invoice_ids: list[int] = Form(...),
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    sent = 0
    first_error: str | None = None
    for invoice_id in invoice_ids:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            continue
        try:
            send_invoice_to_basecone(invoice, method="manual")
            sent += 1
        except (GraphAuthError, GraphApiError) as exc:
            if first_error is None:
                first_error = str(exc)
    session.commit()
    return _dashboard_redirect(
        q, maand,
        forward_ok=str(sent) if sent else "",
        forward_error=first_error or "",
    )


def _addressee_review_ids(session: Session) -> list[int]:
    return [
        inv.id for inv in session.scalars(
            select(Invoice)
            .where(Invoice.addressee_status.in_(("not_for_us", "unknown")), Invoice.addressee_reviewed.is_(False))
            .order_by(Invoice.received_at.desc())
        )
    ]


@app.post("/invoices/{invoice_id}/addressee-confirm-ours")
def addressee_confirm_ours(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_addressee_review_ids(session), invoice_id, "addressee-review", "niet-aan-ons-gericht")
    invoice.addressee_status = "ok"
    invoice.addressee_reviewed = True
    _relabel_invoice_best_effort(invoice)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.post("/invoices/{invoice_id}/addressee-dismiss")
def addressee_dismiss(
    invoice_id: int,
    request: Request,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    anchor = _next_anchor(_addressee_review_ids(session), invoice_id, "addressee-review", "niet-aan-ons-gericht")
    invoice.addressee_reviewed = True
    _relabel_invoice_best_effort(invoice)
    session.commit()
    return _action_response(request, q, maand, anchor=anchor)


@app.get("/invoices/backlog")
def backlog_page(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    candidates = backlog_candidates(session)
    groups: dict[str, list[Invoice]] = {}
    for inv in candidates:
        groups.setdefault(inv.supplier_name or "Onbekend", []).append(inv)
    supplier_groups = [
        {
            "supplier_name": name,
            "invoices": invoices,
            "count": len(invoices),
            "total_cents": sum(inv.amount_cents or 0 for inv in invoices),
        }
        for name, invoices in sorted(groups.items())
    ]
    return templates.TemplateResponse(
        "backlog.html",
        {
            "request": request,
            "user": user,
            "active_nav": "vandaag",
            "supplier_groups": supplier_groups,
            "total_count": len(candidates),
            "total_cents": sum(inv.amount_cents or 0 for inv in candidates),
            "state": get_backlog_state(),
        },
    )


@app.post("/invoices/backlog/send")
def backlog_send(
    invoice_ids: list[int] = Form(default=[]),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    # Only ever sends invoices that are still genuinely valid backlog
    # candidates right now -- a stale/tampered id in the submitted form
    # never slips through.
    valid_ids = {inv.id for inv in backlog_candidates(session)}
    selected_ids = [invoice_id for invoice_id in invoice_ids if invoice_id in valid_ids]
    start_backlog_send(selected_ids)
    return RedirectResponse(url="/invoices/backlog", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/invoices/backlog/status")
def backlog_status(user: str = Depends(require_auth)):
    state = get_backlog_state()
    return JSONResponse({
        "running": state.running,
        "total": state.total,
        "sent": state.sent,
        "errors": state.errors,
        "done": state.done,
    })


@app.get("/vraagposten")
def vraagposten_page(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    items = build_vraagposten(session)
    return templates.TemplateResponse(
        "vraagposten.html",
        {"request": request, "user": user, "active_nav": "vraagposten", "items": items},
    )


@app.get("/vraagposten/export.csv")
def vraagposten_csv(user: str = Depends(require_auth), session: Session = Depends(get_session)):
    import csv
    import io

    items = build_vraagposten(session)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Leeftijd (dagen)", "Datum", "Bedrag", "Tegenpartij", "Omschrijving", "Voorgestelde actie", "Factuur/leverancier"])
    for item in items:
        if item.transaction is not None:
            row_date = item.transaction.booking_date
            row_amount = item.transaction.amount_cents
            row_counterparty = item.transaction.counterparty_name
            row_description = item.transaction.description
        else:
            row_date = item.invoice.due_date if item.invoice else None
            row_amount = -item.invoice.amount_cents if item.invoice and item.invoice.amount_cents else 0
            row_counterparty = item.invoice.supplier_name if item.invoice else ""
            row_description = ""
        writer.writerow([
            item.age_days,
            row_date.strftime("%d-%m-%Y") if row_date else "",
            f"{row_amount / 100:.2f}".replace(".", ","),
            row_counterparty,
            row_description,
            item.label,
            item.invoice.supplier_name if item.invoice else "",
        ])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vraagposten.csv"},
    )
