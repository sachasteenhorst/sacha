"""Tests app/scheduler.py's _ponto_job safety net: even an unanticipated bug
inside run_ponto_sync (not just a network failure, already handled inside
app/bank_ponto.py itself) must never again silently crash the job without
recording an error -- that's exactly what happened in production (see
tests/test_bank_ponto.py's connection-error tests for the actual root cause
that was fixed; this covers the belt-and-braces outer catch on top of it).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.scheduler as scheduler
from app.bank_ponto import PontoSyncState
from app.config import settings
from app.db import Base


@pytest.fixture(autouse=True)
def _configure_ponto(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "")  # no real push in these tests
    monkeypatch.setattr(settings, "ponto_client_id", "client-123")
    monkeypatch.setattr(settings, "ponto_client_secret", "secret-456")
    import app.bank_ponto as bank_ponto
    monkeypatch.setattr(bank_ponto, "_state", PontoSyncState())


@pytest.fixture()
def isolated_session_local(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    return Session


def test_ponto_job_records_error_when_run_ponto_sync_raises_unexpectedly(isolated_session_local, monkeypatch):
    def boom(session):
        raise ValueError("onverwachte bug in de parser")

    monkeypatch.setattr(scheduler, "run_ponto_sync", boom)

    scheduler._ponto_job()  # must not raise

    from app.bank_ponto import get_state
    assert get_state().last_error == "onverwachte bug in de parser"


def test_ponto_job_does_nothing_when_not_configured(isolated_session_local, monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")

    def boom(session):
        raise AssertionError("run_ponto_sync must never be called when Ponto isn't configured")

    monkeypatch.setattr(scheduler, "run_ponto_sync", boom)

    scheduler._ponto_job()  # must not raise, must not call run_ponto_sync
