"""Tests app/vandaag.py's build_vandaag_overzicht -- the single source of
truth shared by the dashboard's "Vandaag voor jou" block and the daily push
notification, so its three buckets are tested here once rather than
separately in test_notify.py/against the dashboard route.
"""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BaseconeForwardStatus, DocumentKind, Invoice, MatchStatus, PaymentMethod, Transaction
from app.vandaag import build_vandaag_overzicht


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
        received_at=datetime(2026, 9, 1),
        invoice_number="F-1",
        supplier_name="Kruitbosch",
        amount_cents=12000,
        document_kind=DocumentKind.INVOICE.value,
        direction="outgoing",
        status=MatchStatus.UNMATCHED,
        payment_method=PaymentMethod.ONBEKEND.value,
        due_date=date(2026, 9, 30),
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def make_transaction(**kwargs):
    defaults = dict(
        external_ref="csv:1",
        booking_date=date(2026, 9, 1),
        amount_cents=-1000,
        counterparty_name="Leverancier",
        raw_data={},
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def test_empty_database_is_alles_in_orde(session):
    overzicht = build_vandaag_overzicht(session)
    assert overzicht.alles_in_orde is True
    assert overzicht.totaal == 0


def test_payable_invoice_counts_towards_facturen_te_betalen(session):
    session.add(make_invoice())
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert len(overzicht.facturen_te_betalen) == 1
    assert overzicht.alles_in_orde is False


def test_incasso_invoice_does_not_count_as_facturen_te_betalen(session):
    # Still forwardable to Basecone (an incasso still needs a bonnetje at
    # the accountant), just never something Sacha has to pay by hand.
    session.add(make_invoice(payment_method=PaymentMethod.INCASSO.value))
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert overzicht.facturen_te_betalen == []


def test_unmatched_transaction_counts_towards_banktransacties_zonder_factuur(session):
    session.add(make_transaction())
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert len(overzicht.banktransacties_zonder_factuur) == 1
    assert overzicht.alles_in_orde is False


def test_rule_handled_transaction_does_not_count(session):
    # A transaction a Rule already resolved (e.g. huur/salaris/managementfee/
    # prive-opname/lease-ontvangst met een regel) must never show up here --
    # that's exactly what app.rules' RULE_HANDLED status is for.
    session.add(make_transaction(status=MatchStatus.RULE_HANDLED))
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert overzicht.banktransacties_zonder_factuur == []
    assert overzicht.alles_in_orde is True


def test_invoice_not_yet_forwarded_counts_towards_niet_naar_basecone(session):
    session.add(make_invoice(
        document_kind=DocumentKind.INVOICE.value,
        in_basecone="unknown",
        supplier_name="Kruitbosch",
    ))
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert len(overzicht.niet_naar_basecone) == 1


def test_cyclesoftware_style_excluded_supplier_never_counts_towards_basecone(session, monkeypatch):
    # Mobility Services/Lease a Bike/VWPFS/HelloRider are excluded from
    # Basecone forwarding entirely (see app.basecone_forward) -- they must
    # never inflate "Vandaag voor jou" either.
    from app.config import settings

    monkeypatch.setattr(settings, "basecone_exclude_suppliers", "Mobility Services")
    session.add(make_invoice(
        supplier_name="Mobility Services B.V.",
        direction="incoming",
        in_basecone="unknown",
    ))
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert overzicht.niet_naar_basecone == []


def test_already_forwarded_invoice_does_not_count(session):
    session.add(make_invoice(in_basecone=BaseconeForwardStatus.YES.value))
    session.commit()

    overzicht = build_vandaag_overzicht(session)

    assert overzicht.niet_naar_basecone == []
