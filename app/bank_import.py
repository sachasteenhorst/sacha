"""Parses Rabobank statement exports (CSV, CAMT.053 XML, MT940 Structured)
into a common row shape ready to store as Transaction rows.

IMPORTANT: these three parsers are built from the publicly documented
format specs, but have not been tried against one of your own downloaded
files yet -- only the unit tests' hand-built samples. Upload one real file
first; if it fails, the error message names exactly what wasn't found
(missing/renamed columns for CSV, missing tags for XML/MT940), which is
usually enough to fix in a couple of lines. This is the same "try it,
read the real error, adjust" loop that got the Basecone/Graph API bits
working earlier.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime


class BankImportError(RuntimeError):
    pass


@dataclass
class BankTransactionRow:
    external_ref: str
    booking_date: date
    amount_cents: int  # signed: negative = money out, positive = money in
    currency: str
    description: str
    counterparty_name: str
    counterparty_iban: str
    reference: str
    raw: dict
    bank_code: str = ""  # Rabobank CSV "Code" column (e.g. "tb", "ba"); empty for CAMT.053/MT940
    # This shop's OWN account IBAN this line belongs to -- lets a later
    # Ponto-fetched duplicate of the same booking be scoped to the same
    # account instead of matching across accounts (see app/bank_ponto.py's
    # find_matching_transaction). Populated for CSV/MT940 (both carry it
    # directly); left empty for CAMT.053, not extracted there yet.
    own_account_iban: str = ""


def _parse_signed_amount(raw: str) -> int:
    """Dutch (1.234,56) or plain (1234.56 / 1234,56) notation, optionally
    signed. Whichever of "." or "," appears last is the decimal point."""
    raw = raw.strip().replace(" ", "")
    if not raw:
        raise BankImportError("Leeg bedragveld.")
    negative = raw.startswith("-")
    if negative or raw.startswith("+"):
        raw = raw[1:]
    last_sep = max(raw.rfind("."), raw.rfind(","))
    if last_sep == -1:
        cents = int(raw) * 100
    else:
        integer_part = re.sub(r"[.,]", "", raw[:last_sep]) or "0"
        decimal_part = (raw[last_sep + 1:] + "00")[:2]
        cents = round(float(f"{integer_part}.{decimal_part}") * 100)
    return -cents if negative else cents


def _hash_ref(prefix: str, *parts: str) -> str:
    source = "|".join(parts)
    return f"{prefix}:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Rabobank CSV ("Rabo Internetbankieren > Downloads en documenten >
# Transacties downloaden")
# ---------------------------------------------------------------------------

_CSV_DATE_COLS = ["Datum"]
_CSV_AMOUNT_COLS = ["Bedrag"]
_CSV_NAME_COLS = ["Naam tegenpartij", "Naam uiteindelijke partij", "Naam initiërende partij"]
_CSV_CPTY_IBAN_COLS = ["Tegenrekening IBAN/BBAN", "Tegenrekening"]
_CSV_OWN_IBAN_COLS = ["IBAN/BBAN"]
_CSV_SEQ_COLS = ["Volgnr"]
_CSV_CODE_COLS = ["Code"]
_CSV_DESC_COLS = ["Omschrijving-1", "Omschrijving-2", "Omschrijving-3"]


def _find_column(fieldnames: list[str], candidates: list[str]) -> str | None:
    lower_map = {f.strip().lower(): f for f in fieldnames}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def _parse_rabo_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise BankImportError(f"Kon datum niet lezen: {raw!r}")


def parse_rabobank_csv(content: bytes) -> list[BankTransactionRow]:
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BankImportError("Kon de tekencodering van het CSV-bestand niet herkennen.")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise BankImportError("Geen kolommen gevonden in het CSV-bestand.")

    date_col = _find_column(reader.fieldnames, _CSV_DATE_COLS)
    amount_col = _find_column(reader.fieldnames, _CSV_AMOUNT_COLS)
    if not date_col or not amount_col:
        raise BankImportError(
            "Verwachte kolommen 'Datum' en/of 'Bedrag' niet gevonden. "
            f"Gevonden kolommen: {', '.join(reader.fieldnames)}"
        )
    name_col = _find_column(reader.fieldnames, _CSV_NAME_COLS)
    iban_col = _find_column(reader.fieldnames, _CSV_CPTY_IBAN_COLS)
    own_iban_col = _find_column(reader.fieldnames, _CSV_OWN_IBAN_COLS)
    seq_col = _find_column(reader.fieldnames, _CSV_SEQ_COLS)
    code_col = _find_column(reader.fieldnames, _CSV_CODE_COLS)
    desc_cols = [c for c in (_find_column(reader.fieldnames, [cand]) for cand in _CSV_DESC_COLS) if c]

    rows: list[BankTransactionRow] = []
    for row in reader:
        raw_date = (row.get(date_col) or "").strip()
        raw_amount = (row.get(amount_col) or "").strip()
        if not raw_date or not raw_amount:
            continue

        description = " ".join((row.get(c) or "").strip() for c in desc_cols if (row.get(c) or "").strip())
        counterparty_name = (row.get(name_col) or "").strip() if name_col else ""
        counterparty_iban = (row.get(iban_col) or "").strip() if iban_col else ""
        own_iban = (row.get(own_iban_col) or "").strip() if own_iban_col else ""
        seq = (row.get(seq_col) or "").strip() if seq_col else ""
        code = (row.get(code_col) or "").strip() if code_col else ""

        rows.append(
            BankTransactionRow(
                external_ref=_hash_ref("csv", own_iban, raw_date, raw_amount, counterparty_iban, description, seq),
                booking_date=_parse_rabo_date(raw_date),
                amount_cents=_parse_signed_amount(raw_amount),
                bank_code=code,
                currency="EUR",
                description=description,
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                reference=seq,
                raw=dict(row),
                own_account_iban=own_iban,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# CAMT.053 (ISO 20022 bank-to-customer statement XML)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _find_local(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _findall_local(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [c for c in elem.iter() if _local(c.tag) == name]


def parse_camt053(content: bytes) -> list[BankTransactionRow]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise BankImportError(f"Kon CAMT.053-bestand niet lezen als XML: {exc}") from exc

    entries = _findall_local(root, "Ntry")
    if not entries:
        raise BankImportError("Geen transacties (<Ntry>) gevonden in het CAMT.053-bestand.")

    rows: list[BankTransactionRow] = []
    for idx, ntry in enumerate(entries):
        amt_elem = _find_local(ntry, "Amt")
        cdt_dbt_elem = _find_local(ntry, "CdtDbtInd")
        if amt_elem is None or cdt_dbt_elem is None or not amt_elem.text:
            continue

        is_debit = (cdt_dbt_elem.text or "").strip().upper() == "DBIT"
        amount_cents = abs(_parse_signed_amount(amt_elem.text))
        if is_debit:
            amount_cents = -amount_cents

        booking_dt = _find_local(_find_local(ntry, "BookgDt"), "Dt")
        if booking_dt is None or not booking_dt.text:
            booking_dt = _find_local(_find_local(ntry, "ValDt"), "Dt")
        if booking_dt is None or not booking_dt.text:
            continue
        booking_date = date.fromisoformat(booking_dt.text[:10])

        # Note: ET.Element is falsy when it has no *child elements*, even
        # with text content -- "A or B" would silently discard a found
        # AcctSvcrRef, so this checks "is None" explicitly.
        ref_elem = _find_local(ntry, "AcctSvcrRef")
        if ref_elem is None:
            ref_elem = _find_local(ntry, "NtryRef")
        reference = (ref_elem.text or "").strip() if ref_elem is not None else ""

        descriptions: list[str] = []
        counterparty_name = ""
        counterparty_iban = ""
        party_tag = "Cdtr" if is_debit else "Dbtr"
        acct_tag = "CdtrAcct" if is_debit else "DbtrAcct"

        for tx_dtls in _findall_local(ntry, "TxDtls"):
            for ustrd in _findall_local(_find_local(tx_dtls, "RmtInf"), "Ustrd"):
                if ustrd.text:
                    descriptions.append(ustrd.text.strip())

            if not counterparty_name:
                rltd_pties = _find_local(tx_dtls, "RltdPties")
                party_elem = _find_local(rltd_pties, party_tag)
                nm_elem = _find_local(party_elem, "Nm")
                if nm_elem is not None and nm_elem.text:
                    counterparty_name = nm_elem.text.strip()
                acct_elem = _find_local(rltd_pties, acct_tag)
                iban_elem = _find_local(acct_elem, "IBAN")
                if iban_elem is not None and iban_elem.text:
                    counterparty_iban = iban_elem.text.strip()

        description = " ".join(descriptions)
        if reference:
            external_ref = f"camt:{reference}"
        else:
            external_ref = _hash_ref("camt", str(booking_date), str(amount_cents), counterparty_iban, description, str(idx))

        rows.append(
            BankTransactionRow(
                external_ref=external_ref,
                booking_date=booking_date,
                amount_cents=amount_cents,
                currency=amt_elem.attrib.get("Ccy", "EUR"),
                description=description,
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                reference=reference,
                raw={"index": idx},
            )
        )
    return rows


# ---------------------------------------------------------------------------
# MT940 Structured (.swi)
# ---------------------------------------------------------------------------

_MT940_LINE_61_RE = re.compile(r"^:61:(\d{6})(?:\d{4})?(RC|RD|C|D)([\d,]+)")
_MT940_SUBFIELD_RE_CACHE: dict[str, re.Pattern] = {}


def _mt940_subfield(text: str, tag: str) -> str:
    pattern = _MT940_SUBFIELD_RE_CACHE.setdefault(tag, re.compile(re.escape(f"/{tag}/") + r"([^/\n]*)"))
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def parse_mt940(content: bytes) -> list[BankTransactionRow]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")

    own_iban = ""
    entries: list[dict] = []
    current: dict | None = None
    seq = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith(":25:"):
            own_iban = line[4:].strip()
        elif line.startswith(":61:"):
            m = _MT940_LINE_61_RE.match(line)
            if not m:
                continue
            seq += 1
            try:
                booking_date = datetime.strptime(m.group(1), "%y%m%d").date()
            except ValueError:
                continue
            # Reversal codes (RC/RD) are rare in everyday retail banking;
            # treated here by their literal C/D suffix rather than flipping
            # the sign, which is close enough for matching purposes.
            is_credit = m.group(2)[-1] == "C"
            amount_cents = abs(_parse_signed_amount(m.group(3).replace(",", ".")))
            current = {
                "booking_date": booking_date,
                "amount_cents": amount_cents if is_credit else -amount_cents,
                "description_lines": [],
                "raw_line_61": line,
                "seq": seq,
            }
            entries.append(current)
        elif line.startswith(":86:") and current is not None:
            current["description_lines"].append(line[4:].strip())
        elif current is not None and current["description_lines"] and line and not line.startswith(":"):
            # :86: (remittance info) can continue on the following line(s).
            current["description_lines"].append(line.strip())

    if not entries:
        raise BankImportError("Geen statementregels (:61:) gevonden in het MT940-bestand.")

    rows: list[BankTransactionRow] = []
    for e in entries:
        description = " ".join(e["description_lines"])
        rows.append(
            BankTransactionRow(
                external_ref=_hash_ref("mt940", own_iban, str(e["booking_date"]), str(e["amount_cents"]), e["raw_line_61"], str(e["seq"])),
                booking_date=e["booking_date"],
                amount_cents=e["amount_cents"],
                currency="EUR",
                description=description,
                counterparty_name=_mt940_subfield(description, "NAME") or _mt940_subfield(description, "NAAM"),
                counterparty_iban=_mt940_subfield(description, "IBAN"),
                reference="",
                raw={"line_61": e["raw_line_61"]},
                own_account_iban=own_iban,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def parse_bank_file(filename: str, content: bytes) -> list[BankTransactionRow]:
    name = filename.lower()
    if name.endswith(".csv"):
        return parse_rabobank_csv(content)
    if name.endswith(".xml"):
        return parse_camt053(content)
    if name.endswith((".swi", ".940", ".sta", ".mt940")):
        return parse_mt940(content)
    raise BankImportError(
        f"Onbekend bestandstype voor '{filename}'. Ondersteund: .csv (Rabobank), "
        ".xml (CAMT.053), .swi/.940/.sta (MT940 Structured)."
    )
