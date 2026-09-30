"""Push notifications via ntfy (https://ntfy.sh, or a self-hosted ntfy
server) -- so Sacha sees what he needs to do on his phone without opening
the dashboard. Off entirely (send_notification is a silent no-op) until
NTFY_TOPIC is configured.

Kept deliberately short and jargon-free: a title with just the supplier and
amount, a message that says only what to do and by when -- never a
factuurnummer, an internal id, or a long omschrijving. Urgency (overdue, or
due within URGENT_DUE_WITHIN_DAYS days) gets ntfy's "urgent" priority and a
red-flag tag so it actually stands out on the phone; everything else is
"default" priority with a plain money-bag tag.

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
from app.payments import days_until_due, is_duplicate_of_existing, is_payable_invoice
from app.vandaag import build_vandaag_overzicht

logger = logging.getLogger("notify")

# An invoice this close to (or past) its due date is worth flagging as
# urgent -- everything further out just gets a plain, non-intrusive push.
URGENT_DUE_WITHIN_DAYS = 3
DASHBOARD_CLICK_PATH = "/dashboard#nog-te-betalen"
# Safety cap: a bulk backlog of new invoices (e.g. right after an upgrade,
# or a mailbox that was unreachable for a while) sends at most this many
# INDIVIDUAL pushes per sync -- anything beyond that is rolled into one
# summary push instead of flooding the phone with one notification each.
MAX_NEW_INVOICE_PUSHES_PER_SYNC = 5


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


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _urgency(days: int) -> tuple[str, str]:
    """ntfy priority+tag for a given "days until due" number -- shared by
    every push that's about a specific due date."""
    if days <= URGENT_DUE_WITHIN_DAYS:
        return "urgent", "rotating_light"
    return "default", "moneybag"


def _due_message(invoice: Invoice, today: date) -> str:
    """Plain-language sentence saying only what to do and by when -- no
    factuurnummer, no internal details."""
    if invoice.due_date is None:
        return "Vervaldatum onbekend -- controleer de factuur."
    days = days_until_due(invoice, today)
    due = invoice.due_date.strftime("%d-%m")
    if days < 0:
        dagen = abs(days)
        return f"Te laat sinds {due} ({dagen} {_plural(dagen, 'dag', 'dagen')})."
    if days == 0:
        return f"Betaal vandaag ({due})."
    return f"Betaal voor {due} (nog {days} {_plural(days, 'dag', 'dagen')})."


def notify_new_payable_invoices(session: Session) -> int:
    """Sends a short "Betalen: Leverancier EUR x" push per still-unnotified
    payable invoice and marks it notified so it's never sent twice, however
    many sync runs it survives. A supplier's reminder e-mail of an invoice
    already seen (same leverancier+factuurnummer+bedrag) is marked notified
    WITHOUT sending -- see app.payments.is_duplicate_of_existing. Caps
    individual pushes at MAX_NEW_INVOICE_PUSHES_PER_SYNC; anything beyond
    that is rolled into one summary push instead. Returns how many pushes
    were actually sent (summary counts as one)."""
    if not ntfy_configured():
        return 0

    candidates = [
        inv for inv in session.scalars(
            select(Invoice).where(Invoice.notified_new_invoice.is_(False)).order_by(Invoice.received_at)
        )
        if is_payable_invoice(inv)
    ]
    if not candidates:
        return 0

    to_notify: list[Invoice] = []
    for invoice in candidates:
        if is_duplicate_of_existing(session, invoice):
            invoice.notified_new_invoice = True  # a re-sent reminder, not news
            continue
        to_notify.append(invoice)
    if not to_notify:
        return 0

    today = date.today()
    sent = 0
    individual, rest = to_notify[:MAX_NEW_INVOICE_PUSHES_PER_SYNC], to_notify[MAX_NEW_INVOICE_PUSHES_PER_SYNC:]

    for invoice in individual:
        supplier = invoice.supplier_name or "onbekende leverancier"
        title = f"Betalen: {supplier}  EUR {_format_amount(invoice.amount_cents)}"
        message = _due_message(invoice, today)
        days = days_until_due(invoice, today) if invoice.due_date else URGENT_DUE_WITHIN_DAYS + 1
        priority, tags = _urgency(days)
        ok = send_notification(title, message, priority=priority, tags=tags)
        if ok:
            invoice.notified_new_invoice = True
            sent += 1

    if rest:
        n = len(rest)
        total_cents = sum(inv.amount_cents for inv in rest)
        any_urgent = any(
            days_until_due(inv, today) <= URGENT_DUE_WITHIN_DAYS for inv in rest if inv.due_date
        )
        priority, tags = ("urgent", "rotating_light") if any_urgent else ("default", "moneybag")
        title = f"{n} {_plural(n, 'factuur', 'facturen')} te betalen"
        message = f"Totaal EUR {_format_amount(total_cents)}. Bekijk de lijst in de app."
        ok = send_notification(title, message, priority=priority, tags=tags)
        if ok:
            for invoice in rest:
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
    """The 08:00 Europe/Amsterdam push -- the phone version of the
    dashboard's "Vandaag voor jou" block (see app.vandaag), so the numbers
    on the notification always match the dashboard exactly. Silent when
    everything is in order; idempotent per calendar day even if the
    scheduler fires the job twice."""
    if not ntfy_configured() or _already_sent_today(session, "daily-summary"):
        return False

    overzicht = build_vandaag_overzicht(session)
    if overzicht.alles_in_orde:
        _mark_sent_today(session, "daily-summary")
        return False

    today = date.today()
    parts = []
    if overzicht.facturen_te_betalen:
        n = len(overzicht.facturen_te_betalen)
        total_cents = sum(inv.amount_cents for inv in overzicht.facturen_te_betalen)
        parts.append(f"{n} {_plural(n, 'factuur', 'facturen')} te betalen (EUR {_format_amount(total_cents)})")
    if overzicht.banktransacties_zonder_factuur:
        n = len(overzicht.banktransacties_zonder_factuur)
        parts.append(f"{n} {_plural(n, 'transactie', 'transacties')} zonder bon")
    if overzicht.niet_naar_basecone:
        n = len(overzicht.niet_naar_basecone)
        parts.append(f"{n} niet naar Basecone")
    message = ", ".join(parts) + "."

    overdue_count = sum(1 for inv in overzicht.facturen_te_betalen if days_until_due(inv, today) < 0)
    if overdue_count:
        title = f"{overdue_count} {_plural(overdue_count, 'factuur', 'facturen')} te laat"
        priority, tags = "urgent", "rotating_light"
    else:
        title = "Vandaag voor jou"
        priority, tags = "default", "moneybag"

    send_notification(title, message, priority=priority, tags=tags)
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
