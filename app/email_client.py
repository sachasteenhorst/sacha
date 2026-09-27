"""Fetches invoice PDFs from a mailbox via the Microsoft Graph API and
extracts the fields we need for matching (amount, invoice date, invoice
number, supplier).

Why Graph and not plain IMAP: Microsoft 365 disables classic IMAP
username/password ("Basic Auth") login by default for business mailboxes.
Graph with an Azure AD app registration (OAuth2 client-credentials) is the
supported way to read mail unattended. See README for the exact Azure
setup steps, including the Application Access Policy that restricts this
app to a single mailbox (application-level Mail.Read otherwise grants
access to every mailbox in the tenant).

PDF text extraction is heuristic (regex over the extracted text). It will
not get every invoice layout right -- fields it can't find are left empty
and the invoice still shows up in the dashboard for you to fill in by hand.
"""
from __future__ import annotations

import os
import re
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime

import pdfplumber
import requests

from app.config import settings

INVOICE_DIR = "./data/invoices"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}

AMOUNT_LABEL_RE = re.compile(
    r"(?:totaal(?:bedrag)?|te betalen|eindtotaal|invoice total|amount due)\D{0,15}"
    r"(?:€|eur)?\s*([\d.,]+)",
    re.IGNORECASE,
)
ANY_AMOUNT_RE = re.compile(r"€\s*([\d]{1,3}(?:[.,]\d{3})*[.,]\d{2})")
INVOICE_NUMBER_RE = re.compile(
    r"(?:factuurnummer|factuur\s*nr\.?|invoice\s*(?:number|no)\.?)\s*[:#]?\s*([A-Za-z0-9\-\/]+)",
    re.IGNORECASE,
)
DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
DATE_DUTCH_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(DUTCH_MONTHS.keys()) + r")\s+(\d{4})\b", re.IGNORECASE
)
INVOICE_DATE_LABEL_RE = re.compile(r"factuurdatum\D{0,10}", re.IGNORECASE)


class GraphAuthError(RuntimeError):
    pass


class GraphApiError(RuntimeError):
    pass


@dataclass
class InvoiceAttachment:
    email_message_id: str
    attachment_filename: str
    email_subject: str
    email_from: str
    received_at: datetime
    pdf_path: str
    extracted_text: str
    invoice_number: str = ""
    invoice_date: date | None = None
    supplier_name: str = ""
    amount_cents: int | None = None
    currency: str = "EUR"


def _parse_amount_to_cents(text: str) -> int | None:
    match = AMOUNT_LABEL_RE.search(text)
    candidates = []
    if match:
        candidates.append(match.group(1))
    all_amounts = ANY_AMOUNT_RE.findall(text)
    candidates.extend(all_amounts)

    best_cents = None
    for raw in candidates:
        normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
        try:
            value = float(normalized)
        except ValueError:
            continue
        cents = round(value * 100)
        # Prefer the label-matched amount; otherwise take the largest amount
        # found (invoice totals are usually the largest number on the page).
        if best_cents is None or cents > best_cents:
            best_cents = cents
    return best_cents


