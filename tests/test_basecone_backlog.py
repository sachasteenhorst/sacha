"""Tests app/basecone_backlog.py: the candidate list and the (synchronous,
for test purposes) send loop -- no real background thread or Graph call."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.basecone_backlog as backlog
from app.basecone_backlog import BacklogSendState, backlog_candidates
from app.db import Base
from app.email_client import GraphApiError, GraphAuthError
from app.models import BaseconeForwardStatus, DocumentKind, Invoice, MatchStatus


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(backlog, "_state", BacklogSendState())
    monkeypatch.setattr(backlog.time, "sleep", lambda seconds: None)


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime(2026, 6, 1),
        invoice_number="F-1",
        supplier_name="Tenways",
        amount_cents=12345,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        excluded_from_auto_forward=True,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


# -- backlog_candidates --

def test_backlog_candidates_only_excluded_forwardable_unsent_invoices(session_factory):
    with session_factory() as s:
        excluded = make_invoice(email_message_id="<a>")
        not_excluded = make_invoice(email_message_id="<b>", excluded_from_auto_forward=False)
        already_sent = make_invoice(email_message_id="<c>", in_basecone=BaseconeForwardStatus.YES.value)
        not_forwardable = make_invoice(email_message_id="<d>", document_kind=DocumentKind.OTHER.value)
        s.add_all([excluded, not_excluded, already_sent, not_forwardable])
        s.commit()

        candidates = backlog_candidates(s)

        assert [inv.id for inv in candidates] == [excluded.id]


def test_backlog_candidates_includes_not_for_us_invoices_for_manual_override(session_factory):
    """"niet aan ons gericht" still shows up here -- unchecked by default
    in templates/backlog.html -- so Sacha can override it per-invoice by
    ticking the box, unlike a real never-forward reason (own sales
    invoice, CycleSoftware/Twinfield, incoming document)."""
    with session_factory() as s:
        not_for_us = make_invoice(email_message_id="<a>", addressee_status="not_for_us")
        own_sale = make_invoice(email_message_id="<b>", supplier_name="Van der Linden Tweewielers")
        s.add_all([not_for_us, own_sale])
        s.commit()

        candidates = backlog_candidates(s)

        assert [inv.id for inv in candidates] == [not_for_us.id]


# -- start_backlog_send / _run_send --

def test_start_backlog_send_refuses_a_second_concurrent_run(monkeypatch):
    monkeypatch.setattr(backlog, "_state", BacklogSendState(running=True))
    assert backlog.start_backlog_send([1, 2, 3]) is False


def test_run_send_sends_each_invoice_and_tracks_progress(session_factory, monkeypatch):
    with session_factory() as s:
        inv1 = make_invoice(email_message_id="<a>")
        inv2 = make_invoice(email_message_id="<b>")
        s.add_all([inv1, inv2])
        s.commit()
        ids = [inv1.id, inv2.id]

    monkeypatch.setattr(backlog, "SessionLocal", session_factory)
    sent_calls = []
    monkeypatch.setattr(backlog, "send_invoice_to_basecone", lambda invoice, method: sent_calls.append(invoice.id))

    backlog._run_send(ids)

    assert sent_calls == ids
    state = backlog.get_state()
    assert state.sent == 2
    assert state.running is False
    assert state.done is True
    assert state.errors == []


def test_run_send_continues_past_a_single_api_error(session_factory, monkeypatch):
    with session_factory() as s:
        inv1 = make_invoice(email_message_id="<a>")
        inv2 = make_invoice(email_message_id="<b>")
        s.add_all([inv1, inv2])
        s.commit()
        ids = [inv1.id, inv2.id]

    monkeypatch.setattr(backlog, "SessionLocal", session_factory)

    def flaky(invoice, method):
        if invoice.email_message_id == "<a>":
            raise GraphApiError("tijdelijke fout")

    monkeypatch.setattr(backlog, "send_invoice_to_basecone", flaky)

    backlog._run_send(ids)

    state = backlog.get_state()
    assert state.sent == 1
    assert len(state.errors) == 1


def test_run_send_stops_on_auth_error(session_factory, monkeypatch):
    with session_factory() as s:
        inv1 = make_invoice(email_message_id="<a>")
        inv2 = make_invoice(email_message_id="<b>")
        s.add_all([inv1, inv2])
        s.commit()
        ids = [inv1.id, inv2.id]

    monkeypatch.setattr(backlog, "SessionLocal", session_factory)

    def fail(invoice, method):
        raise GraphAuthError("niet ingelogd")

    monkeypatch.setattr(backlog, "send_invoice_to_basecone", fail)

    backlog._run_send(ids)

    state = backlog.get_state()
    assert state.sent == 0
    assert len(state.errors) == 1
    assert state.done is True


def test_run_send_sleeps_between_invoices_but_not_after_the_last(session_factory, monkeypatch):
    with session_factory() as s:
        inv1 = make_invoice(email_message_id="<a>")
        inv2 = make_invoice(email_message_id="<b>")
        s.add_all([inv1, inv2])
        s.commit()
        ids = [inv1.id, inv2.id]

    monkeypatch.setattr(backlog, "SessionLocal", session_factory)
    monkeypatch.setattr(backlog, "send_invoice_to_basecone", lambda invoice, method: None)
    sleep_calls = []
    monkeypatch.setattr(backlog.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    backlog._run_send(ids)

    assert sleep_calls == [backlog.SEND_DELAY_SECONDS]
