"""Tests the /invoices/backlog dashboard page and its send/status routes --
app.basecone_backlog's own send loop is already covered in
test_basecone_backlog.py, this just checks the page wiring and that it's
never started without a server check."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.basecone_backlog as backlog
from app.basecone_backlog import BacklogSendState
from app.db import Base, get_session
from app.main import app
from app.models import BaseconeForwardStatus, DocumentKind, Invoice, MatchStatus

AUTH = ("sacha", "changeme")


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


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(backlog, "_state", BacklogSendState())


def make_backlog_invoice(session_factory, **kwargs):
    defaults = dict(
        email_message_id=f"<bl{kwargs.get('invoice_number', 'x')}>",
        attachment_filename="f.pdf",
        received_at=datetime(2026, 6, 1),
        supplier_name="Tenways",
        invoice_number="F-1",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        excluded_from_auto_forward=True,
    )
    defaults.update(kwargs)
    with session_factory() as s:
        inv = Invoice(**defaults)
        s.add(inv)
        s.commit()
        return inv.id


def test_backlog_page_requires_auth(client):
    response = client.get("/invoices/backlog")
    assert response.status_code == 401


def test_backlog_page_lists_candidates_grouped_by_supplier(client, session_factory):
    make_backlog_invoice(session_factory, invoice_number="F-1", supplier_name="Tenways", amount_cents=5000)
    make_backlog_invoice(session_factory, invoice_number="F-2", supplier_name="Tenways", amount_cents=3000)
    make_backlog_invoice(session_factory, invoice_number="F-3", supplier_name="Cycletech", amount_cents=4500)

    response = client.get("/invoices/backlog", auth=AUTH)

    assert response.status_code == 200
    assert "Tenways" in response.text
    assert "Cycletech" in response.text


def test_backlog_page_not_for_us_checkbox_is_unchecked_by_default(client, session_factory):
    inv_id = make_backlog_invoice(session_factory, addressee_status="not_for_us")

    response = client.get("/invoices/backlog", auth=AUTH)

    body = response.text
    idx = body.find(f'value="{inv_id}"')
    assert idx != -1
    checkbox_tag = body[max(0, idx - 80):idx + 20]
    assert "checked" not in checkbox_tag


def test_backlog_page_normal_checkbox_is_checked_by_default(client, session_factory):
    inv_id = make_backlog_invoice(session_factory, addressee_status="ok")

    response = client.get("/invoices/backlog", auth=AUTH)

    body = response.text
    idx = body.find(f'value="{inv_id}"')
    checkbox_tag = body[max(0, idx - 80):idx + 20]
    assert "checked" in checkbox_tag


def test_backlog_send_starts_background_send_with_selected_ids(client, session_factory, monkeypatch):
    inv1_id = make_backlog_invoice(session_factory, invoice_number="F-1")
    inv2_id = make_backlog_invoice(session_factory, invoice_number="F-2")

    started_with = []
    monkeypatch.setattr("app.main.start_backlog_send", lambda ids: started_with.append(ids) or True)

    response = client.post("/invoices/backlog/send", data={"invoice_ids": [inv1_id]}, auth=AUTH)

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/backlog"
    assert started_with == [[inv1_id]]


def test_backlog_send_filters_out_invalid_ids(client, session_factory, monkeypatch):
    inv_id = make_backlog_invoice(session_factory)

    started_with = []
    monkeypatch.setattr("app.main.start_backlog_send", lambda ids: started_with.append(ids) or True)

    response = client.post(
        "/invoices/backlog/send", data={"invoice_ids": [inv_id, 999999]}, auth=AUTH
    )

    assert response.status_code == 303
    assert started_with == [[inv_id]]


def test_backlog_status_returns_json_state(client, monkeypatch):
    monkeypatch.setattr(backlog, "_state", BacklogSendState(running=True, total=5, sent=2, errors=["boom"]))

    response = client.get("/invoices/backlog/status", auth=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body == {"running": True, "total": 5, "sent": 2, "errors": ["boom"], "done": False}
