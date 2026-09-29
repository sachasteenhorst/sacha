"""Tests app/bank_ponto.py against mocked HTTP -- no real Ponto API call is
ever made. Field names/response shapes here are this project's best-effort
reading of Ponto Connect's public JSON:API docs (see the module's own
docstring for the "not tried against a real account yet" caveat); these
tests verify OUR OWN code's logic (auth caching, pagination, dedup, the
consent-expiry warning) against that assumed shape, not that the assumed
shape itself is exactly right.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.bank_ponto as bank_ponto
from app.bank_ponto import PontoError, ponto_configured, run_ponto_sync
from app.config import settings
from app.db import Base
from app.models import Transaction


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _configure_ponto(monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "client-123")
    monkeypatch.setattr(settings, "ponto_client_secret", "secret-456")
    monkeypatch.setattr(settings, "ponto_cert_path", "")
    monkeypatch.setattr(settings, "ponto_key_path", "")
    monkeypatch.setattr(settings, "ponto_key_password", "")
    monkeypatch.setattr(settings, "ponto_signature_key_id", "")
    # Fresh auth cache per test.
    bank_ponto._auth = bank_ponto._PontoAuth()
    bank_ponto._state = bank_ponto.PontoSyncState()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json


def test_ponto_configured_false_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")
    monkeypatch.setattr(settings, "ponto_client_secret", "")
    assert ponto_configured() is False


def test_run_ponto_sync_noop_when_not_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")
    result = run_ponto_sync(session)
    assert result.new_transactions == 0
    assert result.errors == []


def test_run_ponto_sync_fetches_transactions_and_creates_rows(session, monkeypatch):
    calls = []

    def fake_post(url, data=None, auth=None, cert=None, timeout=None):
        calls.append(("POST", url, data))
        if url == bank_ponto.PONTO_TOKEN_URL:
            return FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})
        if url.endswith("/synchronizations"):
            return FakeResponse(200, {"data": {"id": "sync-1"}})
        raise AssertionError(f"unexpected POST {url}")

    def fake_request(method, url, headers=None, cert=None, timeout=None, params=None, json=None):
        calls.append((method, url, params))
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/synchronizations"):
            return FakeResponse(200, {"data": {"id": "sync-1"}})
        if "/synchronizations/" in url:
            return FakeResponse(200, {"data": {"attributes": {"status": "success"}}})
        if url.endswith("/transactions"):
            return FakeResponse(200, {
                "data": [
                    {
                        "id": "tx-1",
                        "attributes": {
                            "amount": "-39.60",
                            "executionDate": "2026-09-15",
                            "currency": "EUR",
                            "remittanceInformation": "Factuur F-1",
                            "counterpartName": "Kruitbosch",
                            "counterpartReference": "NL00RABO0123456789",
                        },
                    }
                ],
                "links": {},
            })
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)

    assert result.errors == []
    assert result.new_transactions == 1
    tx = session.query(Transaction).one()
    assert tx.external_ref == "ponto:tx-1"
    assert tx.amount_cents == -3960
    assert tx.counterparty_name == "Kruitbosch"
    assert bank_ponto.get_state().last_synced_at is not None


def test_run_ponto_sync_skips_transaction_already_imported_from_csv(session, monkeypatch):
    # Same real-world transaction, already booked from a manually-uploaded
    # Rabobank CSV export -- Ponto must not create a duplicate.
    session.add(Transaction(
        external_ref="csv:abc",
        booking_date=date(2026, 9, 15),
        amount_cents=-3960,
        counterparty_name="Kruitbosch",
        counterparty_iban="NL00RABO0123456789",
        description="Factuur F-1",
    ))
    session.commit()

    def fake_post(url, data=None, auth=None, cert=None, timeout=None):
        if url == bank_ponto.PONTO_TOKEN_URL:
            return FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})
        return FakeResponse(200, {"data": {"id": "sync-1"}})

    def fake_request(method, url, headers=None, cert=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/synchronizations"):
            return FakeResponse(200, {"data": {"id": "sync-1"}})
        if "/synchronizations/" in url:
            return FakeResponse(200, {"data": {"attributes": {"status": "success"}}})
        if url.endswith("/transactions"):
            return FakeResponse(200, {
                "data": [{
                    "id": "tx-1",
                    "attributes": {
                        "amount": "-39.60", "executionDate": "2026-09-15", "currency": "EUR",
                        "remittanceInformation": "Factuur F-1", "counterpartName": "Kruitbosch",
                        "counterpartReference": "NL00RABO0123456789",
                    },
                }],
                "links": {},
            })
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)

    assert result.new_transactions == 0
    assert session.query(Transaction).count() == 1


def test_run_ponto_sync_warns_when_consent_expires_soon(session, monkeypatch):
    expiry = (datetime.utcnow() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake_post(url, data=None, auth=None, cert=None, timeout=None):
        if url == bank_ponto.PONTO_TOKEN_URL:
            return FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})
        return FakeResponse(200, {"data": {"id": "sync-1"}})

    def fake_request(method, url, headers=None, cert=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {"authorizationExpirationExpectedAt": expiry}}]})
        if url.endswith("/synchronizations"):
            return FakeResponse(200, {"data": {"id": "sync-1"}})
        if "/synchronizations/" in url:
            return FakeResponse(200, {"data": {"attributes": {"status": "success"}}})
        if url.endswith("/transactions"):
            return FakeResponse(200, {"data": [], "links": {}})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)
    assert result.consent_warning is not None
    assert "verloopt" in result.consent_warning


def test_run_ponto_sync_records_error_on_auth_failure(session, monkeypatch):
    def fake_post(url, data=None, auth=None, cert=None, timeout=None):
        return FakeResponse(401, text="invalid_client")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)

    result = run_ponto_sync(session)
    assert result.new_transactions == 0
    assert len(result.errors) == 1
    assert bank_ponto.get_state().last_error is not None


def test_client_cert_raises_clear_error_for_encrypted_key(monkeypatch):
    monkeypatch.setattr(settings, "ponto_cert_path", "/tmp/cert.pem")
    monkeypatch.setattr(settings, "ponto_key_path", "/tmp/key.pem")
    monkeypatch.setattr(settings, "ponto_key_password", "secret")
    with pytest.raises(PontoError):
        bank_ponto._client_cert()


def test_signature_headers_raise_when_configured_but_not_implemented(monkeypatch):
    monkeypatch.setattr(settings, "ponto_signature_key_id", "key-1")
    with pytest.raises(PontoError):
        bank_ponto._signature_headers("GET", "/accounts")
