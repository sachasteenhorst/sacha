"""Tests app/bank_ponto.py against mocked HTTP -- no real Ponto API call is
ever made. Some field names/response shapes here are now confirmed against
a real Integration on production (see the module's own docstring); others
are still this project's best-effort reading of Ponto's public JSON:API
docs. These tests verify OUR OWN code's logic (auth caching, pagination,
the robust IBAN/name+date-tolerance dedupe, the consent-expiry warning, and
-- critically -- that the scheduled/read-only path NEVER triggers a
synchronization) against that shape, not that every unverified piece of the
shape itself is exactly right.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.bank_ponto as bank_ponto
from app.bank_ponto import PontoError, ponto_configured, run_manual_ponto_refresh, run_ponto_sync
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


def _fake_token_post(url, data=None, auth=None, timeout=None):
    assert url == settings.ponto_token_url
    assert auth == (settings.ponto_client_id, settings.ponto_client_secret)
    return FakeResponse(200, {"access_token": "tok-1", "expires_in": 1800})


ONE_TRANSACTION_PAYLOAD = {
    "data": [{
        "id": "tx-1",
        "attributes": {
            "amount": "-39.60", "executionDate": "2026-09-15", "currency": "EUR",
            "remittanceInformation": "Factuur F-1", "counterpartName": "Kruitbosch",
            "counterpartReference": "NL00RABO0123456789",
        },
    }],
    "links": {},
}


def test_ponto_configured_false_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")
    monkeypatch.setattr(settings, "ponto_client_secret", "")
    assert ponto_configured() is False


def test_default_ponto_host_is_myponto_not_ponto():
    # Real production bug: api.ponto.com is wrong -- the correct host is
    # api.myponto.com. Both settings are overridable via env regardless
    # (that's what makes them settings, not hardcoded constants).
    fields = settings.__class__.model_fields
    assert fields["ponto_api_base_url"].default == "https://api.myponto.com"
    assert fields["ponto_token_url"].default == "https://api.myponto.com/oauth2/token"


def test_run_ponto_sync_noop_when_not_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")
    result = run_ponto_sync(session)
    assert result.new_transactions == 0
    assert result.errors == []


# -- run_ponto_sync (the scheduled job) must NEVER trigger a synchronization --

def test_run_ponto_sync_never_calls_synchronizations_endpoint(session, monkeypatch):
    calls = []

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        calls.append((method, url))
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)
        raise AssertionError(f"unexpected {method} {url} -- run_ponto_sync must be read-only")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)

    assert result.errors == []
    assert result.new_transactions == 1
    assert not any("/synchronizations" in url for _, url in calls)
    tx = session.query(Transaction).one()
    assert tx.external_ref == "ponto:tx-1"
    assert tx.amount_cents == -3960
    assert tx.counterparty_name == "Kruitbosch"
    assert bank_ponto.get_state().last_read_at is not None


def test_run_ponto_sync_records_ponto_reported_synchronized_at_from_meta(session, monkeypatch):
    # Real production bug: this was read from `attributes` and always came
    # back None -- Ponto actually reports it under the JSON:API `meta` object.
    synced_at = "2026-09-29T06:00:00Z"

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}, "meta": {"synchronizedAt": synced_at}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, {"data": [], "links": {}})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    run_ponto_sync(session)

    state = bank_ponto.get_state()
    assert state.last_ponto_synchronized_at is not None
    assert state.last_ponto_synchronized_at.year == 2026


def test_account_synchronized_at_falls_back_to_attributes(monkeypatch):
    # Defensive fallback in case a future API version moves it back/again.
    account = {"attributes": {"synchronizedAt": "2026-01-01T00:00:00Z"}, "meta": {}}
    result = bank_ponto._account_synchronized_at(account)
    assert result is not None
    assert result.year == 2026


def test_account_synchronized_at_none_when_absent_anywhere():
    assert bank_ponto._account_synchronized_at({"attributes": {}, "meta": {}}) is None


def test_run_ponto_sync_skips_transaction_already_imported_from_csv(session, monkeypatch):
    # Same real-world transaction, already booked from a manually-uploaded
    # Rabobank CSV export (with a DIFFERENT description -- the original,
    # narrower dedupe required an exact description match, which is exactly
    # what let 227 real Ponto/CSV pairs slip through as false "new"
    # transactions in production) -- Ponto must not create a duplicate, and
    # must instead reconcile onto the existing CSV row.
    csv_tx = Transaction(
        external_ref="csv:abc",
        booking_date=date(2026, 9, 15),
        amount_cents=-3960,
        counterparty_name="Kruitbosch",
        counterparty_iban="NL00RABO0123456789",
        description="SEPA Overboeking naar Kruitbosch ref 12345",  # differs from Ponto's own wording
        source="csv",
    )
    session.add(csv_tx)
    session.commit()

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)

    assert result.new_transactions == 0
    assert result.matched_existing == 1
    assert session.query(Transaction).count() == 1
    session.refresh(csv_tx)
    assert csv_tx.external_id == "tx-1"


def test_run_ponto_sync_matches_across_a_one_day_date_difference(session, monkeypatch):
    # Ponto's executionDate and Rabobank's own "Datum" column can differ by
    # a day for the same real booking.
    csv_tx = Transaction(
        external_ref="csv:abc", booking_date=date(2026, 9, 14), amount_cents=-3960,
        counterparty_name="Kruitbosch", counterparty_iban="NL00RABO0123456789",
        description="Anders geformuleerd", source="csv",
    )
    session.add(csv_tx)
    session.commit()

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)  # executionDate 2026-09-15
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)
    assert result.matched_existing == 1
    assert result.new_transactions == 0


def test_run_ponto_sync_falls_back_to_name_when_either_side_has_no_iban(session, monkeypatch):
    csv_tx = Transaction(
        external_ref="csv:abc", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Kruitbosch B.V.", counterparty_iban="",  # no IBAN on the CSV side
        description="Anders geformuleerd", source="csv",
    )
    session.add(csv_tx)
    session.commit()

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)  # counterpartName "Kruitbosch"
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)
    assert result.matched_existing == 1


def test_run_ponto_sync_does_not_match_different_counterparty(session, monkeypatch):
    csv_tx = Transaction(
        external_ref="csv:abc", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Iemand Anders", counterparty_iban="NL00RABO0999999999",
        description="Onverwant", source="csv",
    )
    session.add(csv_tx)
    session.commit()

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)
    assert result.matched_existing == 0
    assert result.new_transactions == 1
    assert session.query(Transaction).count() == 2


def test_run_ponto_sync_warns_when_consent_expires_soon(session, monkeypatch):
    expiry = (datetime.utcnow() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {"authorizationExpirationExpectedAt": expiry}}]})
        if url.endswith("/transactions"):
            return FakeResponse(200, {"data": [], "links": {}})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_ponto_sync(session)
    assert result.consent_warning is not None
    assert "verloopt" in result.consent_warning


def test_run_ponto_sync_records_error_on_auth_failure(session, monkeypatch):
    def fake_post(url, data=None, auth=None, timeout=None):
        return FakeResponse(401, text="invalid_client")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)

    result = run_ponto_sync(session)
    assert result.new_transactions == 0
    assert len(result.errors) == 1
    assert bank_ponto.get_state().last_error is not None


def test_token_request_never_sends_a_client_certificate(session, monkeypatch):
    # Per Ponto's docs for a custom integration: Client Credentials only,
    # no mTLS -- requests.post must never be called with a cert kwarg.
    captured = {}

    def fake_post(url, data=None, auth=None, timeout=None, **kwargs):
        captured.update(kwargs)
        return FakeResponse(200, {"access_token": "tok-1", "expires_in": 1800})

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None, **kwargs):
        captured.update(kwargs)
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": []})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", fake_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    run_ponto_sync(session)
    assert "cert" not in captured


# -- run_manual_ponto_refresh ("Nu verversen") IS allowed to synchronize --

def test_manual_refresh_requests_synchronization_with_requester_ip(session, monkeypatch):
    calls = []

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        calls.append((method, url, headers))
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        if url.endswith("/synchronizations"):
            return FakeResponse(200, {"data": {"id": "sync-1"}})
        if "/synchronizations/" in url:
            return FakeResponse(200, {"data": {"attributes": {"status": "success"}}})
        if url.endswith("/transactions"):
            return FakeResponse(200, ONE_TRANSACTION_PAYLOAD)
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_manual_ponto_refresh(session, "203.0.113.42")

    assert result.errors == []
    assert result.new_transactions == 1
    sync_call = next(c for c in calls if c[1].endswith("/synchronizations"))
    assert sync_call[2]["X-Forwarded-For"] == "203.0.113.42"


def test_manual_refresh_requires_a_requester_ip(session, monkeypatch):
    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        if url.endswith("/accounts"):
            return FakeResponse(200, {"data": [{"id": "acc-1", "attributes": {}}]})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(bank_ponto.requests, "post", _fake_token_post)
    monkeypatch.setattr(bank_ponto.requests, "request", fake_request)

    result = run_manual_ponto_refresh(session, "")
    assert len(result.errors) == 1
    assert session.query(Transaction).count() == 0


def test_manual_refresh_noop_when_not_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "ponto_client_id", "")
    result = run_manual_ponto_refresh(session, "203.0.113.42")
    assert result.new_transactions == 0
    assert result.errors


# -- find_matching_transaction (the dedupe rule itself, tested directly) --

def test_find_matching_transaction_scopes_to_the_same_own_account(session):
    from app.bank_ponto import find_matching_transaction

    other_account_tx = Transaction(
        external_ref="csv:a", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Kruitbosch", counterparty_iban="NL00RABO0123456789",
        own_account_iban="NL96RABO0112409008",
    )
    same_account_tx = Transaction(
        external_ref="csv:b", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Kruitbosch", counterparty_iban="NL00RABO0123456789",
        own_account_iban="NL44RABO0199970777",
    )
    session.add_all([other_account_tx, same_account_tx])
    session.commit()

    match = find_matching_transaction(
        session, booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_iban="NL00RABO0123456789", counterparty_name="Kruitbosch",
        own_account_iban="NL44RABO0199970777",
    )
    assert match is not None
    assert match.id == same_account_tx.id


def test_find_matching_transaction_never_returns_a_ponto_sourced_row(session):
    from app.bank_ponto import find_matching_transaction

    session.add(Transaction(
        external_ref="ponto:xyz", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Kruitbosch", counterparty_iban="NL00RABO0123456789", source="ponto",
    ))
    session.commit()

    match = find_matching_transaction(
        session, booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_iban="NL00RABO0123456789", counterparty_name="Kruitbosch",
    )
    assert match is None


def test_find_matching_transaction_ignores_already_reconciled_rows(session):
    from app.bank_ponto import find_matching_transaction

    session.add(Transaction(
        external_ref="csv:a", booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_name="Kruitbosch", counterparty_iban="NL00RABO0123456789",
        external_id="tx-already-claimed",
    ))
    session.commit()

    match = find_matching_transaction(
        session, booking_date=date(2026, 9, 15), amount_cents=-3960,
        counterparty_iban="NL00RABO0123456789", counterparty_name="Kruitbosch",
    )
    assert match is None
