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

Tested against a real Integration on production: `list_accounts()`/
`fetch_transactions()` work against `https://api.myponto.com` (NOT
`api.ponto.com` -- an earlier, wrong assumption; both are overridable via
PONTO_API_BASE_URL/PONTO_TOKEN_URL regardless). Still unverified: the exact
header Ponto expects to identify the end user's IP on a manual
synchronization request (see request_manual_synchronization) -- sent as
"X-Forwarded-For" here as the closest convention; adjust if Ponto's error
response says otherwise, the same "try it, read the real error, adjust"
loop that caught the host mistake.

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
from app.matcher import _normalize_company_name
from app.models import Transaction

# Consent-expiry warning window -- Ponto surfaces an expected expiry per
# account (see _account_consent_expiry); verify the exact attribute name
# against your own account, since this is one of the pieces not yet tried
# against a real Integration (see module docstring).
CONSENT_EXPIRY_WARNING_DAYS = 14

# A CSV/MT940-imported line and the SAME transaction as later fetched from
# Ponto don't always land on the identical calendar date -- Ponto's
# executionDate/valueDate and Rabobank's own "Datum" column can differ by a
# day for the same booking. Matched within this window rather than exactly.
DEDUPE_DATE_TOLERANCE_DAYS = 1


class PontoError(RuntimeError):
    pass


def ponto_configured() -> bool:
    return bool(settings.ponto_client_id and settings.ponto_client_secret)


