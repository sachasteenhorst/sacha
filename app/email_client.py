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

import hashlib
import os
import re
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime

import pdfplumber
import requests

from app.config import settings
from app.models import BaseconeForwardStatus, Direction, DocumentKind

INVOICE_DIR = "./data/invoices"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}

# -- Amount --
# Matches both Dutch (1.234,56) and plain (1,234.56 / 39.60) notation; which
# separator is the decimal one is worked out in _parse_amount_literal from
# whichever of "." or "," appears last, not from a fixed pattern branch.
AMOUNT_NUMBER = r"\d{1,3}(?:[.,]\d{3})*[.,]\d{2}"
AMOUNT_WITH_CURRENCY_RE = re.compile(r"(?:€|eur\.?)\s*(" + AMOUNT_NUMBER + r")", re.IGNORECASE)
AMOUNT_BARE_RE = re.compile(r"(" + AMOUNT_NUMBER + r")")

# Priority order: the first label that yields a parsable amount anywhere in
# the document wins. "Totaal incl. BTW" et al beat a bare "Total" because a
# document can have several unrelated numbers near the generic word "total".
TOTAL_LABEL_PATTERNS = [
    r"totaal\s*incl(?:usief)?\.?\s*(?:van\s*)?btw",
    r"te\s*betalen",
    r"totaalbedrag",
    r"openstaand(?:e)?(?:\s*bedrag)?",
    r"amount\s*due",
    r"eindtotaal",
    r"invoice\s*total",
    r"\btotal\b",
]
# For INCOMING documents (money paid onto the account, e.g. an ENRA
# rekening-courant overzicht) the meaningful label is what's being paid
# out to you, not a "total due" -- tried before the outgoing-style labels.
INCOMING_LABEL_PATTERNS = [
    r"te\s*storten(?:e)?(?:\s*bedrag)?",
    r"gestort(?:e)?(?:\s*bedrag)?",
    r"over\s*te\s*maken(?:\s*bedrag)?",
    r"uit\s*te\s*keren(?:\s*bedrag)?",
]

# -- Invoice number --
INVOICE_NUMBER_STRONG_LABEL_RE = re.compile(
    r"factuurnummer|factuur\s*nr\.?|invoice\s*(?:number|no)\.?", re.IGNORECASE
)
INVOICE_NUMBER_WEAK_LABEL_RE = re.compile(r"\bnummer\b", re.IGNORECASE)
# Words that show up right after an invoice-number label purely because of
# PDF layout (columns collapsed into running text) -- never the number itself.
INVOICE_NUMBER_STOPWORDS = {
    "datum", "factuurdatum", "factuur", "invoice", "pagina", "page",
    "nummer", "number", "bedrag", "totaal", "btw", "klant", "klantnummer",
    "referentie", "reference", "type", "van", "voor", "aan", "the",
}
INVOICE_NUMBER_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.]{1,30}")

DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
DATE_DUTCH_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(DUTCH_MONTHS.keys()) + r")\s+(\d{4})\b", re.IGNORECASE
)
INVOICE_DATE_LABEL_RE = re.compile(r"factuurdatum\D{0,10}", re.IGNORECASE)

# -- Supplier name --
# Domains we already know the proper company name for -- the automatic
# domain-to-name fallback below can't reliably split concatenated Dutch
# words ("vanderlaangroep" -> "Van der Laan Groep"), so known suppliers get
# an exact answer instead of a best-effort guess.
KNOWN_SUPPLIER_DOMAINS = {
    "kruitbosch.nl": "Kruitbosch",
    "accell.nl": "Accell",
    "tenways.com": "Tenways",
    "enra.nl": "ENRA",
    "twsc.nl": "TWSC",
    "gazelle.nl": "Gazelle",
    "wrutteman.nl": "Wrutteman",
    "hellorider.com": "HelloRider",
    "essent.nl": "Essent",
    "fietsunie.nl": "Fietsunie",
    "vanderlaangroep.nl": "Van der Laan Groep",
    "fietshuys.nl": "Fietshuys",
    "zuidewind-bv.nl": "Zuidewind BV",
    "retif.eu": "RETIF",
    "shimano-eu.com": "Shimano",
    "giant-europe.com": "Giant",
}

