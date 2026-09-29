"""Ponto Connect bank sync -- an alternative to manually uploading a
Rabobank CSV/CAMT.053/MT940 export (see app/bank_import.py).

Per Ponto's own documentation for a custom/private integration:

- Authentication is OAuth2 Client Credentials ONLY -- client ID + secret,
  Basic Auth against the token endpoint, token valid ~30 minutes. No client
  certificate, no HTTP message signing.
- Ponto synchronizes each connected account with the bank itself, on its
  own schedule (about 4x/day) -- this app never needs to (and must not)
  trigger that routinely.
- Triggering a synchronization BY HAND (POST /synchronizations) is only
  allowed while the actual end user is present and it's done from their
  real IP address -- doing this from an unattended background job violates
  Ponto's terms and risks the Integration being blocked. So: the scheduled
  job here (run_ponto_sync, called every few hours by app/scheduler.py) is
  STRICTLY READ-ONLY (GET /accounts, GET /accounts/{id}/transactions) and
  never calls /synchronizations. Only the dashboard's "Nu verversen" button
  (see app/main.py) may request one, and only with the real client IP
  attached (see request_manual_synchronization).
- Only booked transactions are ever fetched/stored (GET .../transactions);
  a pending-transactions endpoint, if Ponto exposes one, is deliberately
  never called -- a pending amount can still change before it books.

IMPORTANT -- same caveat as app/bank_import.py and app/basecone_client.py:
this is built from Ponto Connect's publicly documented JSON:API shape
(https://documentation.ponto.com), but has not been exercised against a
real Ponto account yet. Endpoint paths and exact attribute names on the
account/transaction resources may need small adjustments once tried
against your own Integration -- the error messages here are written to say
exactly what came back from Ponto so that's a quick fix, the same "try it,
read the real error, adjust" loop used for the bank CSV parsers. The exact
header Ponto expects to identify the end user's IP on a manual
synchronization request (see request_manual_synchronization) is one such
unverified detail -- sent as "X-Forwarded-For" here as the closest
convention; adjust if Ponto's error response says otherwise.

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
PONTO_TOKEN_URL = "https://api.ponto.com/oauth2/token"

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
    last_read_at: datetime | None = None
    # The most recent "synchronizedAt" Ponto itself reported across all
    # accounts -- i.e. when PONTO last actually talked to the bank, as
    # opposed to last_read_at (when THIS app last read Ponto's API).
    last_ponto_synchronized_at: datetime | None = None
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
            timeout=30,
        )
        if response.status_code != 200:
            raise PontoError(f"Ponto-token ophalen mislukt ({response.status_code}): {response.text}")
        payload = response.json()
        self._access_token = payload["access_token"]
        # Ponto tokens are valid ~30 minutes; expires_in from the response
        # is authoritative when present.
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 1800))
        return self._access_token


_auth = _PontoAuth()


def _request(method: str, path: str, extra_headers: dict | None = None, **kwargs) -> dict:
    token = _auth.token()
    url = PONTO_API_BASE_URL + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"}
    if extra_headers:
        headers.update(extra_headers)
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
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


def _account_synchronized_at(account: dict) -> datetime | None:
    raw = (account.get("attributes") or {}).get("synchronizedAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def request_manual_synchronization(account_id: str, requester_ip: str) -> str:
    """Asks Ponto to refresh this account's transactions from the bank right
    now -- ONLY ever call this from a request handler acting on an actual
    dashboard click (see app/main.py's "Nu verversen" button), carrying the
    real end user's IP address, per Ponto's terms for manual synchronization
    requests. NEVER call this from the scheduler (see module docstring)."""
    if not requester_ip:
        raise PontoError("Geen client-IP bekend -- handmatige synchronisatie kan niet zonder.")
    payload = _request(
        "POST", "/synchronizations",
        extra_headers={"X-Forwarded-For": requester_ip},
        json={"data": {"type": "synchronization", "attributes": {
            "resourceType": "account", "resourceId": account_id, "subtype": "accountTransactions",
        }}},
    )
    return payload["data"]["id"]


def _synchronization_status(sync_id: str) -> str:
    payload = _request("GET", f"/synchronizations/{sync_id}")
    return (payload.get("data", {}).get("attributes") or {}).get("status", "")


def wait_for_synchronization(sync_id: str, max_wait_seconds: int = 20, poll_interval: float = 2.0) -> bool:
    """Polls briefly for a manually-requested synchronization to finish --
    only ever used right after request_manual_synchronization, from the
    dashboard button's request handler; never from the scheduler."""
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
    """Booked transactions only -- deliberately never calls a
    pending-transactions endpoint (see module docstring): a pending amount
    can still change before the bank actually books it."""
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


def _import_transactions(
    session: Session, account_id: str, since: date,
    existing_refs: set[str], dedupe_keys: set[tuple], result: PontoSyncResult,
) -> None:
    transactions = fetch_transactions(account_id, since)
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


def _apply_post_import(session: Session, result: PontoSyncResult) -> None:
    if not result.new_transactions:
        return
    session.flush()
    from app.basecone_forward import auto_forward_new_invoices
    from app.matcher import run_matching
    from app.rules import apply_rules

    run_matching(session)
    apply_rules(session)
    auto_forward_new_invoices(session)


def run_ponto_sync(session: Session) -> PontoSyncResult:
    """READ-ONLY: lists accounts and fetches already-booked transactions.
    Never triggers a Ponto synchronization (see module docstring) -- this is
    what app/scheduler.py calls periodically."""
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
    latest_ponto_sync: datetime | None = None

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
        synced_at = _account_synchronized_at(account)
        if synced_at is not None and (latest_ponto_sync is None or synced_at > latest_ponto_sync):
            latest_ponto_sync = synced_at

        try:
            _import_transactions(session, account_id, since, existing_refs, dedupe_keys, result)
        except PontoError as exc:
            result.errors.append(str(exc))
            continue

    _state.last_read_at = datetime.now(timezone.utc)
    if latest_ponto_sync is not None:
        _state.last_ponto_synchronized_at = latest_ponto_sync
    _state.last_error = result.errors[0] if result.errors else None

    _apply_post_import(session, result)
    return result


def run_manual_ponto_refresh(session: Session, requester_ip: str) -> PontoSyncResult:
    """The dashboard's "Nu verversen" button: explicitly requests a fresh
    Ponto synchronization (with the real end user's IP attached, per
    Ponto's terms), waits briefly, then reads whatever landed -- the ONLY
    code path allowed to call request_manual_synchronization."""
    result = PontoSyncResult()
    if not ponto_configured():
        result.errors.append("Ponto is niet ingesteld.")
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
        try:
            sync_id = request_manual_synchronization(account_id, requester_ip)
            wait_for_synchronization(sync_id)
            _import_transactions(session, account_id, since, existing_refs, dedupe_keys, result)
        except PontoError as exc:
            result.errors.append(str(exc))
            continue

    _state.last_read_at = datetime.now(timezone.utc)
    _state.last_error = result.errors[0] if result.errors else None

    _apply_post_import(session, result)
    return result
