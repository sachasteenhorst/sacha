"""Tests scripts/label_mails.py's dry-run/--apply behavior against an
isolated in-memory database -- app.mail_labels.set_invoice_category itself
is monkeypatched, since this script's own job is just "find the right
invoices and call it", already covered end-to-end in test_mail_labels.py.
"""
import sys
from datetime import datetime, timedelta

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
    import scripts.label_mails as label_mails

    monkeypatch.setattr(label_mails, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)  # the fixture closes it, not main()
    argv = ["label_mails.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    label_mails.main()


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        received_at=datetime.utcnow() - timedelta(days=30),
        supplier_name="Kruitbosch",
        amount_cents=5000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        graph_mailbox="facturen@vanderlindentweewielers.nl",
        graph_message_id="msg-1",
        graph_category="",
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_dry_run_changes_nothing(monkeypatch, session):
    import scripts.label_mails as label_mails

    inv = make_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    called = []
    monkeypatch.setattr(label_mails, "set_invoice_category", lambda i: called.append(i.id) or True)

    _run_main(monkeypatch, session, apply=False)

    assert called == []
    assert session.get(Invoice, inv_id).graph_category == ""


def test_apply_labels_matching_invoices(monkeypatch, session):
    import scripts.label_mails as label_mails

    inv = make_invoice()
    session.add(inv)
    session.commit()
    inv_id = inv.id

    def fake_set_category(invoice):
        invoice.graph_category = "Nog naar Basecone"
        return True

    monkeypatch.setattr(label_mails, "set_invoice_category", fake_set_category)

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, inv_id).graph_category == "Nog naar Basecone"


def test_ignores_invoices_older_than_12_months(monkeypatch, session):
    import scripts.label_mails as label_mails

    old_inv = make_invoice(
        email_message_id="<old>", received_at=datetime.utcnow() - timedelta(days=400),
    )
    session.add(old_inv)
    session.commit()

    called = []
    monkeypatch.setattr(label_mails, "set_invoice_category", lambda i: called.append(i.id) or True)

    _run_main(monkeypatch, session, apply=True)

    assert called == []


def test_skips_invoices_without_a_graph_message_id(monkeypatch, session):
    import scripts.label_mails as label_mails

    inv = make_invoice(email_message_id="<no-id>", graph_message_id="")
    session.add(inv)
    session.commit()

    called = []
    monkeypatch.setattr(label_mails, "set_invoice_category", lambda i: called.append(i.id) or True)

    _run_main(monkeypatch, session, apply=True)

    assert called == []


def test_stops_on_auth_error_but_commits_what_already_changed(monkeypatch, session):
    import scripts.label_mails as label_mails
    from app.email_client import GraphAuthError

    inv1 = make_invoice(email_message_id="<a>")
    inv2 = make_invoice(email_message_id="<b>")
    session.add_all([inv1, inv2])
    session.commit()
    inv1_id, inv2_id = inv1.id, inv2.id

    def fake_set_category(invoice):
        if invoice.email_message_id == "<a>":
            invoice.graph_category = "Nog naar Basecone"
            return True
        raise GraphAuthError("niet ingelogd")

    monkeypatch.setattr(label_mails, "set_invoice_category", fake_set_category)

    _run_main(monkeypatch, session, apply=True)

    assert session.get(Invoice, inv1_id).graph_category == "Nog naar Basecone"
    assert session.get(Invoice, inv2_id).graph_category == ""