# -- Document classification --
# A document matching one of these is never proof of a payment -- it
# shouldn't count as an open invoice waiting to be matched, regardless of
# whether an amount happens to be found on the page.
NON_INVOICE_RE = re.compile(
    r"algemene\s*voorwaarden|general\s*terms|terms\s*(and|&)\s*conditions|\bgtc\b|"
    r"wijziging.*bank.*rekening|change\s*(in|of)\s*(payment\s*)?bank\s*account|"
    r"\bubo\b.{0,10}verklaring|privacy\s*(statement|policy|verklaring)",
    re.IGNORECASE,
)
# A packing slip only counts as "not an invoice" when it also has no
# amount -- some suppliers put pricing on theirs.
PACKING_SLIP_RE = re.compile(r"pakbon|paklijst|packing\s*slip", re.IGNORECASE)
# One document listing several invoices settled by a single bank payment
# (e.g. Accell's "Specificatie automatische incasso").
SPECIFICATION_RE = re.compile(
    r"specificatie.{0,20}incasso|automatische\s*incasso.{0,20}specificatie", re.IGNORECASE
)
# A document that plausibly proves a purchase, for the stricter filter
# applied to attachments from a mailbox that isn't invoice-dedicated (see
# fetch_invoice_attachments).
INVOICE_LIKE_RE = re.compile(
    r"factuur|invoice|creditnota|credit\s*note|specificatie|rekening|\bnota\b", re.IGNORECASE
)


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
    direction: str = Direction.OUTGOING.value
    document_kind: str = DocumentKind.INVOICE.value
    referenced_invoice_numbers: list = None
    content_hash: str = ""
    graph_mailbox: str = ""
    graph_message_id: str = ""
    in_basecone: str = "unknown"
    basecone_forward_method: str = ""

    def __post_init__(self):
        if self.referenced_invoice_numbers is None:
            self.referenced_invoice_numbers = []


def invoice_dedup_key(content_hash: str, supplier_name: str, invoice_number: str, amount_cents: int | None) -> str | None:
    """A document identity that survives being fetched twice from different
    mailboxes (facturen@ and info@ both receiving the same supplier e-mail
    as separate messages, each with its own message id) -- content_hash is
    the strongest signal (byte-identical PDF); when that's not available
    (e.g. an older row from before this existed), falls back to
    leverancier+factuurnummer+bedrag. None means "can't tell", i.e. never
    treated as a duplicate."""
    if content_hash:
        return f"hash:{content_hash}"
    if invoice_number and amount_cents is not None:
        return f"key:{(supplier_name or '').strip().lower()}|{invoice_number.strip().lower()}|{amount_cents}"
    return None


def _parse_amount_literal(raw: str) -> int:
    """Turn "1.234,56", "1,234.56" or "39,60" into cents. Whichever "." or
    ","  appears LAST in the string marks the decimal point; every other
    separator before it (of either kind -- some extracted text mixes them,
    e.g. "1.151.15") is just a thousands grouping and gets stripped."""
    last_sep = max(raw.rfind("."), raw.rfind(","))
    integer_part = re.sub(r"[.,]", "", raw[:last_sep])
    decimal_part = raw[last_sep + 1:]
    return round(float(f"{integer_part}.{decimal_part}") * 100)


def _find_amount_near_label(text: str, label_pattern: str) -> int | None:
    label_re = re.compile(label_pattern, re.IGNORECASE)
    for m in label_re.finditer(text):
        # Look across the label's own line plus the next one -- some
        # layouts put the value right after the label ("Totaal: 39,60"),
        # others put it on the following line ("Totaal:\n39,60"), and some
        # put more than one number on the label's line with the real total
        # last ("Eindtotaal 0,00 197,17"). Capped in both line count and
        # character count so it can't drift into an unrelated section.
        end = m.end()
        for _ in range(2):
            next_nl = text.find("\n", end)
            if next_nl == -1:
                end = len(text)
                break
            end = next_nl + 1
        window = text[m.end(): min(end, m.end() + 120)]
        matches = list(AMOUNT_WITH_CURRENCY_RE.finditer(window)) or list(AMOUNT_BARE_RE.finditer(window))
        if matches:
            return _parse_amount_literal(matches[-1].group(1))
    return None


