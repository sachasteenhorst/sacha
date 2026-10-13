"""Tests the "don't jump back to the top of the page" fix for every
dashboard list action (Klopt/Koppelen/Al betaald/Loopt via incasso/
Negeren/Terugzetten/Naar Basecone/enz.): a plain POST (no JS) must redirect
back with a #anker to the next row in that same list, and a POST sent the
way static/app.js sends it (header X-Requested-With: fetch) must get back
a small JSON ack instead of a redirect -- see app/main.py's _is_ajax/
_next_anchor/_action_response.
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app
from app.models import (
    DocumentKind,
    Invoice,
    Match,
    MatchMethod,
    MatchStatus,
    PaymentMethod,
    Rule,
    RuleAction,
    Transaction,
)

AUTH = ("sacha", "changeme")
AJAX_HEADERS = {"X-Requested-With": "fetch"}


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def client(session_factory):
    def override_get_session():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def make_unmatched_invoice(session_factory, **kwargs):
    defaults = dict(
        email_message_id=f"<m{kwargs.get('invoice_number', 'x')}>",
        attachment_filename="f.pdf",
        received_at=datetime(2026, 9, 5),
        supplier_name="Kruitbosch",
        invoice_number="F-1",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    with session_factory() as s:
        inv = Invoice(**defaults)
        s.add(inv)
        s.commit()
        return inv.id


def make_payable_invoice(session_factory, **kwargs):
    defaults = dict(
        email_message_id=f"<p{kwargs.get('invoice_number', 'x')}>",
        attachment_filename="f.pdf",
        received_at=datetime(2026, 9, 5),
        supplier_name="Kruitbosch",
        invoice_number="F-1",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        payment_method=PaymentMethod.ONBEKEND.value,
        due_date=date(2026, 10, 1),
    )
    defaults.update(kwargs)
    with session_factory() as s:
        inv = Invoice(**defaults)
        s.add(inv)
        s.commit()
        return inv.id


def make_unmatched_transaction(session_factory, **kwargs):
    defaults = dict(
        external_ref=f"tx-{kwargs.get('counterparty_name', 'x')}",
        booking_date=date(2026, 9, 10),
        amount_cents=-5000,
        counterparty_name="Kruitbosch",
        raw_data={},
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    with session_factory() as s:
        t = Transaction(**defaults)
        s.add(t)
        s.commit()
        return t.id


def make_rule_handled_transaction(session_factory, **kwargs):
    defaults = dict(
        external_ref=f"tx-rule-{kwargs.get('counterparty_name', 'x')}",
        booking_date=date(2026, 9, 10),
        amount_cents=-500,
        counterparty_name="Rabobank kosten",
        raw_data={},
        status=MatchStatus.RULE_HANDLED,
    )
    defaults.update(kwargs)
    with session_factory() as s:
        rule = Rule(name="Bankkosten", action=RuleAction.NO_INVOICE_NEEDED.value)
        s.add(rule)
        s.flush()
        t = Transaction(applied_rule_id=rule.id, **defaults)
        s.add(t)
        s.commit()
        return t.id


# -- Facturen nog niet betaald ("Negeren") ----------------------------------

def test_ignore_invoice_redirects_with_anchor_to_next_row(client, session_factory):
    # Two unmatched invoices, received_at DESC order -> inv1 (newest) comes
    # first in the list, so ignoring it should anchor to inv2's row.
    inv2_id = make_unmatched_invoice(session_factory, invoice_number="F-OLD", received_at=datetime(2026, 9, 1))
    inv1_id = make_unmatched_invoice(session_factory, invoice_number="F-NEW", received_at=datetime(2026, 9, 10))

    response = client.post(f"/invoices/{inv1_id}/ignore", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard#row-inv-unmatched-{inv2_id}"


def test_ignore_invoice_falls_back_to_section_anchor_when_last_row(client, session_factory):
    inv_id = make_unmatched_invoice(session_factory)

    response = client.post(f"/invoices/{inv_id}/ignore", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard#facturen-niet-betaald"


def test_ignore_invoice_ajax_returns_json_ack_without_redirect(client, session_factory):
    inv_id = make_unmatched_invoice(session_factory)

    response = client.post(f"/invoices/{inv_id}/ignore", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# -- Nog te betalen ("Al betaald" / "Loopt via incasso") --------------------

def test_mark_invoice_paid_redirects_with_anchor_to_next_payable_row(client, session_factory):
    # Sorted by due_date ascending -> inv1 (earliest) comes first.
    inv1_id = make_payable_invoice(session_factory, invoice_number="F-A", due_date=date(2026, 10, 1))
    inv2_id = make_payable_invoice(session_factory, invoice_number="F-B", due_date=date(2026, 10, 15))

    response = client.post(f"/invoices/{inv1_id}/mark-paid", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard#row-te-betalen-{inv2_id}"


def test_mark_invoice_incasso_ajax_returns_json_ack(client, session_factory):
    inv_id = make_payable_invoice(session_factory)

    response = client.post(f"/invoices/{inv_id}/mark-incasso", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# -- Betalingen zonder factuur ("Geen factuur nodig" / "Bon staat in Basecone") --

def test_ignore_transaction_redirects_with_anchor_to_next_row(client, session_factory):
    # Sorted by booking_date descending -> the newest comes first.
    tx2_id = make_unmatched_transaction(session_factory, counterparty_name="Oud", booking_date=date(2026, 9, 1))
    tx1_id = make_unmatched_transaction(session_factory, counterparty_name="Nieuw", booking_date=date(2026, 9, 10))

    response = client.post(f"/transactions/{tx1_id}/ignore", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard#row-tx-unmatched-{tx2_id}"


def test_receipt_in_basecone_ajax_returns_json_ack(client, session_factory):
    tx_id = make_unmatched_transaction(session_factory)

    response = client.post(f"/transactions/{tx_id}/receipt-in-basecone", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# -- Automatisch afgehandeld ("Terugzetten") --------------------------------

def test_undo_rule_redirects_with_anchor_to_next_row(client, session_factory):
    t2_id = make_rule_handled_transaction(session_factory, counterparty_name="Oud", booking_date=date(2026, 9, 1))
    t1_id = make_rule_handled_transaction(session_factory, counterparty_name="Nieuw", booking_date=date(2026, 9, 10))

    response = client.post(f"/transactions/{t1_id}/undo-rule", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard#row-rule-handled-{t2_id}"


# -- Te controleren ("Klopt" / "Klopt niet") --------------------------------

def _make_suggested_match_group(session_factory):
    with session_factory() as s:
        tx = Transaction(
            external_ref="tx-suggested", booking_date=date(2026, 9, 10),
            amount_cents=-5000, counterparty_name="Kruitbosch", raw_data={},
            status=MatchStatus.SUGGESTED,
        )
        inv = Invoice(
            email_message_id="<suggested-1>", attachment_filename="f.pdf",
            received_at=datetime(2026, 9, 5), supplier_name="Kruitbosch",
            invoice_number="F-1", amount_cents=5000,
            document_kind=DocumentKind.INVOICE.value, direction="outgoing",
            status=MatchStatus.SUGGESTED,
        )
        s.add_all([tx, inv])
        s.flush()
        match = Match(
            transaction_id=tx.id, invoice_id=inv.id, method=MatchMethod.AMOUNT_DATE,
            confidence="medium", confirmed=False, group_id="group-1",
        )
        s.add(match)
        s.commit()
        return "group-1"


def test_confirm_match_group_ajax_returns_json_ack(client, session_factory):
    group_id = _make_suggested_match_group(session_factory)

    response = client.post(f"/match-groups/{group_id}/confirm", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_reject_match_group_redirects_with_section_anchor_when_last(client, session_factory):
    group_id = _make_suggested_match_group(session_factory)

    response = client.post(f"/match-groups/{group_id}/reject", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard#te-controleren"


# -- Niet naar Basecone ("Stuur naar Basecone") -----------------------------

def make_forwardable_invoice(session_factory, **kwargs):
    defaults = dict(
        email_message_id=f"<fwd{kwargs.get('invoice_number', 'x')}>",
        attachment_filename="f.pdf",
        received_at=datetime(2026, 9, 5),
        supplier_name="Kruitbosch",
        invoice_number="F-1",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.MATCHED,
    )
    defaults.update(kwargs)
    with session_factory() as s:
        inv = Invoice(**defaults)
        s.add(inv)
        s.commit()
        return inv.id


def test_forward_to_basecone_ajax_ok(client, session_factory, monkeypatch):
    inv_id = make_forwardable_invoice(session_factory)
    monkeypatch.setattr("app.main.send_invoice_to_basecone", lambda invoice, method: None)

    response = client.post(f"/invoices/{inv_id}/forward-to-basecone", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_forward_to_basecone_ajax_error_does_not_redirect(client, session_factory, monkeypatch):
    from app.email_client import GraphApiError

    inv_id = make_forwardable_invoice(session_factory)

    def fail(invoice, method):
        raise GraphApiError("geen rechten (403)")

    monkeypatch.setattr("app.main.send_invoice_to_basecone", fail)

    response = client.post(f"/invoices/{inv_id}/forward-to-basecone", data={}, headers=AJAX_HEADERS, auth=AUTH)

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "403" in body["error"]


def test_forward_to_basecone_redirects_with_anchor_on_success(client, session_factory, monkeypatch):
    # _basecone_pending_ids sorts by received_at ascending -> the oldest
    # (inv_old) comes first, so forwarding it should anchor to inv_new next.
    inv_old_id = make_forwardable_invoice(session_factory, invoice_number="F-OLD", received_at=datetime(2026, 9, 1))
    inv_new_id = make_forwardable_invoice(session_factory, invoice_number="F-NEW", received_at=datetime(2026, 9, 10))
    monkeypatch.setattr("app.main.send_invoice_to_basecone", lambda invoice, method: None)

    response = client.post(f"/invoices/{inv_old_id}/forward-to-basecone", data={}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/dashboard?forward_ok=1#row-basecone-pending-{inv_new_id}"