@dataclass
class PontoSyncResult:
    new_transactions: int = 0
    # A Ponto-fetched transaction that turned out to be the SAME real-world
    # transaction as an existing (CSV/MT940-imported, or earlier Ponto) row
    # -- no new Transaction row created; the existing one just gets its
    # external_id filled in (see find_matching_transaction) so it's
    # recognised on every later sync without ever creating a duplicate.
    matched_existing: int = 0
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
            settings.ponto_token_url,
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
    url = path if path.startswith("http") else settings.ponto_api_base_url + path
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
    """Corrected against a real account: this is NOT in `attributes` (that
    read back None on production) -- Ponto reports it in the JSON:API
    `meta` object instead. If a future account/API version moves it again,
    falls back to `attributes.synchronizedAt` just in case."""
    raw = (account.get("meta") or {}).get("synchronizedAt") or (account.get("attributes") or {}).get("synchronizedAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _account_own_iban(account: dict) -> str:
    """This account's OWN IBAN (e.g. "NL44RABO0199970777") -- used to scope
    a Ponto-vs-CSV duplicate match to the same account (see
    find_matching_transaction). `attributes.reference` per Ponto's docs on
    the Account resource; unverified against production like the rest of
    this module's field names (see module docstring) -- an empty result
    here just means the own-account check in find_matching_transaction is
    skipped for that sync, not a hard failure."""
    return ((account.get("attributes") or {}).get("reference") or "").strip()


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
        path = (payload.get("links") or {}).get("next")  # a full URL -- _request() passes it through as-is
    return results


def _normalize_iban(iban: str) -> str:
    return (iban or "").replace(" ", "").upper()


def find_matching_transaction(
    session: Session,
    *,
    booking_date: date,
    amount_cents: int,
    counterparty_iban: str,
    counterparty_name: str,
    own_account_iban: str = "",
    exclude_id: int | None = None,
) -> Transaction | None:
    """The SAME real-world bank transaction, already stored under a
    different identity -- e.g. a CSV-imported line whose description reads
    differently from how Ponto phrases the same payment (the original,
    narrower dedupe only matched on an EXACT description too, which is why
    227 Ponto/CSV pairs slipped through as false "new" transactions in
    production).

    Matches on: same amount exactly; booking_date within
    DEDUPE_DATE_TOLERANCE_DAYS (Ponto's executionDate/valueDate vs.
    Rabobank's own "Datum" column can differ by a day for one real booking);
    same own account IF both sides know it (an empty own_account_iban --
    true for every pre-existing row, that column being new -- is treated as
    "unknown", never as a mismatch); and, for the counterparty, an exact
    IBAN match when BOTH sides have one, falling back to a normalized-name
    comparison whenever either side's IBAN is missing.

    Used both by the Ponto sync itself (so a transaction already reconciled
    this way is never fetched again as a duplicate next time -- its
    external_id gets filled in) and by scripts/cleanup_ponto_duplicates.py
    to pair up and merge the ones that already slipped through. Both
    callers always search FROM a Ponto-side transaction outward, so
    candidates are restricted to non-Ponto rows -- this must never pair two
    Ponto rows with each other (a real risk for the backlog of pre-fix
    duplicates, which all have an empty external_id just like a genuine
    unreconciled CSV row does)."""
    candidates = session.scalars(
        select(Transaction).where(
            Transaction.amount_cents == amount_cents,
            Transaction.booking_date >= booking_date - timedelta(days=DEDUPE_DATE_TOLERANCE_DAYS),
            Transaction.booking_date <= booking_date + timedelta(days=DEDUPE_DATE_TOLERANCE_DAYS),
            Transaction.external_id == "",  # already-reconciled rows are never matched a second time
            Transaction.source != "ponto",
        )
    )
    norm_cp_iban = _normalize_iban(counterparty_iban)
    norm_own_iban = _normalize_iban(own_account_iban)
    norm_name = _normalize_company_name(counterparty_name)

    for candidate in candidates:
        if exclude_id is not None and candidate.id == exclude_id:
            continue
        candidate_own_iban = _normalize_iban(candidate.own_account_iban)
        if norm_own_iban and candidate_own_iban and candidate_own_iban != norm_own_iban:
            continue
        candidate_cp_iban = _normalize_iban(candidate.counterparty_iban)
        if norm_cp_iban and candidate_cp_iban:
            if candidate_cp_iban != norm_cp_iban:
                continue
        else:
            if _normalize_company_name(candidate.counterparty_name) != norm_name:
                continue
        return candidate
    return None


def _import_transactions(
    session: Session, account_id: str, own_account_iban: str, since: date,
    existing_refs: set[str], result: PontoSyncResult,
) -> None:
    transactions = fetch_transactions(account_id, since)
    for tx in transactions:
        external_ref = f"ponto:{tx.ponto_id}"
        if external_ref in existing_refs:
            continue
        existing = session.scalars(select(Transaction).where(Transaction.external_id == tx.ponto_id)).first()
        if existing is not None:
            continue  # already reconciled to an existing row on an earlier sync

        match = find_matching_transaction(
            session,
            booking_date=tx.booking_date,
            amount_cents=tx.amount_cents,
            counterparty_iban=tx.counterparty_iban,
            counterparty_name=tx.counterparty_name,
            own_account_iban=own_account_iban,
        )
        if match is not None:
            match.external_id = tx.ponto_id
            session.flush()  # so a later tx in this same batch never re-matches this row
            result.matched_existing += 1
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
                own_account_iban=own_account_iban,
                source="ponto",
                external_id=tx.ponto_id,
            )
        )
        existing_refs.add(external_ref)
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
            _import_transactions(session, account_id, _account_own_iban(account), since, existing_refs, result)
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
    since = date.today() - timedelta(days=settings.sync_lookback_days)

    for account in accounts:
        account_id = account.get("id")
        if not account_id:
            continue
        try:
            sync_id = request_manual_synchronization(account_id, requester_ip)
            wait_for_synchronization(sync_id)
            _import_transactions(session, account_id, _account_own_iban(account), since, existing_refs, result)
        except PontoError as exc:
            result.errors.append(str(exc))
            continue

    _state.last_read_at = datetime.now(timezone.utc)
    _state.last_error = result.errors[0] if result.errors else None

    _apply_post_import(session, result)
    return result
