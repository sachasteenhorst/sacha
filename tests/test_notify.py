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
    # Short, plain-language push: supplier + amount in the title, only the
    # due date (no factuurnummer/omschrijving/technical details) in the body.
    call = sent_notifications[0]
    assert call["headers"]["Title"] == "Betalen: Kruitbosch  EUR 120,00"
    assert "F-1" not in call["data"].decode("utf-8")

    # A second call must never re-notify the same invoice.
    sent_again = notify.notify_new_payable_invoices(session)
    assert sent_again == 0
    assert len(sent_notifications) == 1


def test_notify_new_payable_invoices_urgent_when_due_soon(session, sent_notifications):
    inv = make_invoice(due_date=date.today() + timedelta(days=2))
    session.add(inv)
    session.commit()

    notify.notify_new_payable_invoices(session)

    call = sent_notifications[0]
    assert call["headers"]["Priority"] == "urgent"
    assert call["headers"]["Tags"] == "rotating_light"
    assert "nog 2 dagen" in call["data"].decode("utf-8")


def test_notify_new_payable_invoices_urgent_when_overdue(session, sent_notifications):
    inv = make_invoice(due_date=date.today() - timedelta(days=5))
    session.add(inv)
    session.commit()

    notify.notify_new_payable_invoices(session)

    call = sent_notifications[0]
    assert call["headers"]["Priority"] == "urgent"
    assert "Te laat sinds" in call["data"].decode("utf-8")
    assert "5 dagen" in call["data"].decode("utf-8")


def test_notify_new_payable_invoices_default_priority_when_due_later(session, sent_notifications):
    inv = make_invoice(due_date=date.today() + timedelta(days=20))
    session.add(inv)
    session.commit()

    notify.notify_new_payable_invoices(session)

    call = sent_notifications[0]
    assert call["headers"]["Priority"] == "default"
    assert call["headers"]["Tags"] == "moneybag"


def test_notify_new_payable_invoices_skips_incasso_and_already_notified(session, sent_notifications):
    incasso_inv = make_invoice(payment_method=PaymentMethod.INCASSO.value, email_message_id="<m2>")
    already_notified = make_invoice(notified_new_invoice=True, email_message_id="<m3>")
    session.add_all([incasso_inv, already_notified])
    session.commit()

    sent = notify.notify_new_payable_invoices(session)
    assert sent == 0
    assert sent_notifications == []


def test_notify_new_payable_invoices_caps_individual_pushes_and_sends_one_summary(session, sent_notifications):
    # Real production scenario: after an upgrade, 258 invoices all had
    # notified_new_invoice=False at once -- must never fire 258 pushes.
    invoices = [
        make_invoice(email_message_id=f"<m{i}>", invoice_number=f"F-{i}", supplier_name=f"Leverancier {i}")
        for i in range(8)
    ]
    session.add_all(invoices)
    session.commit()

    sent = notify.notify_new_payable_invoices(session)

    # 5 individual pushes + 1 summary push for the remaining 3.
    assert sent == notify.MAX_NEW_INVOICE_PUSHES_PER_SYNC + 1
    assert len(sent_notifications) == notify.MAX_NEW_INVOICE_PUSHES_PER_SYNC + 1
    assert all(inv.notified_new_invoice for inv in invoices)
    summary_call = sent_notifications[-1]
    assert summary_call["headers"]["Title"] == "3 facturen te betalen"


def test_notify_new_payable_invoices_marks_reminder_notified_without_sending(session, sent_notifications):
    # A supplier resending the same invoice (already pushed once) must
    # never trigger a second push, even though it's a "new" DB row.
    original = make_invoice(email_message_id="<a>", received_at=datetime(2026, 9, 1), notified_new_invoice=True)
    reminder = make_invoice(email_message_id="<b>", received_at=datetime(2026, 9, 15), notified_new_invoice=False)
    session.add_all([original, reminder])
    session.commit()

    sent = notify.notify_new_payable_invoices(session)

    assert sent == 0
    assert sent_notifications == []
    assert reminder.notified_new_invoice is True


# -- send_daily_payment_summary --
#
# This is the push-notification version of the dashboard's "Vandaag voor
# jou" block (app.vandaag) -- same three buckets, same "alles_in_orde"
# silence rule, so what's fired here always matches what's on the
# dashboard. Note this is a deliberate behaviour change from the old
# "due-soon window" rule: a payable invoice due in 30 days now DOES trigger
# a push (it's still something Sacha has to pay), it just isn't flagged
# urgent.

def test_daily_summary_sends_when_something_overdue(session, sent_notifications):
    inv = make_invoice(due_date=date.today() - timedelta(days=2))
    session.add(inv)
    session.commit()

    sent = notify.send_daily_payment_summary(session)
    assert sent is True
    assert len(sent_notifications) == 1
    call = sent_notifications[0]
    assert call["headers"]["Priority"] == "urgent"
    assert call["headers"]["Title"] == "1 factuur te laat"
    assert "1 factuur te betalen (EUR 120,00)" in call["data"].decode("utf-8")


def test_daily_summary_sends_but_not_urgent_when_due_later(session, sent_notifications):
    inv = make_invoice(due_date=date.today() + timedelta(days=30))
    session.add(inv)
    session.commit()

    sent = notify.send_daily_payment_summary(session)
    assert sent is True
    call = sent_notifications[0]
    assert call["headers"]["Priority"] == "default"
    assert call["headers"]["Title"] == "Vandaag voor jou"


def test_daily_summary_silent_when_alles_in_orde(session, sent_notifications):
    sent = notify.send_daily_payment_summary(session)
    assert sent is False
    assert sent_notifications == []


def test_daily_summary_includes_transactions_without_invoice(session, sent_notifications):
    from app.models import Transaction

    session.add(Transaction(
        external_ref="tx-1", booking_date=date.today(), amount_cents=-5000,
        counterparty_name="Onbekend", raw_data={},
    ))
    session.commit()

    sent = notify.send_daily_payment_summary(session)
    assert sent is True
    message = sent_notifications[0]["data"].decode("utf-8")
    assert "1 transactie zonder bon" in message


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
