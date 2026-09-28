"""Tests scripts/reparse_invoices.py's duplicate-cleanup pass in isolation
(no real PDFs -- it's given pre-computed content_hash/fields directly, since
_dedupe_invoices only looks at DB rows, not the filesystem)."""
import importlib.util
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Invoice, MatchStatus

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "reparse_invoices.py")
_spec = importlib.util.spec_from_file_location("reparse_invoices", _SCRIPT_PATH)
reparse_invoices = importlib.util.module_from_spec(_spec)
sys.modules["reparse_invoices"] = reparse_invoices
_spec.loader.exec_module(reparse_invoices)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        email_subject="Factuur",
        email_from="verkoop@leverancier.nl",
        received_at=datetime(2026, 3, 1),
        invoice_number="F-2026-001",
        supplier_name="Kruitbosch",
        amount_cents=12345,
        content_hash="",
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, text):
        self.pages = [_FakePage(text)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_pdfplumber_open_returning(text):
    def _open(path):
        return _FakePdf(text)
    return _open


def test_reparse_reopens_previously_auto_ignored_document(session, tmp_path, monkeypatch):
    # Real production bug: Accell's invoice/specification footer ("... gelden
    # onze algemene voorwaarden ...") got every Accell document classified
    # as "other" and auto-ignored. Once the classifier is fixed, reparse
    # must reopen these -- but only the ones it ignored itself.
    pdf_path = tmp_path / "251103005.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    inv = make_invoice(
        pdf_path=str(pdf_path),
        document_kind="other",
        status=MatchStatus.IGNORED,
        manually_ignored=False,
        amount_cents=None,
        invoice_number="",
        email_from="noreply@accell.nl",
    )
    session.add(inv)
    session.commit()

    real_invoice_text = "Factuurnummer: 251103005\nTotaal incl. BTW: EUR 204,28"
    monkeypatch.setattr(reparse_invoices.pdfplumber, "open", _fake_pdfplumber_open_returning(real_invoice_text))

    total, updated, missing_pdf, unreadable, newly_other, reopened = reparse_invoices._reparse_fields(session)

    assert reopened == 1
    assert inv.status == MatchStatus.UNMATCHED
    assert inv.document_kind == "invoice"


def test_reparse_never_reopens_manually_ignored_document(session, tmp_path, monkeypatch):
    pdf_path = tmp_path / "factuur.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    inv = make_invoice(
        pdf_path=str(pdf_path),
        document_kind="invoice",
        status=MatchStatus.IGNORED,
        manually_ignored=True,
        amount_cents=5000,
        invoice_number="F-1",
    )
    session.add(inv)
    session.commit()

    # Re-extraction yields the exact same (still a real invoice) result --
    # this must never flip a manually-ignored document back open.
    monkeypatch.setattr(reparse_invoices.pdfplumber, "open", _fake_pdfplumber_open_returning("Factuurnummer: F-1\nTotaal incl. BTW: EUR 50,00"))

    total, updated, missing_pdf, unreadable, newly_other, reopened = reparse_invoices._reparse_fields(session)

    assert reopened == 0
    assert inv.status == MatchStatus.IGNORED


def test_dedupe_removes_content_hash_duplicate_keeping_oldest(session):
    older = make_invoice(email_message_id="<msg-1>", content_hash="hash-a", received_at=datetime(2026, 3, 1))
    newer = make_invoice(email_message_id="<msg-2>", content_hash="hash-a", received_at=datetime(2026, 3, 2))
    session.add_all([older, newer])
    session.commit()

    removed = reparse_invoices._dedupe_invoices(session)

    remaining = session.query(Invoice).all()
    assert removed == 1
    assert len(remaining) == 1
    assert remaining[0].id == older.id


def test_dedupe_prefers_keeping_matched_copy(session):
    matched = make_invoice(email_message_id="<msg-1>", content_hash="hash-b", status=MatchStatus.MATCHED, received_at=datetime(2026, 3, 5))
    unmatched = make_invoice(email_message_id="<msg-2>", content_hash="hash-b", status=MatchStatus.UNMATCHED, received_at=datetime(2026, 3, 1))
    session.add_all([matched, unmatched])
    session.commit()

    removed = reparse_invoices._dedupe_invoices(session)

    remaining = session.query(Invoice).all()
    assert removed == 1
    assert len(remaining) == 1
    assert remaining[0].id == matched.id


def test_dedupe_never_deletes_two_matched_copies(session):
    # Both already made a real decision -- too risky to silently pick one.
    a = make_invoice(email_message_id="<msg-1>", content_hash="hash-c", status=MatchStatus.MATCHED)
    b = make_invoice(email_message_id="<msg-2>", content_hash="hash-c", status=MatchStatus.MATCHED)
    session.add_all([a, b])
    session.commit()

    removed = reparse_invoices._dedupe_invoices(session)

    assert removed == 0
    assert session.query(Invoice).count() == 2


def test_dedupe_falls_back_to_supplier_number_amount_without_hash(session):
    a = make_invoice(email_message_id="<msg-1>", content_hash="", invoice_number="VFNL1", amount_cents=1000, supplier_name="Kruitbosch")
    b = make_invoice(email_message_id="<msg-2>", content_hash="", invoice_number="vfnl1", amount_cents=1000, supplier_name="kruitbosch")
    session.add_all([a, b])
    session.commit()

    removed = reparse_invoices._dedupe_invoices(session)

    assert removed == 1
    assert session.query(Invoice).count() == 1


def test_dedupe_leaves_genuinely_different_invoices_alone(session):
    a = make_invoice(email_message_id="<msg-1>", content_hash="hash-x")
    b = make_invoice(email_message_id="<msg-2>", content_hash="hash-y")
    session.add_all([a, b])
    session.commit()

    removed = reparse_invoices._dedupe_invoices(session)

    assert removed == 0
    assert session.query(Invoice).count() == 2


def test_top_suppliers_without_amount_reports_biggest_group_first(session, capsys):
    session.add_all([
        make_invoice(email_message_id="<msg-1>", supplier_name="Tenways", amount_cents=None),
        make_invoice(email_message_id="<msg-2>", supplier_name="Tenways", amount_cents=None),
        make_invoice(email_message_id="<msg-3>", supplier_name="Tenways", amount_cents=None),
        make_invoice(email_message_id="<msg-4>", supplier_name="ENRA", amount_cents=None),
        # Not counted: has an amount, or already matched.
        make_invoice(email_message_id="<msg-5>", supplier_name="Tenways", amount_cents=1000),
        make_invoice(email_message_id="<msg-6>", supplier_name="Tenways", amount_cents=None, status=MatchStatus.MATCHED),
    ])
    session.commit()

    reparse_invoices._print_top_suppliers_without_amount(session)

    out = capsys.readouterr().out
    assert "Tenways" in out
    assert "ENRA" in out
    assert out.index("Tenways") < out.index("ENRA")  # Tenways (3) reported before ENRA (1)


def test_top_suppliers_without_amount_silent_when_none(session, capsys):
    session.add(make_invoice(email_message_id="<msg-1>", amount_cents=1000))
    session.commit()

    reparse_invoices._print_top_suppliers_without_amount(session)

    assert capsys.readouterr().out == ""
