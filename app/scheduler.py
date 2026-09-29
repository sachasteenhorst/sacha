"""Background jobs: the Basecone/e-mail sync, the Ponto bank sync, and the
daily "Nog te betalen" push-notification summary.
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.bank_ponto import ponto_configured, run_ponto_sync
from app.config import settings
from app.db import SessionLocal
from app.notify import notify_new_payable_invoices, send_daily_payment_summary, send_warning_once_per_day
from app.sync import run_sync

logger = logging.getLogger("sync_scheduler")

_scheduler: BackgroundScheduler | None = None


def _warn_for_sync_errors(session, errors: list[str]) -> None:
    email_error = next((e for e in errors if e.startswith("E-mail:")), None)
    if email_error:
        send_warning_once_per_day(
            session, "graph_login",
            "Microsoft Graph-login probleem",
            f"E-mail ophalen lukt niet: {email_error}. Mogelijk moet je opnieuw inloggen "
            "(scripts/graph_login.py).",
        )
    forward_error = next((e for e in errors if e.startswith("Basecone doorsturen:")), None)
    if forward_error:
        send_warning_once_per_day(
            session, "basecone_forward",
            "Basecone-doorsturen mislukt",
            forward_error,
        )


def _job() -> None:
    session = SessionLocal()
    try:
        result = run_sync(session)
        logger.info(
            "Sync klaar: %s nieuwe transacties, %s nieuwe facturen, %s errors",
            result.new_transactions,
            result.new_invoices,
            len(result.errors),
        )
        for err in result.errors:
            logger.warning("Sync fout: %s", err)
        notify_new_payable_invoices(session)
        _warn_for_sync_errors(session, result.errors)
        session.commit()
    finally:
        session.close()


def _ponto_job() -> None:
    if not ponto_configured():
        return
    session = SessionLocal()
    try:
        result = run_ponto_sync(session)
        for err in result.errors:
            logger.warning("Ponto-sync fout: %s", err)
            send_warning_once_per_day(session, "ponto_sync", "Bank-sync (Ponto) mislukt", err)
        session.commit()
    finally:
        session.close()


def _daily_summary_job() -> None:
    session = SessionLocal()
    try:
        send_daily_payment_summary(session)
        session.commit()
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _job,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="basecone_email_sync",
    )
    scheduler.add_job(
        _ponto_job,
        "interval",
        hours=4,
        id="ponto_bank_sync",
    )
    scheduler.add_job(
        _daily_summary_job,
        CronTrigger(hour=8, minute=0, timezone=ZoneInfo("Europe/Amsterdam")),
        id="daily_payment_summary",
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler
