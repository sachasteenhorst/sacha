"""Tests app/db.py's manually_ignored backfill logic in isolation, by
monkeypatching its SessionLocal to point at a throwaway in-memory database
(the real module-level engine stays untouched)."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.db import Base
from app.models import DocumentKind, Invoice, MatchStatus


@pytest.fixture()
def isolated_session_local(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", Session)
    return Session


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime(2026, 3, 1),
        supplier_name="Kruitbosch",
        document_kind=DocumentKind.INVOICE.value,
        status=MatchStatus.UNMATCHED,
        manually_ignored=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_backfill_marks_ignored_other_documents_as_not_manual(isolated_session_local):
    Session = isolated_session_local
    with Session() as session:
        inv = make_invoice(document_kind=DocumentKind.OTHER.value, status=MatchStatus.IGNORED)
        session.add(inv)
        session.commit()
        inv_id = inv.id

    db_module._backfill_manually_ignored()

    with Session() as session:
        inv = session.get(Invoice, inv_id)
        assert inv.manually_ignored is False


def test_backfill_marks_ignored_non_other_documents_as_manual(isolated_session_local):
    # The auto-ignore path (sync.py/reparse) never sets IGNORED on a
    # document classified as invoice/specification -- only the dashboard's
    # manual "Negeren" button does, so this must be protected.
    Session = isolated_session_local
    with Session() as session:
        inv = make_invoice(document_kind=DocumentKind.INVOICE.value, status=MatchStatus.IGNORED)
        session.add(inv)
        session.commit()
        inv_id = inv.id

    db_module._backfill_manually_ignored()

    with Session() as session:
        inv = session.get(Invoice, inv_id)
        assert inv.manually_ignored is True


def test_backfill_leaves_non_ignored_invoices_untouched(isolated_session_local):
    Session = isolated_session_local
    with Session() as session:
        inv = make_invoice(document_kind=DocumentKind.OTHER.value, status=MatchStatus.UNMATCHED)
        session.add(inv)
        session.commit()
        inv_id = inv.id

    db_module._backfill_manually_ignored()

    with Session() as session:
        inv = session.get(Invoice, inv_id)
        assert inv.manually_ignored is False
