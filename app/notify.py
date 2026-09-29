"""Push notifications via ntfy (https://ntfy.sh, or a self-hosted ntfy
server) -- so Sacha sees "nieuwe factuur"/"X te laat" on his phone without
opening the dashboard. Off entirely (send_notification is a silent no-op)
until NTFY_TOPIC is configured.

Every push carries Click = DASHBOARD_PUBLIC_URL + "/dashboard#nog-te-betalen"
so tapping the notification jumps straight to the relevant section.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, NotificationLog
from app.payments import days_until_due, is_payable_invoice

logger = logging.getLogger("notify")

# A late/soon-due invoice is worth reminding about within this many days --
# matches the task's "binnen 7 dagen vervalt" rule for the daily summary.
DAILY_SUMMARY_WINDOW_DAYS = 7
DASHBOARD_CLICK_PATH = "/dashboard#nog-te-betalen"


def ntfy_configured() -> bool:
    return bool(settings.ntfy_topic)


def _click_url() -> str:
    return settings.dashboard_public_url.rstrip("/") + DASHBOARD_CLICK_PATH


def send_notification(title: str, message: str, priority: str = "default", tags: str = "", click: str | None = None) -> bool:
    """POSTs to ntfy. Returns whether it actually sent (False when ntfy
    isn't configured, or the request failed -- logged, never raised, since a
    push failure must never abort a sync)."""
    if not ntfy_configured():
        return False
    headers = {
        "Title": title,
        "Priority": priority,
        "Click": click or _click_url(),
    }
    if tags:
        headers["Tags"] = tags
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    url = settings.ntfy_url.rstrip("/") + "/" + settings.ntfy_topic
    try:
        response = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        if response.status_code >= 300:
            logger.warning("ntfy-melding mislukt (%s): %s", response.status_code, response.text)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("ntfy-melding mislukt: %s", exc)
        return False


def send_test_notification() -> bool:
    return send_notification(
        "Testmelding",
        "Dit is een testmelding vanuit de administratie-tool. Als je dit ziet, werkt de koppeling.",
        priority="default",
        tags="test_tube",
    )


def _format_amount(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def notify_new_payable_invoices(session: Session) -> int:
    """Sends one push per still-unnotified payable invoice ("nieuwe factuur:
    Leverancier EUR x, uiterlijk dd-mm") and marks it notified so it's never
    sent twice, however many sync runs it survives. Returns how many were
    sent."""
    if not ntfy_configured():
        return 0
    sent = 0
    candidates = session.scalars(
        select(Invoice).where(Invoice.notified_new_invoice.is_(False))
    )
    for invoice in candidates:
        if not is_payable_invoice(invoice):
            continue
        due_label = invoice.due_date.strftime("%d-%m") if invoice.due_date else "?"
        ok = send_notification(
            "Nieuwe factuur",
            f"Nieuwe factuur: {invoice.supplier_name or 'onbekende leverancier'} "
            f"EUR {_format_amount(invoice.amount_cents)}, uiterlijk {due_label}",
            tags="receipt",
        )
        if ok:
            invoice.notified_new_invoice = True
            sent += 1
    return sent


def _already_sent_today(session: Session, key: str) -> bool:
    today_key = f"{key}:{date.today().isoformat()}"
    return session.scalar(select(NotificationLog).where(NotificationLog.key == today_key)) is not None


def _mark_sent_today(session: Session, key: str) -> None:
    today_key = f"{key}:{date.today().isoformat()}"
    session.add(NotificationLog(key=today_key, sent_at=datetime.utcnow()))


def send_daily_payment_summary(session: Session) -> bool:
    """The 08:00 Europe/Amsterdam daily summary -- only sent when there's
    actually something to act on (something overdue, or due within
    DAILY_SUMMARY_WINDOW_DAYS days); silent otherwise. Idempotent per
    calendar day even if the scheduler fires the job twice."""
    if not ntfy_configured() or _already_sent_today(session, "daily-summary"):
        return False

    today = date.today()
    payable = [inv for inv in session.scalars(select(Invoice)) if is_payable_invoice(inv)]
    due_soon = [inv for inv in payable if days_until_due(inv, today) <= DAILY_SUMMARY_WINDOW_DAYS]
    if not due_soon:
        _mark_sent_today(session, "daily-summary")
        return False

    overdue = [inv for inv in due_soon if days_until_due(inv, today) < 0]
    due_soon.sort(key=lambda inv: inv.due_date)
    total_cents = sum(inv.amount_cents for inv in due_soon)
    top5 = due_soon[:5]
    lines = [
        f"- {inv.supplier_name or '?'} EUR {_format_amount(inv.amount_cents)} "
        f"({'te laat' if days_until_due(inv, today) < 0 else f'nog {days_until_due(inv, today)}d'})"
        for inv in top5
    ]
    message = (
        f"{len(due_soon)} factu(u)r(en), totaal EUR {_format_amount(total_cents)}:\n"
        + "\n".join(lines)
    )
    title = f"{len(overdue)} te laat" if overdue else "Facturen bijna te laat"
    send_notification(title, message, priority="high" if overdue else "default", tags="warning" if overdue else "hourglass")
    _mark_sent_today(session, "daily-summary")
    return True


def send_warning_once_per_day(session: Session, key: str, title: str, message: str) -> bool:
    """Graph-login-expired / Basecone-doorsturen-faalt / bank-sync-faalt
    warnings -- deduped per calendar day so a problem that persists across
    every hourly sync doesn't spam the phone."""
    if not ntfy_configured() or _already_sent_today(session, f"warning:{key}"):
        return False
    send_notification(title, message, priority="high", tags="warning")
    _mark_sent_today(session, f"warning:{key}")
    return True
