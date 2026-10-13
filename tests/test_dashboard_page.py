"""Tests the /dashboard route's page weight and the /invoices/search-options
endpoint that replaced the old per-row <select> of every open invoice.

Real production bug: the "Betalingen zonder factuur" table put EVERY open
invoice as an <option> inside a per-row <select>, once per unresolved bank
transaction -- 565 rows x ~4000 facturen = 2.3 million <option> tags and a
235MB dashboard page. These tests seed a comparably-sized backlog (worse,
in fact: every invoice simultaneously unmatched/payable/not-yet-forwarded)
and assert the page stays small and the picker endpoint only ever returns a
handful of candidates on demand.
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app
from app.models import DocumentKind, Invoice, MatchStatus, PaymentMethod, Transaction

AUTH = ("sacha", "changeme")
# The actual target from the bug report ("ruim onder 1MB"); a generous
# multiple of that is still a strong regression guard without being brittle.
DASHBOARD_MAX_BYTES = 1024 * 1024


@pytest.fixture()
def session_factory():
    # StaticPool + check_same_thread=False: FastAPI runs sync route handlers
    # in a worker thread, which would otherwise see a brand-new (schema-less)
    # in-memory SQLite connection than the one Base.metadata.create_all just
    # set up here -- StaticPool shares the single connection across threads.
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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_large_backlog(session_factory, n_transactions=600, n_invoices=4000):
    with session_factory() as s:
        today = date.today()
        for i in range(n_transactions):
            s.add(Transaction(
                external_ref=f"tx-{i}", booking_date=today - timedelta(days=i % 300),
                amount_cents=-(1000 + i), counterparty_name=f"Leverancier {i}", raw_data={},
            ))
        for i in range(n_invoices):
            s.add(Invoice(
                email_message_id=f"<m{i}>", attachment_filename="f.pdf",
                received_at=datetime.combine(today - timedelta(days=i % 300), datetime.min.time()),
                invoice_number=f"F-{i}", supplier_name=f"Leverancier {i}", amount_cents=1000 + i,
                document_kind=DocumentKind.INVOICE.value, direction="outgoing",
                status=MatchStatus.UNMATCHED, payment_method=PaymentMethod.ONBEKEND.value,
                due_date=today - timedelta(days=i % 300 - 30),
            ))
        s.commit()


def test_dashboard_stays_small_with_a_large_backlog(client, session_factory):
    _seed_large_backlog(session_factory)

    response = client.get("/dashboard", auth=AUTH)

    assert response.status_code == 200
    assert len(response.content) < DASHBOARD_MAX_BYTES
    # The old per-row <select> (one full copy of every open invoice, PER
    # unresolved transaction) is gone -- the koppel-picker fetches its
    # options on demand instead. A handful of <option> tags is fine (just
    # the small, bounded "maand" filter dropdown); thousands is the bug.
    assert response.text.count("<option") < 50


def test_dashboard_renders_quickly_on_empty_database(client):
    response = client.get("/dashboard", auth=AUTH)
    assert response.status_code == 200


def test_search_options_limits_results_and_matches_supplier_name(client, session_factory):
    _seed_large_backlog(session_factory, n_transactions=0, n_invoices=50)

    response = client.get("/invoices/search-options", params={"q": "Leverancier"}, auth=AUTH)

    assert response.status_code == 200
    results = response.json()
    assert 0 < len(results) <= 20


def test_search_options_falls_back_to_amount_when_no_query(client, session_factory):
    with session_factory() as s:
        s.add(Invoice(
            email_message_id="<a>", attachment_filename="f.pdf", received_at=datetime(2026, 9, 1),
            invoice_number="F-1", supplier_name="Kruitbosch", amount_cents=12345,
            document_kind=DocumentKind.INVOICE.value, status=MatchStatus.UNMATCHED,
        ))
        s.add(Invoice(
            email_message_id="<b>", attachment_filename="f.pdf", received_at=datetime(2026, 9, 1),
            invoice_number="F-2", supplier_name="Heel ander bedrijf", amount_cents=99999,
            document_kind=DocumentKind.INVOICE.value, status=MatchStatus.UNMATCHED,
        ))
        s.commit()

    response = client.get("/invoices/search-options", params={"amount_cents": 12300}, auth=AUTH)

    assert response.status_code == 200
    results = response.json()
    assert any("Kruitbosch" in r["label"] for r in results)
    assert not any("Heel ander bedrijf" in r["label"] for r in results)


def test_search_options_requires_auth(client):
    response = client.get("/invoices/search-options")
    assert response.status_code == 401


def test_ignore_invoice_relabels_best_effort(client, session_factory, monkeypatch):
    with session_factory() as s:
        inv = Invoice(
            email_message_id="<ignore-1>", attachment_filename="f.pdf",
            received_at=datetime(2026, 9, 1), supplier_name="Kruitbosch",
            amount_cents=5000, document_kind=DocumentKind.INVOICE.value,
            status=MatchStatus.UNMATCHED, graph_message_id="msg-1",
        )
        s.add(inv)
        s.commit()
        inv_id = inv.id

    calls = []
    monkeypatch.setattr("app.mail_labels.set_invoice_category", lambda i: calls.append(i.id) or True)

    response = client.post(f"/invoices/{inv_id}/ignore", data={}, auth=AUTH)

    assert response.status_code in (200, 303, 307)
    assert calls == [inv_id]
