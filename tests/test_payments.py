from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import DocumentKind, Invoice, LearnedIncassoSupplier, MatchStatus, PaymentMethod, Transaction
from app.payments import (
    days_until_due,
    determine_payment_method,
    is_payable_invoice,
    learn_incasso_supplier,
    mark_paid,
)


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
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def make_transaction(**kwargs):
    defaults = dict(
        external_ref="tx-1",
        booking_date=date(2026, 9, 1),
        amount_cents=-12000,
        counterparty_name="Kruitbosch",
        raw_data={},
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


# -- is_payable_invoice --

def test_is_payable_invoice_true_for_open_outgoing_non_incasso_invoice(session):
    inv = make_invoice()
    assert is_payable_invoice(inv) is True


def test_is_payable_invoice_false_for_incoming_document(session):
    inv = make_invoice(direction="incoming")
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_for_sales_invoice(session):
    inv = make_invoice(document_kind=DocumentKind.SALES_INVOICE.value, direction="incoming")
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_for_incasso(session):
    inv = make_invoice(payment_method=PaymentMethod.INCASSO.value)
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_when_already_matched(session):
    inv = make_invoice(status=MatchStatus.MATCHED)
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_when_marked_paid_by_hand(session):
    inv = make_invoice()
    mark_paid(inv)
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_for_credit_note_or_zero_amount(session):
    assert is_payable_invoice(make_invoice(amount_cents=0)) is False
    assert is_payable_invoice(make_invoice(amount_cents=-500)) is False
    assert is_payable_invoice(make_invoice(amount_cents=None)) is False


def test_is_payable_invoice_false_without_due_date(session):
    assert is_payable_invoice(make_invoice(due_date=None)) is False


def test_is_payable_invoice_false_for_other_document_kind(session):
    assert is_payable_invoice(make_invoice(document_kind=DocumentKind.OTHER.value)) is False


# -- days_until_due --

def test_days_until_due_negative_when_overdue(session):
    inv = make_invoice(due_date=date(2026, 9, 1))
    assert days_until_due(inv, today=date(2026, 9, 5)) == -4


def test_days_until_due_positive_when_not_yet_due(session):
    inv = make_invoice(due_date=date(2026, 9, 10))
    assert days_until_due(inv, today=date(2026, 9, 5)) == 5


# -- determine_payment_method --

def test_determine_payment_method_incasso_from_text_hint(session):
    assert determine_payment_method(session, "Onbekende Leverancier", incasso_hint=True) == PaymentMethod.INCASSO.value


def test_determine_payment_method_incasso_from_configured_supplier_list(session):
    # Gazelle is on the default PAY_INCASSO_SUPPLIERS list.
    assert determine_payment_method(session, "Gazelle", incasso_hint=False) == PaymentMethod.INCASSO.value


def test_determine_payment_method_onbekend_for_unknown_supplier_no_hint(session):
    assert determine_payment_method(session, "Onbekende Leverancier", incasso_hint=False) == PaymentMethod.ONBEKEND.value


def test_determine_payment_method_manual_override_wins_over_configured_list(session, monkeypatch):
    monkeypatch.setattr(settings, "pay_manual_suppliers", "Gazelle")
    assert determine_payment_method(session, "Gazelle", incasso_hint=True) == PaymentMethod.ONBEKEND.value


def test_determine_payment_method_from_existing_incasso_bank_transaction(session):
    # A supplier not on any list -- but a real bank transaction from a
    # name-similar counterparty already shows Rabobank's own "ei" incasso
    # code, which is proof enough this supplier collects by direct debit.
    session.add(make_transaction(counterparty_name="Onbekende Leverancier B.V.", bank_code="ei"))
    session.commit()
    assert determine_payment_method(session, "Onbekende Leverancier", incasso_hint=False) == PaymentMethod.INCASSO.value


def test_determine_payment_method_from_machtigingskenmerk_raw_data(session):
    session.add(make_transaction(counterparty_name="Onbekende Leverancier B.V.", raw_data={"Machtigingskenmerk": "ABC123"}))
    session.commit()
    assert determine_payment_method(session, "Onbekende Leverancier", incasso_hint=False) == PaymentMethod.INCASSO.value


def test_determine_payment_method_ignores_transaction_without_incasso_signal(session):
    session.add(make_transaction(counterparty_name="Onbekende Leverancier B.V.", bank_code="ba"))
    session.commit()
    assert determine_payment_method(session, "Onbekende Leverancier", incasso_hint=False) == PaymentMethod.ONBEKEND.value


# -- learn_incasso_supplier --

def test_learn_incasso_supplier_records_and_reclassifies_existing_invoices(session):
    inv1 = make_invoice(supplier_name="Nieuwe Leverancier", email_message_id="<m1>")
    inv2 = make_invoice(supplier_name="Nieuwe Leverancier BV", email_message_id="<m2>")
    session.add_all([inv1, inv2])
    session.commit()

    learn_incasso_supplier(session, "Nieuwe Leverancier")
    session.commit()

    assert inv1.payment_method == PaymentMethod.INCASSO.value
    assert inv2.payment_method == PaymentMethod.INCASSO.value
    assert session.query(LearnedIncassoSupplier).count() == 1

    # A NEW invoice from the same supplier is now recognised as incasso too.
    assert determine_payment_method(session, "Nieuwe Leverancier", incasso_hint=False) == PaymentMethod.INCASSO.value


def test_learn_incasso_supplier_does_not_duplicate_on_repeated_calls(session):
    learn_incasso_supplier(session, "Herhaal Leverancier")
    learn_incasso_supplier(session, "Herhaal Leverancier")
    session.commit()
    assert session.query(LearnedIncassoSupplier).count() == 1
