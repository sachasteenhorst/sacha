"""Tests scripts/fix_tenways.py's dry-run/--apply behavior against an
isolated in-memory database."""
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import DocumentKind, Invoice, MatchStatus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _run_main(monkeypatch, session, *, apply: bool):
    import scripts.fix_tenways as fix_tenways

    monkeypatch.setattr(fix_tenways, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)  # the fixture closes it, not main()
    argv = ["fix_tenways.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    fix_tenways.main()


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<tenways-1>",
        attachment_filename="INV_2026_50218.pdf",
        email_subject="Tenways Technovation Europe B.V. Invoice (Ref INV/2026/50218)",
        email_from="notifications@tenways.com",
        received_at=datetime(2026, 6, 1),
        invoice_number="INV/2026/50218",
        supplier_name="Derk ten Kleij",
        amount_cents=15000,
        document_kind=DocumentKind.OTHER.value,
        direction="outgoing",
        status=MatchStatus.IGNORED,
        extracted_text="",
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_dry_run_changes_nothing(session, monkeypatch):
    inv = make_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=False)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "Derk ten Kleij"
    assert fresh.document_kind == DocumentKind.OTHER.value
    assert fresh.excluded_from_auto_forward is False


def test_apply_normalises_supplier_name_and_reclassifies_invoice(session, monkeypatch):
    inv = make_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "Tenways"
    assert fresh.document_kind == DocumentKind.INVOICE.value
    # Was OTHER, is now forwardable -- must not get auto-forwarded by surprise.
    assert fresh.excluded_from_auto_forward is True


def test_apply_credit_note_becomes_invoice_kind(session, monkeypatch):
    inv = make_invoice(
        attachment_filename="RINV_2026_03285.pdf",
        email_subject="Tenways Technovation Europe B.V. Credit",
        invoice_number="RINV/2026/03285",
    )
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.document_kind == DocumentKind.INVOICE.value
    assert fresh.excluded_from_auto_forward is True


def test_apply_quote_stays_other_and_is_never_excluded_from_auto_forward(session, monkeypatch):
    inv = make_invoice(
        attachment_filename="S0123456.pdf",
        email_subject="Quotation for your new bikes",
        invoice_number="",
        amount_cents=None,
    )
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "Tenways"
    assert fresh.document_kind == DocumentKind.OTHER.value
    assert fresh.excluded_from_auto_forward is False


def test_apply_leaves_already_correct_invoice_untouched(session, monkeypatch):
    inv = make_invoice(supplier_name="Tenways", document_kind=DocumentKind.INVOICE.value)
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "Tenways"
    assert fresh.document_kind == DocumentKind.INVOICE.value
    # Was already correct (not OTHER before this run) -- never touch this flag.
    assert fresh.excluded_from_auto_forward is False


def test_apply_never_touches_in_basecone_status(session, monkeypatch):
    from app.models import BaseconeForwardStatus

    inv = make_invoice(in_basecone=BaseconeForwardStatus.UNKNOWN.value)
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.in_basecone == BaseconeForwardStatus.UNKNOWN.value


def test_candidates_matched_by_supplier_name_even_without_tenways_domain(session, monkeypatch):
    # An invoice already normalised to "Tenways" (e.g. by a future mail
    # sync, not just the backfill) should still be found and reported on.
    inv = make_invoice(email_from="noreply@someotherdomain.example", supplier_name="Tenways")
    session.add(inv)
    session.commit()

    _run_main(monkeypatch, session, apply=False)  # must not raise / must find it

    assert session.get(Invoice, inv.id).supplier_name == "Tenways"


def test_unrelated_invoice_is_never_touched(session, monkeypatch):
    other = Invoice(
        email_message_id="<other-1>", attachment_filename="f.pdf",
        received_at=datetime(2026, 6, 1), supplier_name="Kruitbosch",
        amount_cents=5000, document_kind=DocumentKind.INVOICE.value,
        direction="outgoing", status=MatchStatus.UNMATCHED,
        email_from="noreply@kruitbosch.nl", extracted_text="",
    )
    session.add(other)
    session.commit()
    other_id = other.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, other_id)
    assert fresh.supplier_name == "Kruitbosch"
    assert fresh.document_kind == DocumentKind.INVOICE.value
    assert fresh.excluded_from_auto_forward is False
