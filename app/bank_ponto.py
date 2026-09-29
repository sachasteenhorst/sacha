"""Ponto Connect bank sync -- an alternative to manually uploading a
Rabobank CSV/CAMT.053/MT940 export (see app/bank_import.py): with a Ponto
"Integration" (OAuth2 Client Credentials, created once in the Ponto
dashboard for your OWN account -- this is not the multi-tenant PSD2 AISP
consent flow), new transactions can be fetched automatically every few
hours.

IMPORTANT -- same caveat as app/bank_import.py and app/basecone_client.py:
this is built from Ponto Connect's publicly documented JSON:API shape
(https://documentation.ponto.com), but has not been exercised against a
real Ponto account yet. Endpoint paths, the OAuth2 grant details, and the
exact attribute names on the account/transaction resources may need small
adjustments once tried against your own Integration -- the error messages
here are written to say exactly what came back from Ponto so that's a quick
fix, the same "try it, read the real error, adjust" loop used for the bank
CSV parsers.

Off entirely (never called, never scheduled with an effect) until both
PONTO_CLIENT_ID and PONTO_CLIENT_SECRET are set.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Transaction

PONTO_API_BASE_URL = "https://api.ponto.com"
PONTO_TOKEN_URL = "https://authorization.myponto.com/oauth2/token"

# Consent-expiry warning window -- Ponto surfaces an expected expiry per
# account (see _account_consent_expiry); verify the exact attribute name
# against your own account, since this is one of the pieces not yet tried
# against a real Integration (see module docstring).
CONSENT_EXPIRY_WARNING_DAYS = 14


class PontoError(RuntimeError):
    pass


def ponto_configured() -> bool:
    return bool(settings.ponto_client_id and settings.ponto_client_secret)


@dataclass
class PontoSyncResult:
    new_transactions: int = 0
    errors: list[str] = field(default_factory=list)
    consent_warning: str | None = None


@dataclass
class PontoSyncState:
    """In-memory, like app/sync_state.py -- only needed for the dashboard's
    "Bank: Ponto" status chip; resets harmlessly on a restart."""
    last_synced_at: datetime | None = None
    last_error: str | None = None


_state = PontoSyncState()


def get_state() -> PontoSyncState:
    return _state


class _PontoAuth:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 30:
            return self._access_token
        if not ponto_configured():
            raise PontoError("PONTO_CLIENT_ID/PONTO_CLIENT_SECRET zijn niet ingesteld.")

        response = requests.post(
            PONTO_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(settings.ponto_client_id, settings.ponto_client_secret),
            cert=_client_cert(),
            timeout=30,
        )
        if response.status_code != 200:
            raise PontoError(f"Ponto-token ophalen mislukt ({response.status_code}): {response.text}")
        payload = response.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        return self._access_token


_auth = _PontoAuth()


def _client_cert() -> tuple[str, str] | None:
    """mTLS client certificate, only if your Integration requires one (most
    Client Credentials integrations don't). An encrypted private key
    (PONTO_KEY_PASSWORD set) isn't supported here yet -- `requests` has no
    built-in way to pass a key passphrase, so for now decrypt the key file
    once yourself (e.g. `openssl rsa -in key.pem -out key-plain.pem`) and
    point PONTO_KEY_PATH at the decrypted copy."""
    if not (settings.ponto_cert_path and settings.ponto_key_path):
        return None
    if settings.ponto_key_password:
        raise PontoError(
            "PONTO_KEY_PASSWORD wordt nog niet ondersteund -- ontsleutel de sleutel eenmalig "
            "zelf (bijv. `openssl rsa -in key.pem -out key-plain.pem`) en zet PONTO_KEY_PATH "
            "op het ontsleutelde bestand."
        )
    return (settings.ponto_cert_path, settings.ponto_key_path)


def _signature_headers(method: str, path: str, body: str = "") -> dict:
    """HTTP message signature headers -- TODO: only some Ponto Integration
    types require this (check your Integration's settings in the Ponto
    dashboard); the exact signing scheme isn't implemented yet since
    guessing at a cryptographic signature format for a real bank API would
    be worse than leaving it a visible TODO. Currently a no-op unless
    PONTO_SIGNATURE_KEY_ID is set, in which case it raises so a
    misconfiguration is never silently ignored."""
    if not settings.ponto_signature_key_id:
        return {}
    raise PontoError(
        "PONTO_SIGNATURE_KEY_ID is ingesteld maar HTTP-signatures zijn nog niet "
        "geimplementeerd (zie app/bank_ponto.py._signature_headers). Neem de "
        "signature-eisen uit je Ponto-dashboard door voordat je dit gebruikt."
    )


def _request(method: str, path: str, **kwargs) -> dict:
    token = _auth.token()
    url = PONTO_API_BASE_URL + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"}
    headers.update(_signature_headers(method, path))
    response = requests.request(method, url, headers=headers, cert=_client_cert(), timeout=30, **kwargs)
    if response.status_code >= 300:
        raise PontoError(f"Ponto API-fout ({response.status_code}) bij {method} {path}: {response.text}")
    return response.json() if response.content else {}


def list_accounts() -> list[dict]:
    """Raw JSON:API resource objects (data[].attributes) -- one per bank
    account this Integration has access to."""
    payload = _request("GET", "/accounts")
    return payload.get("data", [])


def _account_consent_expiry(account: dict) -> date | None:
    raw = (account.get("attributes") or {}).get("authorizationExpirationExpectedAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def request_synchronization(account_id: str) -> str:
    """Asks Ponto to refresh this account's transactions from the bank right
    now. Returns the synchronization id."""
    payload = _request(
        "POST", "/synchronizations",
        json={"data": {"type": "synchronization", "attributes": {
            "resourceType": "account", "resourceId": account_id, "subtype": "accountTransactions",
        }}},
    )
    return payload["data"]["id"]


def _synchronization_status(sync_id: str) -> str:
    payload = _request("GET", f"/synchronizations/{sync_id}")
    return (payload.get("data", {}).get("attributes") or {}).get("status", "")


def wait_for_synchronization(sync_id: str, max_wait_seconds: int = 20, poll_interval: float = 2.0) -> bool:
    """Polls briefly for the synchronization to finish -- Ponto processes it
    asynchronously, but a scheduled job every 4 hours doesn't need to block
    long: if it's not done within max_wait_seconds, the next scheduled run
    picks up whatever landed in the meantime anyway (fetch_transactions
    below just reads whatever Ponto currently has cached, sync or not)."""
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        status = _synchronization_status(sync_id)
        if status in ("success", "error"):
            return status == "success"
        time.sleep(poll_interval)
    return False


@dataclass
class PontoTransaction:
    ponto_id: str
    booking_date: date
    amount_cents: int
    currency: str
    description: str
    counterparty_name: str
    counterparty_iban: str
    raw: dict


def _parse_ponto_transaction(row: dict) -> PontoTransaction | None:
    attrs = row.get("attributes") or {}
    amount = attrs.get("amount")
    exec_date_raw = attrs.get("executionDate") or attrs.get("valueDate")
    if amount is None or not exec_date_raw:
        return None
    amount_cents = round(float(amount) * 100)
    booking_date = date.fromisoformat(str(exec_date_raw)[:10])
    return PontoTransaction(
        ponto_id=row.get("id", ""),
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=attrs.get("currency", "EUR"),
        description=attrs.get("remittanceInformation") or attrs.get("description") or "",
        counterparty_name=attrs.get("counterpartName") or "",
        counterparty_iban=attrs.get("counterpartReference") or "",
        raw=attrs,
    )


def fetch_transactions(account_id: str, since: date) -> list[PontoTransaction]:
    since_iso = f"{since.isoformat()}T00:00:00.000Z"
    path = f"/accounts/{account_id}/transactions"
    params = {"page[limit]": "100", "filter[executionDate][gte]": since_iso}
    results: list[PontoTransaction] = []
    while path:
        payload = _request("GET", path, params=params)
        params = None  # the "next" link already carries the query string
        for row in payload.get("data", []):
            parsed = _parse_ponto_transaction(row)
            if parsed is not None:
                results.append(parsed)
        next_link = (payload.get("links") or {}).get("next")
        path = next_link.replace(PONTO_API_BASE_URL, "") if next_link else None
    return results


def _existing_dedupe_keys(session: Session) -> set[tuple]:
    """Same identity Sacha's own CSV uploads use to skip a re-uploaded
    overlapping period (datum+bedrag+tegenrekening+omschrijving) -- so a
    Ponto-fetched transaction that was ALSO already imported by hand from a
    downloaded statement doesn't get booked twice."""
    keys = set()
    for booking_date, amount_cents, iban, description in session.execute(
        select(Transaction.booking_date, Transaction.amount_cents, Transaction.counterparty_iban, Transaction.description)
    ):
        keys.add((booking_date, amount_cents, (iban or "").strip().upper(), (description or "").strip()))
    return keys


def run_ponto_sync(session: Session) -> PontoSyncResult:
    result = PontoSyncResult()
    if not ponto_configured():
        return result

    try:
        accounts = list_accounts()
    except PontoError as exc:
        result.errors.append(str(exc))
        _state.last_error = str(exc)
        return result

    existing_refs = set(session.scalars(select(Transaction.external_ref)))
    dedupe_keys = _existing_dedupe_keys(session)
    since = date.today() - timedelta(days=settings.sync_lookback_days)

    for account in accounts:
        account_id = account.get("id")
        if not account_id:
            continue

        expiry = _account_consent_expiry(account)
        if expiry is not None and (expiry - date.today()).days <= CONSENT_EXPIRY_WARNING_DAYS:
            result.consent_warning = (
                f"De Ponto-toestemming voor rekening {account_id} verloopt op "
                f"{expiry.strftime('%d-%m-%Y')} -- verleng 'm in het Ponto-dashboard."
            )

        try:
            sync_id = request_synchronization(account_id)
            wait_for_synchronization(sync_id)
            transactions = fetch_transactions(account_id, since)
        except PontoError as exc:
            result.errors.append(str(exc))
            continue

        for tx in transactions:
            external_ref = f"ponto:{tx.ponto_id}"
            if external_ref in existing_refs:
                continue
            dedupe_key = (tx.booking_date, tx.amount_cents, tx.counterparty_iban.strip().upper(), tx.description.strip())
            if dedupe_key in dedupe_keys:
                continue
            session.add(
                Transaction(
                    external_ref=external_ref,
                    booking_date=tx.booking_date,
                    amount_cents=tx.amount_cents,
                    currency=tx.currency,
                    description=tx.description,
                    counterparty_name=tx.counterparty_name,
                    counterparty_iban=tx.counterparty_iban,
                    reference=tx.ponto_id,
                    raw_data=tx.raw,
                )
            )
            existing_refs.add(external_ref)
            dedupe_keys.add(dedupe_key)
            result.new_transactions += 1

    _state.last_synced_at = datetime.now(timezone.utc)
    _state.last_error = result.errors[0] if result.errors else None

    if result.new_transactions:
        session.flush()
        from app.basecone_forward import auto_forward_new_invoices
        from app.matcher import run_matching
        from app.rules import apply_rules

        run_matching(session)
        apply_rules(session)
        auto_forward_new_invoices(session)

    return result
