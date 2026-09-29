"""Tests app/notify.py's dedupe logic and message building -- ntfy itself is
never actually called (requests.post is monkeypatched)."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.notify as notify
from app.config import settings
from app.db import Base
from app.models import DocumentKind, Invoice, MatchStatus, NotificationLog, PaymentMethod


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _configure_ntfy(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "test-topic")
    monkeypatch.setattr(settings, "ntfy_url", "https://ntfy.sh")
    monkeypatch.setattr(settings, "ntfy_token", "")


@pytest.fixture()
def sent_notifications(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    return calls


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime(2026, 9, 1),
        invoice_number="F-1",
        supplier_name="Kruitbosch",
        amount_cents=12000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        payment_method=PaymentMethod.ONBEKEND.value,
        due_date=date(2026, 9, 30),
        notified_new_invoice=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


# -- send_notification / ntfy_configured --

def test_ntfy_configured_false_without_topic(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "")
    assert notify.ntfy_configured() is False


def test_send_notification_noop_without_topic(monkeypatch, sent_notifications):
    monkeypatch.setattr(settings, "ntfy_topic", "")
    assert notify.send_notification("Titel", "Bericht") is False
    assert sent_notifications == []


def test_send_notification_posts_to_ntfy_with_headers(sent_notifications):
    ok = notify.send_notification("Titel", "Bericht", priority="high", tags="warning")
    assert ok is True
    assert len(sent_notifications) == 1
    call = sent_notifications[0]
    assert call["url"] == "https://ntfy.sh/test-topic"
    assert call["headers"]["Title"] == "Titel"
    assert call["headers"]["Priority"] == "high"
    assert call["headers"]["Tags"] == "warning"
    assert call["headers"]["Click"].endswith("/dashboard#nog-te-betalen")


# -- notify_new_payable_invoices --

def test_notify_new_payable_invoices_sends_once_and_marks_notified(session, sent_notifications):
    inv = make_invoice()
    session.add(inv)
    session.commit()

    sent = notify.notify_new_payable_invoices(session)
    assert sent == 1
    assert inv.notified_new_invoice is True
    assert "Kruitbosch" in sent_notifications[0]["data"].decode("utf-8")

    # A second call must never re-notify the same invoice.
    sent_again = notify.notify_new_payable_invoices(session)
    assert sent_again == 0
    assert len(sent_notifications) == 1


def test_notify_new_payable_invoices_skips_incasso_and_already_notified(session, sent_notifications):
    incasso_inv = make_invoice(payment_method=PaymentMethod.INCASSO.value, email_message_id="<m2>")
    already_notified = make_invoice(notified_new_invoice=True, email_message_id="<m3>")
    session.add_all([incasso_inv, already_notified])
    session.commit()

    sent = notify.notify_new_payable_invoices(session)
    assert sent == 0
    assert sent_notifications == []


# -- send_daily_payment_summary --

def test_daily_summary_sends_when_something_overdue(session, sent_notifications):
    inv = make_invoice(due_date=date.today() - timedelta(days=2))
    session.add(inv)
    session.commit()

    sent = notify.send_daily_payment_summary(session)
    assert sent is True
    assert len(sent_notifications) == 1
    assert sent_notifications[0]["headers"]["Priority"] == "high"


def test_daily_summary_silent_when_nothing_due_soon(session, sent_notifications):
    inv = make_invoice(due_date=date.today() + timedelta(days=30))
    session.add(inv)
    session.commit()

    sent = notify.send_daily_payment_summary(session)
    assert sent is False
    assert sent_notifications == []


def test_daily_summary_only_sent_once_per_day(session, sent_notifications):
    inv = make_invoice(due_date=date.today() - timedelta(days=1))
    session.add(inv)
    session.commit()

    notify.send_daily_payment_summary(session)
    session.commit()
    assert len(sent_notifications) == 1

    # A second call the same day (e.g. the scheduler firing twice) must not
    # send a duplicate push.
    result = notify.send_daily_payment_summary(session)
    assert result is False
    assert len(sent_notifications) == 1


# -- send_warning_once_per_day --

def test_warning_sent_once_per_day_per_key(session, sent_notifications):
    first = notify.send_warning_once_per_day(session, "graph_login", "Titel", "Bericht")
    session.commit()
    second = notify.send_warning_once_per_day(session, "graph_login", "Titel", "Bericht")

    assert first is True
    assert second is False
    assert len(sent_notifications) == 1


def test_warning_different_keys_both_send(session, sent_notifications):
    notify.send_warning_once_per_day(session, "graph_login", "A", "a")
    session.commit()
    notify.send_warning_once_per_day(session, "basecone_forward", "B", "b")
    session.commit()
    assert len(sent_notifications) == 2
