"""Tests scripts/fix_cycletech.py's dry-run/--apply behavior against an
isolated in-memory database -- covers both halves of this one script:
CycleSoftware B.V.'s real vendor invoices (wrongly excluded from Basecone
before, see app/config.py's BASECONE_EXCLUDE_SUPPLIERS fix) and Cycletech's
classification/invoice-number fix."""
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BaseconeForwardStatus, DocumentKind, Invoice, MatchStatus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _run_main(monkeypatch, session, *, apply: bool):
    import scripts.fix_cycletech as fix_cycletech

    monkeypatch.setattr(fix_cycletech, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)  # the fixture closes it, not main()
    argv = ["fix_cycletech.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    fix_cycletech.main()


def make_cyclesoftware_invoice(**kwargs):
    defaults = dict(
        email_message_id="<cs-1>",
        attachment_filename="Factuur-2027026.pdf",
        email_subject="Hierbij je factuur 2027026 van CycleSoftware.",
        email_from="noreply@twsc.nl",
        received_at=datetime(2026, 6, 1),
        invoice_number="2027026",
        supplier_name="CycleSoftware",
        amount_cents=81297,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        excluded_from_auto_forward=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def make_cycletech_invoice(**kwargs):
    defaults = dict(
        email_message_id="<ct-1>",
        attachment_filename="Factuur_122951-482075(1).pdf",
        email_subject="Factuur 482075",
        email_from="administratie@cycletech.nl",
        received_at=datetime(2026, 6, 1),
        invoice_number="122951",
        supplier_name="Cycletech",
        amount_cents=4500,
        document_kind=DocumentKind.OTHER.value,
        direction="outgoing",
        status=MatchStatus.IGNORED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        excluded_from_auto_forward=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


# -- CycleSoftware B.V. half --

def test_dry_run_changes_nothing(session, monkeypatch):
    inv = make_cyclesoftware_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=False)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "CycleSoftware"
    assert fresh.excluded_from_auto_forward is False


def test_apply_normalises_cyclesoftware_supplier_name_and_excludes_from_auto_forward(session, monkeypatch):
    inv = make_cyclesoftware_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "CycleSoftware B.V."
    assert fresh.excluded_from_auto_forward is True
    assert fresh.document_kind == DocumentKind.INVOICE.value  # untouched, was already correct


def test_apply_never_touches_already_forwarded_cyclesoftware_invoice(session, monkeypatch):
    inv = make_cyclesoftware_invoice(in_basecone=BaseconeForwardStatus.YES.value)
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    # Already sent -- no need to exclude it from anything.
    assert fresh.excluded_from_auto_forward is False


def test_cyclesoftware_sales_export_row_is_never_touched(session, monkeypatch):
    sales = Invoice(
        email_message_id="cyclesoftware:F-1", attachment_filename="cyclesoftware-export",
        email_from="cyclesoftware-export", received_at=datetime(2026, 6, 1),
        supplier_name="Jan de Klant", amount_cents=5000,
        document_kind=DocumentKind.SALES_INVOICE.value, direction="incoming",
        status=MatchStatus.UNMATCHED,
    )
    session.add(sales)
    session.commit()
    sales_id = sales.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, sales_id)
    assert fresh.supplier_name == "Jan de Klant"
    assert fresh.excluded_from_auto_forward is False


# -- Cycletech half --

def test_apply_reclassifies_cycletech_invoice_and_fixes_invoice_number(session, monkeypatch):
    inv = make_cycletech_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.supplier_name == "Cycletech"
    assert fresh.document_kind == DocumentKind.INVOICE.value
    assert fresh.invoice_number == "482075"
    assert fresh.excluded_from_auto_forward is True


def test_apply_cycletech_bundled_incasso_overzicht_copy_gets_same_fix(session, monkeypatch):
    inv = make_cycletech_invoice(
        attachment_filename="Factuur_122951-482075.pdf",
        email_subject="CYCLETECH | Incasso overzicht",
        invoice_number="482075(1)",
    )
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    assert fresh.invoice_number == "482075"
    assert fresh.document_kind == DocumentKind.INVOICE.value


def test_apply_leaves_already_correct_cycletech_invoice_untouched(session, monkeypatch):
    inv = make_cycletech_invoice(
        supplier_name="Cycletech", document_kind=DocumentKind.INVOICE.value, invoice_number="482075",
    )
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, inv_id)
    # Was already correct (not OTHER before this run) -- never touch this flag.
    assert fresh.excluded_from_auto_forward is False


def test_unrelated_invoice_is_never_touched(session, monkeypatch):
    other = Invoice(
        email_message_id="<other-1>", attachment_filename="f.pdf",
        received_at=datetime(2026, 6, 1), supplier_name="Kruitbosch",
        amount_cents=5000, document_kind=DocumentKind.INVOICE.value,
        direction="outgoing", status=MatchStatus.UNMATCHED,
        email_from="noreply@kruitbosch.nl",
    )
    session.add(other)
    session.commit()
    other_id = other.id

    _run_main(monkeypatch, session, apply=True)

    fresh = session.get(Invoice, other_id)
    assert fresh.supplier_name == "Kruitbosch"
    assert fresh.excluded_from_auto_forward is False
