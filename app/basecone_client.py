"""Client for pulling bank statement lines out of Basecone.

IMPORTANT: Basecone's public API details (exact endpoint paths, auth flow
specifics, and JSON field names) depend on your account/API product and
can change. The OAuth2 client-credentials flow and endpoint below are the
commonly documented shape, but you should verify them against the API
documentation/support contact you receive when registering for Basecone
API access, and adjust `settings.basecone_*` (or the FIELD_MAP below) to
match rather than assuming this is exact out of the box.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

import requests

from app.config import settings

# Maps our internal field names to the keys we expect in a single bank
# statement line as returned by Basecone. If your account's API returns
# different key names, change the values here -- nothing else needs to
# change.
FIELD_MAP = {
    "id": "id",
    "date": "bookingDate",
    "amount": "amount",  # decimal string/number, negative = money out
    "currency": "currency",
    "description": "description",
    "counterparty_name": "counterPartyName",
    "counterparty_iban": "counterPartyIban",
    "reference": "paymentReference",
}


class BaseconeAuthError(RuntimeError):
    pass


class BaseconeApiError(RuntimeError):
    pass


@dataclass
class BankTransactionData:
    basecone_id: str
    booking_date: date
    amount_cents: int
    currency: str
    description: str
    counterparty_name: str
    counterparty_iban: str
    reference: str
    raw: dict


class BaseconeClient:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _authenticate(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at - 30:
            return self._access_token

        if not settings.basecone_client_id or not settings.basecone_client_secret:
            raise BaseconeAuthError(
                "Basecone client_id/client_secret zijn niet ingesteld. "
                "Vul BASECONE_CLIENT_ID en BASECONE_CLIENT_SECRET in via .env "
                "(deze krijg je bij het registreren voor Basecone API-toegang)."
            )

        response = requests.post(
            settings.basecone_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.basecone_client_id,
                "client_secret": settings.basecone_client_secret,
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise BaseconeAuthError(
                f"Basecone authenticatie mislukt ({response.status_code}): {response.text}"
            )

        payload = response.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        return self._access_token

    def fetch_bank_transactions(self, since: date) -> list[BankTransactionData]:
        """Fetch bank statement lines booked on or after `since`."""
        if not settings.basecone_administration_id:
            raise BaseconeAuthError(
                "BASECONE_ADMINISTRATION_ID is niet ingesteld -- dit identificeert "
                "je administratie/bedrijf binnen Basecone."
            )

        token = self._authenticate()
        path = settings.basecone_bank_transactions_path.format(
            administration_id=settings.basecone_administration_id
        )
        url = settings.basecone_api_base_url.rstrip("/") + path

        results: list[BankTransactionData] = []
        page = 1
        while True:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"fromDate": since.isoformat(), "page": page, "pageSize": 200},
                timeout=30,
            )
            if response.status_code != 200:
                raise BaseconeApiError(
                    f"Basecone gaf een fout terug ({response.status_code}) bij het "
                    f"ophalen van afschriften: {response.text}"
                )

            payload = response.json()
            # Basecone (like most paged APIs) may wrap results in an "items"/"data"
            # envelope, or return a bare list depending on account/version.
            items = payload.get("items") if isinstance(payload, dict) else payload
            if items is None:
                items = payload.get("data", []) if isinstance(payload, dict) else []

            if not items:
                break

            for raw in items:
                results.append(self._parse_transaction(raw))

            has_more = payload.get("hasMore") if isinstance(payload, dict) else False
            if not has_more:
                break
            page += 1

        return results

    @staticmethod
    def _parse_transaction(raw: dict) -> BankTransactionData:
        def get(field: str):
            key = FIELD_MAP[field]
            if key not in raw:
                raise BaseconeApiError(
                    f"Verwacht veld '{key}' ontbreekt in Basecone-response voor '{field}'. "
                    "Controleer FIELD_MAP in app/basecone_client.py tegen de echte "
                    "Basecone API-documentatie."
                )
            return raw[key]

        booking_date_raw = get("date")
        if isinstance(booking_date_raw, str):
            booking_date = datetime.fromisoformat(booking_date_raw.replace("Z", "+00:00")).date()
        else:
            booking_date = booking_date_raw

        amount_raw = get("amount")
        amount_cents = round(float(amount_raw) * 100)

        return BankTransactionData(
            basecone_id=str(get("id")),
            booking_date=booking_date,
            amount_cents=amount_cents,
            currency=raw.get(FIELD_MAP["currency"], "EUR") or "EUR",
            description=raw.get(FIELD_MAP["description"], "") or "",
            counterparty_name=raw.get(FIELD_MAP["counterparty_name"], "") or "",
            counterparty_iban=raw.get(FIELD_MAP["counterparty_iban"], "") or "",
            reference=raw.get(FIELD_MAP["reference"], "") or "",
            raw=raw,
        )
