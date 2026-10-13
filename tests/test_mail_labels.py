"""Tests app/mail_labels.py: desired-category logic and the Graph calls
that apply it (mocked -- no real Graph API call is ever made)."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.email_client as email_client
import app.mail_labels as mail_labels
from app.db import Base
from app.mail_labels import (
    CATEGORY_BASECONE_OK,
    CATEGORY_GEEN_FACTUUR,
    CATEGORY_NOG_NAAR_BASECONE,
    LabelSyncState,
    desired_category,
    ensure_categories_exist,
    invoices_needing_relabel,
    label_error_hint,
    relabel_invoices,
    set_invoice_category,
)
from app.models import BaseconeForwardStatus, DocumentKind, Direction, Invoice, MatchStatus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(email_client._auth, "token", lambda: "tok-1")
    mail_labels._reset_ensured_cache()
    monkeypatch.setattr(mail_labels, "_state", LabelSyncState())


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime(2026, 9, 1),
        invoice_number="F-1",
        supplier_name="Kruitbosch",
        amount_cents=12000,
        document_kind=DocumentKind.INVOICE.value,
        direction=Direction.OUTGOING.value,
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        manually_ignored=False,
        graph_mailbox="facturen@vanderlindentweewielers.nl",
        graph_message_id="msg-abc",
        graph_category="",
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json


# -- desired_category --

def test_desired_category_basecone_ok_wins_regardless_of_everything_else():
    inv = make_invoice(in_basecone=BaseconeForwardStatus.YES.value, manually_ignored=True)
    assert desired_category(inv) == CATEGORY_BASECONE_OK


def test_desired_category_manually_ignored_is_geen_factuur():
    inv = make_invoice(manually_ignored=True)
    assert desired_category(inv) == CATEGORY_GEEN_FACTUUR


def test_desired_category_open_inkoopfactuur_is_nog_naar_basecone():
    inv = make_invoice()
    assert desired_category(inv) == CATEGORY_NOG_NAAR_BASECONE


def test_desired_category_other_document_kind_is_geen_factuur():
    inv = make_invoice(document_kind=DocumentKind.OTHER.value)
    assert desired_category(inv) == CATEGORY_GEEN_FACTUUR


def test_desired_category_cyclesoftware_sales_copy_is_geen_factuur():
    inv = make_invoice(supplier_name="Mobility Services B.V.", direction=Direction.INCOMING.value)
    assert desired_category(inv) == CATEGORY_GEEN_FACTUUR


# -- set_invoice_category / ensure_categories_exist --

def test_set_invoice_category_skips_without_graph_message_id():
    inv = make_invoice(graph_message_id="")
    assert set_invoice_category(inv) is False


def test_set_invoice_category_skips_when_already_correct():
    inv = make_invoice(graph_category=CATEGORY_NOG_NAAR_BASECONE)
    assert set_invoice_category(inv) is False


def test_set_invoice_category_preserves_other_categories(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/outlook/masterCategories"):
            return FakeResponse(200, {"value": [{"displayName": "Belangrijk"}]})
        return FakeResponse(200, {"categories": ["Belangrijk", "Oude label van mij"]})

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, json))
        return FakeResponse(201, {"id": "cat-1"})

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls.append(("PATCH", url, json))
        return FakeResponse(200, {"categories": json["categories"]})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "post", fake_post)
    monkeypatch.setattr(email_client.requests, "patch", fake_patch)

    inv = make_invoice()
    changed = set_invoice_category(inv)

    assert changed is True
    assert inv.graph_category == CATEGORY_NOG_NAAR_BASECONE
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    new_categories = patch_calls[0][2]["categories"]
    assert "Belangrijk" in new_categories
    assert "Oude label van mij" in new_categories
    assert CATEGORY_NOG_NAAR_BASECONE in new_categories
    # Our two OTHER categories (not the one assigned) never leak in.
    assert CATEGORY_BASECONE_OK not in new_categories
    assert CATEGORY_GEEN_FACTUUR not in new_categories
    # masterCategories got the two still-missing categories created.
    created_names = {c[2]["displayName"] for c in calls if c[0] == "POST"}
    assert CATEGORY_NOG_NAAR_BASECONE in created_names
    assert CATEGORY_GEEN_FACTUUR in created_names
    assert "Belangrijk" not in created_names  # already existed -- not recreated


def test_ensure_categories_exist_failure_is_swallowed(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(403, text="forbidden")

    monkeypatch.setattr(email_client.requests, "get", fake_get)

    ensure_categories_exist("facturen@vanderlindentweewielers.nl")  # must not raise


def test_set_invoice_category_still_labels_message_when_master_categories_fails(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/outlook/masterCategories"):
            return FakeResponse(403, text="forbidden")
        return FakeResponse(200, {"categories": []})

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return FakeResponse(200, {})

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "patch", fake_patch)

    inv = make_invoice()
    changed = set_invoice_category(inv)

    assert changed is True
    assert calls and CATEGORY_NOG_NAAR_BASECONE in calls[0]["categories"]


def test_set_invoice_category_raises_on_message_patch_permission_error(monkeypatch):
    from app.email_client import GraphApiError

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/outlook/masterCategories"):
            return FakeResponse(200, {"value": []})
        return FakeResponse(200, {"categories": []})

    def fake_patch(url, headers=None, json=None, timeout=None):
        return FakeResponse(403, text="forbidden")

    monkeypatch.setattr(email_client.requests, "get", fake_get)
    monkeypatch.setattr(email_client.requests, "post", lambda *a, **k: FakeResponse(201, {}))
    monkeypatch.setattr(email_client.requests, "patch", fake_patch)

    inv = make_invoice()
    with pytest.raises(GraphApiError):
        set_invoice_category(inv)


# -- invoices_needing_relabel / relabel_invoices --

def test_invoices_needing_relabel_skips_up_to_date_ones(session):
    up_to_date = make_invoice(email_message_id="<a>", graph_category=CATEGORY_NOG_NAAR_BASECONE)
    stale = make_invoice(email_message_id="<b>", graph_category="")
    no_message_id = make_invoice(email_message_id="<c>", graph_message_id="")
    session.add_all([up_to_date, stale, no_message_id])
    session.commit()

    needing = invoices_needing_relabel(session)

    assert stale in needing
    assert up_to_date not in needing
    assert no_message_id not in needing


def test_relabel_invoices_stops_on_auth_error(monkeypatch):
    from app.email_client import GraphAuthError

    def boom(_inv):
        raise GraphAuthError("niet ingelogd")

    monkeypatch.setattr(mail_labels, "set_invoice_category", boom)
    inv1 = make_invoice(email_message_id="<a>")
    inv2 = make_invoice(email_message_id="<b>")

    result = relabel_invoices([inv1, inv2])

    assert result.updated == 0
    assert len(result.errors) == 1  # stopped after the first, never tried the second


def test_relabel_invoices_continues_past_a_single_api_error(monkeypatch):
    from app.email_client import GraphApiError

    def flaky(inv):
        if inv.email_message_id == "<a>":
            raise GraphApiError("tijdelijke fout")
        return True

    monkeypatch.setattr(mail_labels, "set_invoice_category", flaky)
    inv1 = make_invoice(email_message_id="<a>")
    inv2 = make_invoice(email_message_id="<b>")

    result = relabel_invoices([inv1, inv2])

    assert result.updated == 1
    assert len(result.errors) == 1


# -- label_error_hint --

def test_label_error_hint_permission_issue():
    hint = label_error_hint("Geen rechten om e-mails te labelen in facturen@... (fout 403)")
    assert "mail.readwrite" in hint.lower() or "rechten" in hint.lower()


def test_label_error_hint_none_when_no_error():
    assert label_error_hint(None) is None