def _parse_amount_to_cents(text: str, direction: str = Direction.OUTGOING.value) -> int | None:
    patterns = TOTAL_LABEL_PATTERNS
    if direction == Direction.INCOMING.value:
        patterns = INCOMING_LABEL_PATTERNS + TOTAL_LABEL_PATTERNS
    for pattern in patterns:
        cents = _find_amount_near_label(text, pattern)
        if cents is not None:
            return cents

    # No recognised "total" label anywhere -- fall back to the largest
    # amount that has an explicit currency marker right in front of it. A
    # bare number with no currency and no label nearby is too weak a signal
    # (it's as likely to be a quantity, VAT rate or reference number).
    candidates = [_parse_amount_literal(m.group(1)) for m in AMOUNT_WITH_CURRENCY_RE.finditer(text)]
    return max(candidates) if candidates else None


# Real invoice numbers are practically always longer than this -- a short
# token like "28" or "08" is almost certainly something else entirely (seen
# in production: an internet-speed figure on an Mbps invoice), and a short
# number is also exactly the kind of thing that coincidentally turns up
# as a substring inside an unrelated bank description later (matcher.py's
# reference matching enforces the same floor for the same reason).
MIN_INVOICE_NUMBER_LENGTH = 4


def _first_valid_number_token(window: str) -> str:
    for token_match in INVOICE_NUMBER_TOKEN_RE.finditer(window):
        token = token_match.group(0).strip(".-/")
        if not token or token.lower() in INVOICE_NUMBER_STOPWORDS:
            continue
        if not any(ch.isdigit() for ch in token):
            continue
        if len(token) < MIN_INVOICE_NUMBER_LENGTH:
            continue
        return token
    return ""


# A short, mostly-numeric token (an incassant ID, klantnummer, debiteurnummer
# -- e.g. Kruitbosch's incassant ID "306228") that PDF-layout column collapse
# sometimes puts right after a "Factuurnummer" label instead of the real
# number. The real thing (e.g. "VFNL002280953") is longer and starts with
# letters, so this is a narrow, deliberately conservative check.
ID_LIKE_INVOICE_NUMBER_RE = re.compile(r"^\d{4,7}[A-Za-z]?$")
# A real invoice number is almost always right there in the filename too
# (suppliers name the PDF after it) -- e.g.
# "Kruitbosch Factuur VFNL002280953_1.pdf". Used both as a fallback and to
# override a label match that looks like an ID instead (see above).
FILENAME_TOKEN_STOPWORDS = {
    "factuur", "faktuur", "invoice", "creditnota", "credit", "note",
    "copy", "kopie", "duplicate", "final", "specificatie",
}


def _looks_like_id_not_invoice_number(token: str) -> bool:
    return bool(ID_LIKE_INVOICE_NUMBER_RE.match(token))


def _extract_invoice_number_from_filename(filename: str) -> str:
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename or "")
    candidates = []
    for token in re.split(r"[\s_\-]+", base):
        cleaned = token.strip(".")
        if not cleaned or cleaned.lower() in FILENAME_TOKEN_STOPWORDS:
            continue
        if not any(ch.isdigit() for ch in cleaned) or len(cleaned) < 5:
            continue
        candidates.append(cleaned)
    # The real invoice number is the most distinctive (longest) token --
    # a trailing copy-suffix like "_1" or "(2)" is short and loses.
    return max(candidates, key=len) if candidates else ""


def _labelled_invoice_number_candidates(text: str) -> list[str]:
    # Strong, unambiguous labels first, across every occurrence in the
    # document; a generic bare "nummer" is only tried if the strong label
    # yielded nothing at all anywhere (it also matches
    # "klantnummer"/"ordernummer" less often than you'd think, since those
    # are single words with no space before "nummer" and
    # INVOICE_NUMBER_WEAK_LABEL_RE requires a word boundary right before it).
    for label_re in (INVOICE_NUMBER_STRONG_LABEL_RE, INVOICE_NUMBER_WEAK_LABEL_RE):
        candidates = []
        for m in label_re.finditer(text):
            token = _first_valid_number_token(text[m.end(): m.end() + 60])
            if token:
                candidates.append(token)
        if candidates:
            return candidates
    return []