def _parse_invoice_date(text: str) -> date | None:
    label_match = INVOICE_DATE_LABEL_RE.search(text)
    search_window = text[label_match.end():label_match.end() + 30] if label_match else text

    numeric = DATE_NUMERIC_RE.search(search_window)
    if numeric:
        day, month, year = (int(g) for g in numeric.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    dutch = DATE_DUTCH_RE.search(search_window)
    if dutch:
        day = int(dutch.group(1))
        month = DUTCH_MONTHS[dutch.group(2).lower()]
        year = int(dutch.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


def _extract_fields(text: str, email_from: str) -> dict:
    invoice_number_match = INVOICE_NUMBER_RE.search(text)
    return {
        "invoice_number": invoice_number_match.group(1) if invoice_number_match else "",
        "invoice_date": _parse_invoice_date(text),
        "amount_cents": _parse_amount_to_cents(text),
        # Best-effort: the sender's display name/address is usually the supplier.
        "supplier_name": email_from,
    }


# Mail.Read.Shared is a Delegated (not Application) permission -- it lets a
# signed-in user's app read mail in any mailbox that user has been given
# "Full Access" delegate rights to (e.g. a shared mailbox like info@...),
# without requiring a Global Administrator to grant tenant-wide consent.
# The trade-off: there's no unattended client-credentials login, so a human
# has to sign in once (via scripts/graph_login.py) to produce a refresh
# token, which this class then uses to keep getting new access tokens.
GRAPH_SCOPES = "https://graph.microsoft.com/Mail.Read.Shared offline_access"
REFRESH_TOKEN_FILE = "./data/graph_refresh_token.txt"


class _GraphAuth:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def _read_refresh_token() -> str | None:
        if os.path.exists(REFRESH_TOKEN_FILE):
            with open(REFRESH_TOKEN_FILE) as fh:
                token = fh.read().strip()
                return token or None
        return None

    @staticmethod
    def _write_refresh_token(token: str) -> None:
        os.makedirs(os.path.dirname(REFRESH_TOKEN_FILE), exist_ok=True)
        with open(REFRESH_TOKEN_FILE, "w") as fh:
            fh.write(token)

    def token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 30:
            return self._access_token

        if not (settings.graph_tenant_id and settings.graph_client_id):
            raise GraphAuthError(
                "Microsoft Graph is niet geconfigureerd. Vul GRAPH_TENANT_ID "
                "en GRAPH_CLIENT_ID in via .env (zie README)."
            )

        refresh_token = self._read_refresh_token()
        if not refresh_token:
            raise GraphAuthError(
                "Nog niet ingelogd bij Microsoft Graph. Draai eenmalig "
                "`python3 scripts/graph_login.py` en volg de instructies "
                "(zie README)."
            )

        # The app registration is a public client ("Allow public client
        # flows" = Yes, required for the device code login in
        # scripts/graph_login.py) -- Azure AD rejects a client_secret on
        # these token requests for a public client, so it's deliberately
        # left out here.
        token_url = f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": settings.graph_client_id,
            "refresh_token": refresh_token,
            "scope": GRAPH_SCOPES,
        }

        response = requests.post(token_url, data=data, timeout=30)
        if response.status_code != 200:
            raise GraphAuthError(
                f"Microsoft Graph token vernieuwen mislukt ({response.status_code}): "
                f"{response.text}. Mogelijk moet je opnieuw inloggen via "
                "scripts/graph_login.py."
            )

        payload = response.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        # Microsoft rotates the refresh token on every use -- persist the new
        # one immediately or the next refresh will fail with an invalid token.
        if "refresh_token" in payload:
            self._write_refresh_token(payload["refresh_token"])
        return self._access_token


_auth = _GraphAuth()


def _graph_get(url: str, params: dict | None = None) -> dict:
    token = _auth.token()
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    if response.status_code != 200:
        raise GraphApiError(f"Graph API-fout ({response.status_code}) bij {url}: {response.text}")
    return response.json()


def _list_messages_since(since: date) -> list[dict]:
    if not settings.graph_mailbox:
        raise GraphAuthError(
            "GRAPH_MAILBOX is niet ingesteld -- dit is het mailadres van de "
            "mailbox waar facturen binnenkomen."
        )

    since_iso = f"{since.isoformat()}T00:00:00Z"
    url = (
        f"{GRAPH_BASE_URL}/users/{settings.graph_mailbox}/mailFolders/"
        f"{settings.graph_mail_folder}/messages"
    )
    params = {
        "$filter": f"receivedDateTime ge {since_iso} and hasAttachments eq true",
        "$select": "id,subject,from,receivedDateTime,hasAttachments",
        "$top": "50",
    }

    messages: list[dict] = []
    while url:
        payload = _graph_get(url, params)
        messages.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
        params = None  # nextLink already contains the query string
    return messages


def _list_pdf_attachments(message_id: str) -> list[dict]:
    url = f"{GRAPH_BASE_URL}/users/{settings.graph_mailbox}/messages/{message_id}/attachments"
    payload = _graph_get(url)
    attachments = []
    for att in payload.get("value", []):
        name = att.get("name", "")
        if name.lower().endswith(settings.invoice_attachment_extension) and "contentBytes" in att:
            attachments.append(att)
    return attachments


def fetch_invoice_attachments(since: date) -> list[InvoiceAttachment]:
    os.makedirs(INVOICE_DIR, exist_ok=True)

    results: list[InvoiceAttachment] = []
    messages = _list_messages_since(since)

    for message in messages:
        message_id = message["id"]
        subject = message.get("subject", "")
        from_info = (message.get("from") or {}).get("emailAddress", {})
        from_addr = from_info.get("address", "") or from_info.get("name", "")
        received_raw = message.get("receivedDateTime")
        try:
            received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            received_at = datetime.utcnow()

        for attachment in _list_pdf_attachments(message_id):
            filename = attachment.get("name", "attachment.pdf")
            safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", filename)
            pdf_path = os.path.join(INVOICE_DIR, f"{message_id}_{safe_name}")

            with open(pdf_path, "wb") as fh:
                fh.write(b64decode(attachment["contentBytes"]))

            extracted_text = ""
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    extracted_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            except Exception:
                # Corrupt/unreadable PDF -- keep the file, leave fields empty.
                extracted_text = ""

            fields = _extract_fields(extracted_text, from_addr)
            results.append(
                InvoiceAttachment(
                    email_message_id=message_id,
                    attachment_filename=filename,
                    email_subject=subject,
                    email_from=from_addr,
                    received_at=received_at,
                    pdf_path=pdf_path,
                    extracted_text=extracted_text,
                    **fields,
                )
            )

    return results
