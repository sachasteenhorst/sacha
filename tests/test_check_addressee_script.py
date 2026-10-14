"""Tests scripts/check_addressee.py's dry-run/--apply behavior against an
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
    import scripts.check_addressee as check_addressee

    monkeypatch.setattr(check_addressee, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)  # the fixture closes it, not main()
    argv = ["check_addressee.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    check_addressee.main()


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime(2026, 6, 1),
        invoice_number="F-1",
        supplier_name="Kruitbosch",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        extracted_text="",
        addressee_status="ok",
        addressee_reviewed=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_dry_run_changes_nothing(session, monkeypatch):
    inv = make_invoice(extracted_text="Factuur voor J. de Klant, Dorpsstraat 1")
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=False)

    assert session.get(Invoice, inv_id).addressee_status == "ok"


def test_apply_flags_invoice_not_addressed_to_us(session, monkeypatch):
    inv = make_invoice(extracted_text="Factuur voor J. de Klant, Dorpsstraat 1")
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, inv_id).addressee_status == "not_for_us"


def test_apply_leaves_invoice_addressed_to_us_as_ok(session, monkeypatch):
    inv = make_invoice(extracted_text="Factuur voor Hing B.V., Fietsstraat 1")
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, inv_id).addressee_status == "ok"


def test_apply_flags_unknown_for_empty_text(session, monkeypatch):
    inv = make_invoice(extracted_text="")
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, inv_id).addressee_status == "unknown"


def test_apply_never_touches_a_reviewed_invoice(session, monkeypatch):
    inv = make_invoice(
        extracted_text="Factuur voor J. de Klant, Dorpsstraat 1",
        addressee_status="ok", addressee_reviewed=True,
    )
    session.add(inv)
    session.commit()
    inv_id = inv.id

    _run_main(monkeypatch, session, apply=True)

    # Sacha already clicked "Toch voor ons" -- never flip it back.
    assert session.get(Invoice, inv_id).addressee_status == "ok"


def test_sales_invoice_is_never_checked(session, monkeypatch):
    sales = make_invoice(
        document_kind=DocumentKind.SALES_INVOICE.value, direction="incoming",
        extracted_text="Factuur voor J. de Klant, Dorpsstraat 1",
    )
    session.add(sales)
    session.commit()
    sales_id = sales.id

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, sales_id).addressee_status == "ok"


def test_incoming_document_is_never_checked(session, monkeypatch):
    incoming = make_invoice(
        direction="incoming", extracted_text="Factuur voor J. de Klant, Dorpsstraat 1",
    )
    session.add(incoming)
    session.commit()
    incoming_id = incoming.id

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, incoming_id).addressee_status == "ok"


def test_counts_in_output(session, monkeypatch, capsys):
    session.add_all([
        make_invoice(email_message_id="<a>", extracted_text="Factuur voor Hing B.V."),
        make_invoice(email_message_id="<b>", extracted_text="Factuur voor J. de Klant, Dorpsstraat 1"),
        make_invoice(email_message_id="<c>", extracted_text=""),
    ])
    session.commit()

    _run_main(monkeypatch, session, apply=False)

    out = capsys.readouterr().out
    assert "Aan ons gericht (ok):     1" in out
    assert "Niet aan ons gericht:     1" in out
    assert "Geadresseerde onbekend:   1" in out