def _extract_invoice_number(text: str, filename: str = "") -> str:
    candidates = _labelled_invoice_number_candidates(text)
    for token in candidates:
        if not _looks_like_id_not_invoice_number(token):
            return token
    # Every labelled candidate looked like an incassant/klantnummer (layout
    # collapse) -- the filename usually carries the real number instead.
    filename_number = _extract_invoice_number_from_filename(filename)
    if filename_number:
        return filename_number
    return candidates[0] if candidates else ""


def _extract_all_invoice_numbers(text: str) -> list[str]:
    """For a SPECIFICATION document: every invoice number mentioned, not
    just the first (e.g. Accell's "Specificatie automatische incasso"
    listing several invoices settled by one direct debit)."""
    found: list[str] = []
    for label_re in (INVOICE_NUMBER_STRONG_LABEL_RE, INVOICE_NUMBER_WEAK_LABEL_RE):
        for m in label_re.finditer(text):
            token = _first_valid_number_token(text[m.end(): m.end() + 60])
            if token and token not in found:
                found.append(token)
    return found


SUBJECT_INVOICE_REF_RE = re.compile(r"\(Ref[:\s]+([A-Za-z0-9/\-]+)\)", re.IGNORECASE)


def _extract_invoice_number_from_subject(subject: str) -> str:
    """Fallback when the PDF text yields nothing: some suppliers (Tenways)
    put the invoice reference in the e-mail subject instead, e.g.
    "... Invoice (Ref INV/2026/23162)"."""
    m = SUBJECT_INVOICE_REF_RE.search(subject or "")
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1)
    return ""


def _classify_document_kind(filename: str, subject: str, text: str, amount_cents: int | None) -> str:
    haystack = f"{filename} {subject} {text[:1500]}"
    if NON_INVOICE_RE.search(haystack):
        return DocumentKind.OTHER.value
    if SPECIFICATION_RE.search(haystack):
        return DocumentKind.SPECIFICATION.value
    if amount_cents is None and PACKING_SLIP_RE.search(haystack):
        return DocumentKind.OTHER.value
    return DocumentKind.INVOICE.value


def _is_own_company(address: str, display_name: str) -> bool:
    """True when the sender is the shop itself -- e.g. a verkoopfactuur it
    sent to a customer, archived/CC'd into a scanned mailbox like info@.
    That's proof of money the shop is owed, never a bill to pay, so it must
    not be treated as an open inkoopfactuur (see _extract_fields)."""
    address = (address or "").strip().lower()
    if "@" in address and address.split("@", 1)[1] in settings.own_mail_domains:
        return True
    name_lower = (display_name or "").strip().lower()
    return any(own_name in name_lower for own_name in settings.own_company_name_list if own_name)


def _recipients_contain(message: dict, address: str) -> bool:
    if not address:
        return False
    address = address.lower()
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for r in message.get(field) or []:
            if (r.get("emailAddress", {}).get("address") or "").lower() == address:
                return True
    return False


def message_was_sent_to_basecone(message: dict) -> bool:
    """True when the ORIGINAL e-mail already had the Basecone-inbox address
    on to/cc/bcc -- the supplier put it there directly, so it's already in
    Basecone and auto-forwarding it again would be a duplicate."""
    return _recipients_contain(message, settings.basecone_forward_address)


def _assign_direction(supplier_name: str) -> str:
    name_lower = (supplier_name or "").lower()
    for incoming_name in settings.incoming_supplier_names:
        if incoming_name and (incoming_name in name_lower or name_lower in incoming_name):
            return Direction.INCOMING.value
    return Direction.OUTGOING.value


def _base_domain(domain: str) -> str:
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _domain_to_supplier_name(domain: str) -> str:
    base = _base_domain(domain)
    if base in KNOWN_SUPPLIER_DOMAINS:
        return KNOWN_SUPPLIER_DOMAINS[base]
    label = base.split(".")[0]
    words = [w for w in re.split(r"[-_]", label) if w]
    return " ".join(w.capitalize() for w in words) if words else base


