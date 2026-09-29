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
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import sync_state
from app.basecone_forward import basecone_status_label, is_confident, is_forwardable, send_invoice_to_basecone
from app.config import settings
from app.db import get_session, init_db
from app.email_client import INVOICE_DIR, REFRESH_TOKEN_FILE, GraphApiError, GraphAuthError
from app.matcher import _transaction_direction
from app.models import BaseconeForwardStatus, Invoice, Match, MatchMethod, MatchStatus, Rule, RuleAction, Transaction
from app.rules import apply_rules
from app.scheduler import start_scheduler
from app.sync import import_bank_file, run_sync
from app.vraagposten import build_vraagposten

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


def _dashboard_redirect(q: str = "", maand: str = "", **extra: str) -> RedirectResponse:
    params = {k: v for k, v in {"q": q, "maand": maand, **extra}.items() if v}
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
                "label": f"{MONTHS_NL[month - 1]} {year}",
                "total": bucket["total"],
                "resolved": bucket["resolved"],
                "pct": pct,
            }
        )
    return result


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
        "te_controleren": len({m.group_id for m in suggested_matches_all}),
        "betalingen_zonder_factuur": len(unmatched_transactions_all),
        "facturen_niet_betaald": len(unmatched_invoices_all),
        "gekoppeld_deze_maand": matched_this_month,
        "automatisch_afgehandeld": len(rule_handled_all),
        "niet_naar_basecone": len(basecone_todo),
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
            "upload_new": upload_new,
            "upload_skipped": upload_skipped,
            "upload_error": upload_error,
            "month_options": month_options,
            "counters": counters,
            "status_bar": status_bar,
            "unmatched_transactions": _filter_transactions(unmatched_transactions_all, q, maand),
            "unmatched_invoices": _filter_invoices(unmatched_invoices_all, q, maand),
            "suggested_groups": _group_and_filter_matches(suggested_matches_all, q, maand),
            "recent_groups": _group_and_filter_matches(recent_matches_all, q, maand),
            "all_open_invoices": all_open_invoices,
            "oldest_unmatched_days": oldest_unmatched_days,
            "rule_handled_transactions": _filter_transactions(rule_handled_all, q, maand),
            "monthly_progress": _monthly_progress(session),
            "basecone_todo": basecone_todo,
            "basecone_review": basecone_review,
            "forward_error": request.query_params.get("forward_error"),
            "forward_ok": request.query_params.get("forward_ok"),
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


@app.post("/match-groups/{group_id}/confirm")
def confirm_match_group(
    group_id: str,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    matches = list(session.scalars(select(Match).where(Match.group_id == group_id)))
    if not matches:
        raise HTTPException(404, "Match niet gevonden")
    for m in matches:
        m.confirmed = True
        m.invoice.status = MatchStatus.MATCHED
    matches[0].transaction.status = MatchStatus.MATCHED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/match-groups/{group_id}/reject")
def reject_match_group(
    group_id: str,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    matches = list(session.scalars(select(Match).where(Match.group_id == group_id)))
    if not matches:
        raise HTTPException(404, "Match niet gevonden")
    transaction = matches[0].transaction
    for m in matches:
        m.invoice.status = MatchStatus.UNMATCHED
        session.delete(m)
    transaction.status = MatchStatus.UNMATCHED
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/transactions/{transaction_id}/match")
def manual_match(
    transaction_id: int,
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


@app.post("/transactions/{transaction_id}/receipt-in-basecone")
def receipt_in_basecone(
    transaction_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    transaction.status = MatchStatus.RECEIPT_ELSEWHERE
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
    invoice.manually_ignored = True
    session.commit()
    return _dashboard_redirect(q, maand)


@app.post("/transactions/{transaction_id}/undo-rule")
def undo_rule(
    transaction_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transactie niet gevonden")
    transaction.status = MatchStatus.UNMATCHED
    transaction.applied_rule_id = None
    session.commit()
    return _dashboard_redirect(q, maand)


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
    return templates.TemplateResponse(
        "rules.html",
        {
            "request": request,
            "rules": rules,
            "rule_counts": rule_counts,
            "prefill": prefill,
        },
    )


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


@app.post("/invoices/{invoice_id}/forward-to-basecone")
def forward_to_basecone(
    invoice_id: int,
    q: str = Form(""),
    maand: str = Form(""),
    user: str = Depends(require_auth),
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Factuur niet gevonden")
    try:
        send_invoice_to_basecone(invoice, method="manual")
    except (GraphAuthError, GraphApiError) as exc:
        session.rollback()
        return _dashboard_redirect(q, maand, forward_error=str(exc))
    session.commit()
    return _dashboard_redirect(q, maand, forward_ok="1")


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


@app.get("/vraagposten")
def vraagposten_page(request: Request, user: str = Depends(require_auth), session: Session = Depends(get_session)):
    items = build_vraagposten(session)
    return templates.TemplateResponse("vraagposten.html", {"request": request, "items": items})


@app.get("/vraagposten/export.csv")
def vraagposten_csv(user: str = Depends(require_auth), session: Session = Depends(get_session)):
    import csv
    import io

    items = build_vraagposten(session)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Leeftijd (dagen)", "Datum", "Bedrag", "Tegenpartij", "Omschrijving", "Voorgestelde actie", "Factuur/leverancier"])
    for item in items:
        writer.writerow([
            item.age_days,
            item.transaction.booking_date.strftime("%d-%m-%Y"),
            f"{item.transaction.amount_cents / 100:.2f}".replace(".", ","),
            item.transaction.counterparty_name,
            item.transaction.description,
            item.label,
            item.invoice.supplier_name if item.invoice else "",
        ])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vraagposten.csv"},
    )
