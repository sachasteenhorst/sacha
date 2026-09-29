"""Tests app/basecone_forward.py against mocked Graph calls -- no real mail
is ever sent. Every send_mail/list_sent_items_since/list_message_attachment_names
call goes through app.email_client's own module-level functions, which is
what's patched here."""
import os
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import basecone_forward as bf
from app.config import settings
from app.db import Base
from app.email_client import GraphApiError
from app.models import BaseconeForwardStatus, DocumentKind, Invoice


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _isolate_marker_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "AUTO_FORWARD_MARKER_FILE", str(tmp_path / "auto_forward_since.txt"))
    monkeypatch.setattr(settings, "auto_forward_from_date", "")
    monkeypatch.setattr(settings, "basecone_forward_address", "vdlg.161024@mailvanderlaangroep.nl")


def make_invoice(tmp_path, **kwargs) -> Invoice:
    pdf_path = kwargs.pop("pdf_path", None)
    if pdf_path is None:
        pdf_path = str(tmp_path / f"{kwargs.get('attachment_filename', 'factuur.pdf')}")
        with open(pdf_path, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        email_subject="Factuur 123",
        email_from="noreply@kruitbosch.nl",
        received_at=datetime(2026, 9, 1),
        invoice_number="F-123",
        amount_cents=12345,
        supplier_name="Kruitbosch",
        document_kind=DocumentKind.INVOICE.value,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
        pdf_path=pdf_path,
        graph_mailbox="facturen@vanderlindentweewielers.nl",
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


# -- normalize_subject --

def test_normalize_subject_strips_forward_and_reply_prefixes():
    assert bf.normalize_subject("RE: FW: Factuur 123") == "Factuur 123"
    assert bf.normalize_subject("Doorst.: Factuur 123") == "Factuur 123"
    assert bf.normalize_subject("Factuur 123") == "Factuur 123"


# -- is_confident / is_forwardable --

def test_is_confident_requires_number_and_amount(tmp_path):
    confident = make_invoice(tmp_path)
    assert bf.is_confident(confident) is True

    no_number = make_invoice(tmp_path, invoice_number="")
    assert bf.is_confident(no_number) is False

    no_amount = make_invoice(tmp_path, amount_cents=None)
    assert bf.is_confident(no_amount) is False


def test_is_forwardable_excludes_other_and_already_sent(tmp_path):
    assert bf.is_forwardable(make_invoice(tmp_path)) is True
    assert bf.is_forwardable(make_invoice(tmp_path, document_kind=DocumentKind.OTHER.value)) is False
    assert bf.is_forwardable(make_invoice(tmp_path, in_basecone=BaseconeForwardStatus.YES.value)) is False


def test_is_forwardable_excludes_incoming_documents(tmp_path):
    # An incoming document from a supplier with no BASECONE_INCLUDE_INCOMING
    # exception -- money coming IN, never a bill this shop needs to pay, so
    # it must never be auto-forwarded to Basecone.
    incoming = make_invoice(tmp_path, supplier_name="Onbekende Incoming Leverancier", direction="incoming")
    assert bf.is_forwardable(incoming) is False
    assert bf.not_applicable_reason(incoming) is not None
    assert bf.basecone_status_label(incoming).startswith("n.v.t.")


def test_is_forwardable_makes_an_exception_for_enra_despite_incoming_direction(tmp_path):
    # ENRA's rekening-courantoverzicht IS incoming (money flowing in), but
    # unlike HelloRider/Mobility Services it's a real document the
    # accountant needs to see, not a CycleSoftware/Twinfield duplicate --
    # BASECONE_INCLUDE_INCOMING carves out this one exception.
    enra = make_invoice(tmp_path, supplier_name="ENRA", direction="incoming")
    assert bf.is_forwardable(enra) is True
    assert bf.not_applicable_reason(enra) is None
    assert bf.basecone_status_label(enra) == "nee"


def test_is_forwardable_excludes_own_company_sales_invoices(tmp_path):
    own = make_invoice(tmp_path, supplier_name="Van der Linden Tweewielers")
    assert bf.is_forwardable(own) is False


def test_is_forwardable_excludes_configured_suppliers(tmp_path):
    # Real production case: Mobility Services' Lease a Bike "factuur" and
    # HelloRider's documents are copies of a sale this shop's own till
    # (CycleSoftware) already booked into Twinfield -- forwarding them too
    # would double-book it.
    for supplier in ("Mobility Services", "Lease a Bike", "VWPFS B.V.", "HelloRider", "CycleSoftware"):
        excluded = make_invoice(tmp_path, supplier_name=supplier)
        assert bf.is_forwardable(excluded) is False, supplier
        assert "CycleSoftware/Twinfield" in bf.basecone_status_label(excluded)


def test_is_forwardable_excludes_hellorider_even_though_it_is_incoming(tmp_path):
    # HelloRider is BOTH an incoming-direction supplier AND on the
    # exclude list -- unlike ENRA, it has no BASECONE_INCLUDE_INCOMING
    # exception, so it must stay excluded.
    hellorider = make_invoice(tmp_path, supplier_name="HelloRider", direction="incoming")
    assert bf.is_forwardable(hellorider) is False
    assert "CycleSoftware/Twinfield" in bf.basecone_status_label(hellorider)


def test_is_forwardable_true_for_normal_outgoing_supplier_invoice(tmp_path):
    normal = make_invoice(tmp_path, supplier_name="Kruitbosch", invoice_number="F-123", amount_cents=12345)
    assert bf.is_forwardable(normal) is True
    assert bf.basecone_status_label(normal) == "nee"


# -- resolve_auto_forward_since --

def test_resolve_auto_forward_since_records_today_once(monkeypatch):
    today = date.today()
    result1 = bf.resolve_auto_forward_since()
    assert result1 == today
    assert os.path.exists(bf.AUTO_FORWARD_MARKER_FILE)

    # A second call reuses the recorded marker, not "today" again.
    result2 = bf.resolve_auto_forward_since()
    assert result2 == today


def test_resolve_auto_forward_since_explicit_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_from_date", "2020-01-01")
    assert bf.resolve_auto_forward_since() == date(2020, 1, 1)


# -- send_invoice_to_basecone --

def test_send_invoice_to_basecone_success_records_method_and_recipient(tmp_path, monkeypatch):
    sent_calls = []
    monkeypatch.setattr(bf, "send_mail", lambda mailbox, message: sent_calls.append((mailbox, message)))

    inv = make_invoice(tmp_path)
    bf.send_invoice_to_basecone(inv, method="manual")

    assert len(sent_calls) == 1
    mailbox, message = sent_calls[0]
    assert mailbox == "facturen@vanderlindentweewielers.nl"
    assert message["toRecipients"][0]["emailAddress"]["address"] == settings.basecone_forward_address
    assert message["attachments"][0]["name"] == inv.attachment_filename
    assert inv.in_basecone == BaseconeForwardStatus.YES.value
    assert inv.basecone_forward_method == "manual"
    assert inv.basecone_forwarded_to == settings.basecone_forward_address
    assert inv.basecone_forwarded_at is not None


def test_send_invoice_to_basecone_falls_back_to_me_on_403(tmp_path, monkeypatch):
    calls = []

    def fake_send_mail(mailbox, message):
        calls.append(mailbox)
        if mailbox != "me":
            raise GraphApiError("Geen rechten om te versturen (Mail.Send.Shared ontbreekt).")

    monkeypatch.setattr(bf, "send_mail", fake_send_mail)
    inv = make_invoice(tmp_path)
    bf.send_invoice_to_basecone(inv, method="manual")

    assert calls == ["facturen@vanderlindentweewielers.nl", "me"]
    assert inv.in_basecone == BaseconeForwardStatus.YES.value


def test_send_invoice_to_basecone_raises_when_every_mailbox_fails(tmp_path, monkeypatch):
    def always_fail(mailbox, message):
        raise GraphApiError("nope")

    monkeypatch.setattr(bf, "send_mail", always_fail)
    inv = make_invoice(tmp_path)
    with pytest.raises(GraphApiError):
        bf.send_invoice_to_basecone(inv, method="manual")
    assert inv.in_basecone == BaseconeForwardStatus.UNKNOWN.value


def test_send_invoice_to_basecone_missing_pdf_raises(tmp_path):
    inv = make_invoice(tmp_path, pdf_path=str(tmp_path / "does-not-exist.pdf"))
    with pytest.raises(GraphApiError):
        bf.send_invoice_to_basecone(inv, method="manual")


# -- auto_forward_new_invoices --

def test_auto_forward_disabled_by_default_does_nothing(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_basecone", False)
    sent_calls = []
    monkeypatch.setattr(bf, "send_mail", lambda mailbox, message: sent_calls.append(mailbox))

    inv = make_invoice(tmp_path, received_at=datetime.utcnow())
    session.add(inv)
    session.commit()

    result = bf.auto_forward_new_invoices(session)

    assert result.sent == 0
    assert sent_calls == []
    assert inv.in_basecone == BaseconeForwardStatus.UNKNOWN.value


def test_auto_forward_only_sends_new_confident_forwardable_invoices(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_basecone", True)
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    sent_calls = []
    monkeypatch.setattr(bf, "send_mail", lambda mailbox, message: sent_calls.append(mailbox))

    new_confident = make_invoice(tmp_path, attachment_filename="a.pdf", received_at=datetime(2026, 6, 1))
    backlog = make_invoice(tmp_path, attachment_filename="b.pdf", received_at=datetime(2025, 1, 1))
    unconfident = make_invoice(tmp_path, attachment_filename="c.pdf", received_at=datetime(2026, 6, 1), invoice_number="")
    not_forwardable = make_invoice(tmp_path, attachment_filename="d.pdf", received_at=datetime(2026, 6, 1), document_kind=DocumentKind.OTHER.value)
    already_sent = make_invoice(tmp_path, attachment_filename="e.pdf", received_at=datetime(2026, 6, 1), in_basecone=BaseconeForwardStatus.YES.value)
    session.add_all([new_confident, backlog, unconfident, not_forwardable, already_sent])
    session.commit()

    result = bf.auto_forward_new_invoices(session)

    assert result.sent == 1
    assert sent_calls == ["facturen@vanderlindentweewielers.nl"]
    assert new_confident.in_basecone == BaseconeForwardStatus.YES.value
    assert backlog.in_basecone == BaseconeForwardStatus.UNKNOWN.value
    assert unconfident.in_basecone == BaseconeForwardStatus.UNKNOWN.value


def test_auto_forward_respects_max_per_sync_cap(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_basecone", True)
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    monkeypatch.setattr(settings, "auto_forward_max_per_sync", 2)
    monkeypatch.setattr(bf, "send_mail", lambda mailbox, message: None)

    invoices = [
        make_invoice(tmp_path, attachment_filename=f"f{i}.pdf", email_message_id=f"<msg-{i}>", received_at=datetime(2026, 6, 1))
        for i in range(5)
    ]
    session.add_all(invoices)
    session.commit()

    result = bf.auto_forward_new_invoices(session)

    assert result.sent == 2


def test_auto_forward_collects_errors_without_stopping(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_basecone", True)
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")

    def fail_on_first(mailbox, message):
        raise GraphApiError("boom")

    monkeypatch.setattr(bf, "send_mail", fail_on_first)
    inv = make_invoice(tmp_path, received_at=datetime(2026, 6, 1))
    session.add(inv)
    session.commit()

    result = bf.auto_forward_new_invoices(session)

    assert result.sent == 0
    assert len(result.errors) == 1
    assert inv.in_basecone == BaseconeForwardStatus.UNKNOWN.value


# -- check_backlog_against_sent_items --

def test_backlog_detection_marks_matching_subject_as_forwarded(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")

    backlog_inv = make_invoice(tmp_path, email_subject="Factuur 123", received_at=datetime(2025, 1, 1))
    session.add(backlog_inv)
    session.commit()

    sent_message = {
        "id": "sent-1",
        "subject": "RE: Factuur 123",
        "toRecipients": [{"emailAddress": {"address": settings.basecone_forward_address}}],
        "ccRecipients": [],
        "bccRecipients": [],
    }
    monkeypatch.setattr(bf, "list_sent_items_since", lambda mailbox, since: [sent_message])
    monkeypatch.setattr(bf, "list_message_attachment_names", lambda mailbox, message_id: [])

    updated = bf.check_backlog_against_sent_items(session)

    assert updated == 1
    assert backlog_inv.in_basecone == BaseconeForwardStatus.YES.value
    assert backlog_inv.basecone_forward_method == "detected"


def test_backlog_detection_marks_matching_attachment_filename_as_forwarded(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")

    backlog_inv = make_invoice(tmp_path, email_subject="Iets anders", attachment_filename="factuur-987.pdf", received_at=datetime(2025, 1, 1))
    session.add(backlog_inv)
    session.commit()

    sent_message = {
        "id": "sent-1",
        "subject": "Doorgestuurd",
        "toRecipients": [{"emailAddress": {"address": settings.basecone_forward_address}}],
        "ccRecipients": [],
        "bccRecipients": [],
    }
    monkeypatch.setattr(bf, "list_sent_items_since", lambda mailbox, since: [sent_message])
    monkeypatch.setattr(bf, "list_message_attachment_names", lambda mailbox, message_id: ["factuur-987.pdf"])

    updated = bf.check_backlog_against_sent_items(session)

    assert updated == 1
    assert backlog_inv.in_basecone == BaseconeForwardStatus.YES.value


def test_backlog_detection_leaves_unmatched_invoices_unknown(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")

    backlog_inv = make_invoice(tmp_path, email_subject="Factuur 999", received_at=datetime(2025, 1, 1))
    session.add(backlog_inv)
    session.commit()

    monkeypatch.setattr(bf, "list_sent_items_since", lambda mailbox, since: [])

    updated = bf.check_backlog_against_sent_items(session)

    assert updated == 0
    assert backlog_inv.in_basecone == BaseconeForwardStatus.UNKNOWN.value


def test_backlog_detection_ignores_sent_message_not_to_basecone(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_forward_from_date", "2026-01-01")
    monkeypatch.setattr(settings, "graph_mailbox", "facturen@vanderlindentweewielers.nl")

    backlog_inv = make_invoice(tmp_path, email_subject="Factuur 123", received_at=datetime(2025, 1, 1))
    session.add(backlog_inv)
    session.commit()

    unrelated_message = {
        "id": "sent-1",
        "subject": "Factuur 123",
        "toRecipients": [{"emailAddress": {"address": "someone-else@example.com"}}],
        "ccRecipients": [],
        "bccRecipients": [],
    }
    monkeypatch.setattr(bf, "list_sent_items_since", lambda mailbox, since: [unrelated_message])

    updated = bf.check_backlog_against_sent_items(session)

    assert updated == 0
    assert backlog_inv.in_basecone == BaseconeForwardStatus.UNKNOWN.value