def _derive_supplier_name(address: str, display_name: str) -> str:
    address = (address or "").strip()
    display_name = (display_name or "").strip()
    # A real display name beats a guess from the domain -- unless it's
    # missing, or is itself just the address again (some senders set both
    # to the same value).
    if display_name and "@" not in display_name and display_name.lower() != address.lower():
        return display_name
    if "@" in address:
        return _domain_to_supplier_name(address.split("@", 1)[1])
    return display_name or address or "Onbekende leverancier"


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


def _extract_fields(
    text: str, address: str, display_name: str = "", subject: str = "", filename: str = ""
) -> dict:
    supplier_name = _derive_supplier_name(address, display_name)
    direction = _assign_direction(supplier_name)
    amount_cents = _parse_amount_to_cents(text, direction)
    invoice_number = _extract_invoice_number(text, filename) or _extract_invoice_number_from_subject(subject)
    document_kind = _classify_document_kind(filename, subject, text, amount_cents)
    if _is_own_company(address, display_name):
        # A verkoopfactuur we sent ourselves is never a purchase to pay --
        # never let it show up as an open inkoopfactuur.
        document_kind = DocumentKind.OTHER.value
    referenced_invoice_numbers = (
        _extract_all_invoice_numbers(text) if document_kind == DocumentKind.SPECIFICATION.value else []
    )
    return {
        "invoice_number": invoice_number,
        "invoice_date": _parse_invoice_date(text),
        "amount_cents": amount_cents,
        "supplier_name": supplier_name,
        "direction": direction,
        "document_kind": document_kind,
        "referenced_invoice_numbers": referenced_invoice_numbers,
    }


# Mail.Read.Shared is a Delegated (not Application) permission -- it lets a
# signed-in user's app read mail in any mailbox that user has been given
# "Full Access" delegate rights to (e.g. a shared mailbox like info@...),
# without requiring a Global Administrator to grant tenant-wide consent.
# The trade-off: there's no unattended client-credentials login, so a human
# has to sign in once (via scripts/graph_login.py) to produce a refresh
# token, which this class then uses to keep getting new access tokens.
#
# Mail.Send (send as "me") and Mail.Send.Shared (send as a shared mailbox,
# needed to send from facturen@/info@) are ALSO requested here -- but only
# ever consented to once someone re-runs graph_login.py after adding those
# permissions in Entra (see README). Until then, an existing refresh token
# only has Mail.Read.Shared consent, and _GraphAuth.token() below falls back
# to requesting just that -- so upgrading this app to a newer version can
# never break an already-working read-only sync, only leave forwarding
# switched off until that one manual step happens.
GRAPH_READ_SCOPES = "https://graph.microsoft.com/Mail.Read.Shared"
GRAPH_SEND_SCOPES = "https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Mail.Send.Shared"
GRAPH_SCOPES = f"{GRAPH_READ_SCOPES} {GRAPH_SEND_SCOPES} offline_access"
REFRESH_TOKEN_FILE = "./data/graph_refresh_token.txt"


