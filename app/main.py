"""FastAPI dashboard: shows which bank transactions do/don't have a
matching invoice yet, and lets you confirm suggested matches, create
manual matches, or mark a transaction/invoice as "no invoice needed"
(e.g. bank costs, private expense).
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sync_state
from app.config import settings
from app.db import get_session, init_db
from app.email_client import INVOICE_DIR, REFRESH_TOKEN_FILE
from app.models import Invoice, Match, MatchMethod, MatchStatus, Transaction
from app.scheduler import start_scheduler
from app.sync import run_sync

app = FastAPI(title="Fietsenwinkel administratie")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

MONTHS_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

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


templates.env.filters["euros"] = _euros
templates.env.filters["dutch_date"] = _dutch_date
templates.env.filters["method_label"] = _method_label


def _dashboard_redirect(q: str = "", maand: str = "") -> RedirectResponse:
    params = {k: v for k, v in {"q": q, "maand": maand}.items() if v}
    url = "/dashboard" + (f"?{urlencode(params)}" if params else "")
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


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


def _filter_matches(items: list[Match], q: str, maand: str) -> list[Match]:
    out = items
    if maand:
        out = [m for m in out if _transaction_month(m.transaction) == maand or _invoice_month(m.invoice) == maand]
    if q:
        out = [
            m for m in out
            if _matches_query(q, texts=[m.transaction.counterparty_name, m.transaction.description, m.transaction.reference], amount_cents=m.transaction.amount_cents)
            or _matches_query(q, texts=[m.invoice.supplier_name, m.invoice.email_subject, m.invoice.invoice_number], amount_cents=m.invoice.amount_cents)
        ]
    return out


def _dot_status(configured: bool, error: str | None) -> str:
    if error:
        return "red"
    if configured:
        return "green"
    return "grey"


@app.get("/")
def index(user: str = Depends(require_auth)):
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    q = request.query_params.get("q", "").strip()
    maand = request.query_params.get("maand", "").strip()

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
            .limit(25)
        )
    )

    # For the manual-match dropdown -- never filtered by q/maand.
    all_open_invoices = list(
        session.scalars(
            select(Invoice)
            .where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED]))
            .order_by(Invoice.received_at.desc())
        )
    )

    oldest_unmatched_days = None
    if unmatched_transactions_all:
        oldest = min(t.booking_date for t in unmatched_transactions_all)
        oldest_unmatched_days = (date.today() - oldest).days

    # -- Counters (always totals, unaffected by the active filter) --
    now_ams = datetime.now(ZoneInfo("Europe/Amsterdam"))
    confirmed_dates = session.scalars(select(Match.created_at).where(Match.confirmed.is_(True))).all()
    matched_this_month = sum(1 for dt in confirmed_dates if dt.year == now_ams.year and dt.month == now_ams.month)

    counters = {
        "te_controleren": len(suggested_matches_all),
        "betalingen_zonder_factuur": len(unmatched_transactions_all),
        "facturen_niet_betaald": len(unmatched_invoices_all),
        "gekoppeld_deze_maand": matched_this_month,
        "maand_label": MONTHS_NL[now_ams.month - 1],
    }

    # -- Status bar --
    email_configured = bool(
        settings.graph_tenant_id and settings.graph_client_id and settings.graph_mailbox
        and os.path.exists(REFRESH_TOKEN_FILE)
    )
    bank_configured = bool(
        settings.basecone_client_id and settings.basecone_client_secret and settings.basecone_administration_id
    )
    last = sync_state.get()
    last_sync_label = None
    if last.ran_at:
        last_sync_label = last.ran_at.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")

    status_bar = {
        "email_status": _dot_status(email_configured, last.email_error),
        "bank_status": _dot_status(bank_configured, last.basecone_error),
        "last_sync_label": last_sync_label,
        "last_sync_new_invoices": last.new_invoices,
        "last_sync_new_transactions": last.new_transactions,
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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "q": q,
            "maand": maand,
            "month_options": month_options,
            "counters": counters,
            "status_bar": status_bar,
            "unmatched_transactions": _filter_transactions(unmatched_transactions_all, q, maand),
            "unmatched_invoices": _filter_invoices(unmatched_invoices_all, q, maand),
            "suggested_matches": _filter_matches(suggested_matches_all, q, maand),
            "recent_matches": _filter_matches(recent_matches_all, q, maand),
            "all_open_invoices": all_open_invoices,
            "oldest_unmatched_days": oldest_unmatched_days,
        },
    )


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


@app.post("/matches/{match_id}/confirm")
def confirm_match(
    match_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(404, "Match niet gevonden")
    match.confirmed = True
    match.transaction.status = MatchStatus.MATCHED
    match.invoice.status = MatchStatus.MATCHED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/matches/{match_id}/reject")
def reject_match(
    match_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(404, "Match niet gevonden")
    transaction, invoice = match.transaction, match.invoice
    session.delete(match)
    transaction.status = MatchStatus.UNMATCHED
    invoice.status = MatchStatus.UNMATCHED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/transactions/{transaction_id}/match")
def manual_match(
    transaction_id: int,
    invoice_id: int = Form(...),
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    invoice = session.get(Invoice, invoice_id)
    if transaction is None or invoice is None:
        raise HTTPException(404, "Transactie of factuur niet gevonden")

    for m in list(transaction.matches):
        if not m.confirmed:
            session.delete(m)

    match = Match(
        transaction_id=transaction.id,
        invoice_id=invoice.id,
        method=MatchMethod.MANUAL,
        confidence="high",
        confirmed=True,
    )
    session.add(match)
    transaction.status = MatchStatus.MATCHED
    invoice.status = MatchStatus.MATCHED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/transactions/{transaction_id}/ignore")
def ignore_transaction(
    transaction_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    transaction.status = MatchStatus.IGNORED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/invoices/{invoice_id}/ignore")
def ignore_invoice(
    invoice_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    invoice.status = MatchStatus.IGNORED
    session.commit()
    return _dashboard_redirect(q, maand)
