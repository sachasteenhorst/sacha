"""FastAPI dashboard: shows which bank transactions do/don't have a
matching invoice yet, and lets you confirm suggested matches, create
manual matches, or mark a transaction/invoice as "no invoice needed"
(e.g. bank costs, private expense).
"""
from __future__ import annotations

import secrets
from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db
from app.models import Invoice, Match, MatchMethod, MatchStatus, Transaction
from app.scheduler import start_scheduler
from app.sync import run_sync

app = FastAPI(title="Fietsenwinkel administratie")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

security = HTTPBasic()


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


templates.env.filters["euros"] = _euros


@app.get("/")
def index(user: str = Depends(require_auth)):
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    unmatched_transactions = list(
        session.scalars(
            select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED).order_by(Transaction.booking_date.desc())
        )
    )
    unmatched_invoices = list(
        session.scalars(
            select(Invoice).where(Invoice.status == MatchStatus.UNMATCHED).order_by(Invoice.received_at.desc())
        )
    )
    suggested_matches = list(
        session.scalars(
            select(Match).where(Match.confirmed.is_(False)).order_by(Match.created_at.desc())
        )
    )
    recent_matches = list(
        session.scalars(
            select(Match)
            .where(Match.confirmed.is_(True))
            .order_by(Match.created_at.desc())
            .limit(25)
        )
    )

    # For the manual-match dropdown.
    all_open_invoices = list(
        session.scalars(
            select(Invoice)
            .where(Invoice.status.in_([MatchStatus.UNMATCHED, MatchStatus.SUGGESTED]))
            .order_by(Invoice.received_at.desc())
        )
    )

    oldest_unmatched_days = None
    if unmatched_transactions:
        oldest = min(t.booking_date for t in unmatched_transactions)
        oldest_unmatched_days = (date.today() - oldest).days

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "unmatched_transactions": unmatched_transactions,
            "unmatched_invoices": unmatched_invoices,
            "suggested_matches": suggested_matches,
            "recent_matches": recent_matches,
            "all_open_invoices": all_open_invoices,
            "oldest_unmatched_days": oldest_unmatched_days,
        },
    )


@app.post("/sync")
def trigger_sync(user: str = Depends(require_auth), session: Session = Depends(get_session)):
    run_sync(session)
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/matches/{match_id}/confirm")
def confirm_match(match_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(404, "Match niet gevonden")
    match.confirmed = True
    match.transaction.status = MatchStatus.MATCHED
    match.invoice.status = MatchStatus.MATCHED
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/matches/{match_id}/reject")
def reject_match(match_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(404, "Match niet gevonden")
    transaction, invoice = match.transaction, match.invoice
    session.delete(match)
    transaction.status = MatchStatus.UNMATCHED
    invoice.status = MatchStatus.UNMATCHED
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/transactions/{transaction_id}/match")
def manual_match(
    transaction_id: int,
    invoice_id: int = Form(...),
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
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/transactions/{transaction_id}/ignore")
def ignore_transaction(transaction_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    transaction.status = MatchStatus.IGNORED
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/invoices/{invoice_id}/ignore")
def ignore_invoice(invoice_id: int, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    invoice.status = MatchStatus.IGNORED
    session.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