class _GraphAuth:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._has_send_scope: bool = False

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

        # Try the full scope (read + send) first; an OLDER refresh token that
        # was only ever consented for Mail.Read.Shared gets rejected for
        # that, so fall back to read-only -- this is what keeps an upgrade
        # from breaking an already-working sync (see the comment on
        # GRAPH_SCOPES above). self._sent_scope records which one actually
        # won, so callers that need to send can tell whether that's even
        # possible with the current consent before trying.
        response = None
        for scope, granted_send in ((GRAPH_SCOPES, True), (f"{GRAPH_READ_SCOPES} offline_access", False)):
            data = {
                "grant_type": "refresh_token",
                "client_id": settings.graph_client_id,
                "refresh_token": refresh_token,
                "scope": scope,
            }
            response = requests.post(token_url, data=data, timeout=30)
            if response.status_code == 200:
                self._has_send_scope = granted_send
                break

        if response is None or response.status_code != 200:
            raise GraphAuthError(
                f"Microsoft Graph token vernieuwen mislukt ({response.status_code if response else '?'}): "
                f"{response.text if response else ''}. Mogelijk moet je opnieuw inloggen via "
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

    def has_send_scope(self) -> bool:
        """Whether the current refresh token actually got Mail.Send(.Shared)
        consent -- lets a caller give a clear "permission missing" message
        instead of a raw 403 from Graph."""
        self.token()
        return self._has_send_scope


_auth = _GraphAuth()


def _graph_get(url: str, params: dict | None = None) -> dict:
    token = _auth.token()
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    if response.status_code != 200:
        raise GraphApiError(f"Graph API-fout ({response.status_code}) bij {url}: {response.text}")
    return response.json()


def _list_messages_since(mailbox: str, since: date) -> list[dict]:
    since_iso = f"{since.isoformat()}T00:00:00Z"
    url = f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders/{settings.graph_mail_folder}/messages"
    params = {
        "$filter": f"receivedDateTime ge {since_iso} and hasAttachments eq true",
        "$select": "id,internetMessageId,subject,from,receivedDateTime,hasAttachments,toRecipients,ccRecipients,bccRecipients",
        "$top": "50",
    }

    messages: list[dict] = []
    while url:
        payload = _graph_get(url, params)
        messages.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
        params = None  # nextLink already contains the query string
    return messages


def list_sent_items_since(mailbox: str, since: date) -> list[dict]:
    """Sent Items of `mailbox` -- pass "me" for the logged-in user's own
    mailbox. Used to find an invoice that was already forwarded to Basecone
    by hand, before this app could do it automatically."""
    since_iso = f"{since.isoformat()}T00:00:00Z"
    base = f"{GRAPH_BASE_URL}/me" if mailbox == "me" else f"{GRAPH_BASE_URL}/users/{mailbox}"
    url = f"{base}/mailFolders/SentItems/messages"
    params = {
        "$filter": f"sentDateTime ge {since_iso}",
        "$select": "id,subject,toRecipients,ccRecipients,bccRecipients,sentDateTime",
        "$top": "50",
    }
    messages: list[dict] = []
    while url:
        payload = _graph_get(url, params)
        messages.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
        params = None
    return messages


def list_message_attachment_names(mailbox: str, message_id: str) -> list[str]:
    base = f"{GRAPH_BASE_URL}/me" if mailbox == "me" else f"{GRAPH_BASE_URL}/users/{mailbox}"
    url = f"{base}/messages/{message_id}/attachments"
    try:
        payload = _graph_get(url, {"$select": "name"})
    except GraphApiError:
        return []
    return [a.get("name", "") for a in payload.get("value", [])]


def send_mail(mailbox: str, message: dict) -> None:
    """POSTs to /users/{mailbox}/sendMail (or /me/sendMail for the logged-in
    user), saving a copy to Sent Items. Requires Mail.Send.Shared for a
    shared mailbox or Mail.Send for "me" -- see GRAPH_SCOPES."""
    token = _auth.token()
    base = f"{GRAPH_BASE_URL}/me" if mailbox == "me" else f"{GRAPH_BASE_URL}/users/{mailbox}"
    url = f"{base}/sendMail"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"message": message, "saveToSentItems": True},
        timeout=30,
    )
    if response.status_code == 403:
        scope_name = "Mail.Send" if mailbox == "me" else "Mail.Send.Shared"
        raise GraphApiError(
            f"Geen rechten om te versturen vanuit {mailbox} -- de gedelegeerde "
            f"permissie {scope_name} ontbreekt (of is nog niet opnieuw "
            "geconsenteerd). Voeg 'm toe in Entra en log opnieuw in met "
            "scripts/graph_login.py (zie README)."
        )
    if response.status_code not in (200, 202):
        raise GraphApiError(f"Versturen mislukt ({response.status_code}) vanuit {mailbox}: {response.text}")


