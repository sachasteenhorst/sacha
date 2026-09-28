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
