"""Background job that periodically runs the Basecone/e-mail sync."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import SessionLocal
from app.sync import run_sync

logger = logging.getLogger("sync_scheduler")

_scheduler: BackgroundScheduler | None = None


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
    scheduler.start()
    _scheduler = scheduler
    return scheduler