def _list_pdf_attachments(mailbox: str, message_id: str) -> list[dict]:
    url = f"{GRAPH_BASE_URL}/users/{mailbox}/messages/{message_id}/attachments"
    payload = _graph_get(url)
    attachments = []
    for att in payload.get("value", []):
        name = att.get("name", "")
        if name.lower().endswith(settings.invoice_attachment_extension) and "contentBytes" in att:
            attachments.append(att)
    return attachments


def fetch_invoice_attachments(since: date) -> list[InvoiceAttachment]:
    os.makedirs(INVOICE_DIR, exist_ok=True)

    mailboxes = settings.graph_mailboxes
    if not mailboxes:
        raise GraphAuthError(
            "GRAPH_MAILBOX is niet ingesteld -- dit is het mailadres (of een "
            "kommagescheiden lijst van mailadressen) waar facturen binnenkomen."
        )

    results: list[InvoiceAttachment] = []
    # The same e-mail can land in more than one configured mailbox (e.g. CC'd
    # to both facturen@ and info@) -- dedup on the message's real
    # Internet Message-ID + attachment filename within this run, on top of
    # the DB-level uniqueness check the caller (sync.py) does across runs.
    seen_keys: set[tuple[str, str]] = set()
    # A second, independent line of defence: facturen@ and info@ often
    # receive the SAME invoice as two genuinely separate messages (different
    # internetMessageId each), so the check above alone lets both through --
    # this catches that by document identity instead of message identity.
    seen_dedup_keys: set[str] = set()

    for mailbox_index, mailbox in enumerate(mailboxes):
        # Only the first configured mailbox is treated as invoice-dedicated;
        # any others (e.g. a general info@ inbox) get every PDF attachment
        # checked against INVOICE_LIKE_RE first, so newsletters and other
        # unrelated mail don't turn into fake "open invoices".
        is_primary = mailbox_index == 0

        for message in _list_messages_since(mailbox, since):
            message_id = message["id"]
            internet_message_id = message.get("internetMessageId") or message_id
            subject = message.get("subject", "")
            from_info = (message.get("from") or {}).get("emailAddress", {})
            from_addr = from_info.get("address", "") or ""
            from_name = from_info.get("name", "") or ""
            received_raw = message.get("receivedDateTime")
            try:
                received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, AttributeError):
                received_at = datetime.utcnow()
            sent_to_basecone_directly = message_was_sent_to_basecone(message)

            for attachment in _list_pdf_attachments(mailbox, message_id):
                filename = attachment.get("name", "attachment.pdf")

                if not is_primary and not INVOICE_LIKE_RE.search(f"{filename} {subject}"):
                    continue

                key = (internet_message_id, filename)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                content_bytes = b64decode(attachment["contentBytes"])
                content_hash = hashlib.sha256(content_bytes).hexdigest()

                safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", filename)
                pdf_path = os.path.join(INVOICE_DIR, f"{message_id}_{safe_name}")

                with open(pdf_path, "wb") as fh:
                    fh.write(content_bytes)

                extracted_text = ""
                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        extracted_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
                except Exception:
                    # Corrupt/unreadable PDF -- keep the file, leave fields empty.
                    extracted_text = ""

                fields = _extract_fields(extracted_text, from_addr, from_name, subject, filename)
                dedup_key = invoice_dedup_key(content_hash, fields["supplier_name"], fields["invoice_number"], fields["amount_cents"])
                if dedup_key is not None and dedup_key in seen_dedup_keys:
                    os.remove(pdf_path)  # the copy we just wrote is the duplicate -- don't keep it on disk
                    continue
                if dedup_key is not None:
                    seen_dedup_keys.add(dedup_key)

                results.append(
                    InvoiceAttachment(
                        email_message_id=internet_message_id,
                        attachment_filename=filename,
                        email_subject=subject,
                        email_from=from_addr,
                        received_at=received_at,
                        pdf_path=pdf_path,
                        extracted_text=extracted_text,
                        content_hash=content_hash,
                        graph_mailbox=mailbox,
                        graph_message_id=message_id,
                        in_basecone=BaseconeForwardStatus.YES.value if sent_to_basecone_directly else BaseconeForwardStatus.UNKNOWN.value,
                        basecone_forward_method="original" if sent_to_basecone_directly else "",
                        **fields,
                    )
                )

    return results
