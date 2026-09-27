from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.matcher import run_matching
from app.models import Invoice, MatchStatus, Transaction


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def make_transaction(**kwargs):
    defaults = dict(
        basecone_id="tx-1",
        booking_date=date(2024, 3, 10),
        amount_cents=-12345,
        description="Betaling factuur",
        counterparty_name="Leverancier BV",
        counterparty_iban="NL00BANK0123456789",
        reference="",
        raw_data={},
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        email_subject="Factuur",
        email_from="verkoop@leverancier.nl",
        received_at=date(2024, 3, 1),
        invoice_number="F-2024-001",
        invoice_date=date(2024, 3, 1),
        supplier_name="Leverancier BV",
        amount_cents=12345,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_reference_match_is_auto_confirmed(session):
    tx = make_transaction(description="Betaling factuur F-2024-001")
    inv = make_invoice()
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert tx.status == MatchStatus.MATCHED
    assert inv.status == MatchStatus.MATCHED
    assert tx.matches[0].confirmed is True


def test_amount_date_match_is_suggested_not_confirmed(session):
    tx = make_transaction(description="Overboeking", booking_date=date(2024, 3, 15))
    inv = make_invoice(invoice_number="", invoice_date=date(2024, 3, 1))
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.suggested == 1
    assert tx.status == MatchStatus.SUGGESTED
    assert inv.status == MatchStatus.SUGGESTED
    assert tx.matches[0].confirmed is False


def test_no_candidate_stays_unmatched(session):
    tx = make_transaction(amount_cents=-9999)
    inv = make_invoice(amount_cents=12345, invoice_date=date(2024, 1, 1))
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 0
    assert summary.suggested == 0
    assert tx.status == MatchStatus.UNMATCHED
    assert inv.status == MatchStatus.UNMATCHED


def test_amount_outside_date_window_not_matched(session):
    tx = make_transaction(booking_date=date(2024, 6, 1))
    inv = make_invoice(invoice_number="", invoice_date=date(2024, 1, 1))  # >150 days away
    session.add_all([tx, inv])
    session.commit()

    run_matching(session)
    session.commit()

    assert tx.status == MatchStatus.UNMATCHED
    assert inv.status == MatchStatus.UNMATCHED
