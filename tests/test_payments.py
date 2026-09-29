from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import DocumentKind, Invoice, LearnedIncassoSupplier, MatchStatus, PaymentMethod, Transaction
from app.payments import (
    dedupe_key,
    deduplicate_invoices,
    days_until_due,
    determine_payment_method,
    get_old_unreviewed_invoices,
    get_payable_invoices,
    is_duplicate_of_existing,
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


# -- is_payable_invoice: stricter exclusions (production bug: 258 false
# "Nog te betalen" entries after the previous round's deploy) --

def test_is_payable_invoice_false_when_own_vat_number_read_as_invoice_number(session):
    # Real production bug: a document where our OWN BTW-nummer got read as
    # the "factuurnummer" is, by definition, about us as the seller.
    inv = make_invoice(invoice_number="NL866688730B01")
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_false_for_consumer_email_domains(session):
    for domain in ("gmail.com", "hotmail.com", "icloud.com", "ziggo.nl", "kpnmail.nl"):
        inv = make_invoice(email_from=f"iemand@{domain}", email_message_id=f"<{domain}>")
        assert is_payable_invoice(inv) is False, domain


def test_is_payable_invoice_true_for_business_email_domain(session):
    inv = make_invoice(email_from="facturen@kruitbosch.nl")
    assert is_payable_invoice(inv) is True


def test_is_payable_invoice_false_when_not_addressed_to_own_company(session):
    # No mention of "Van der Linden"/"Hing" anywhere in the document text --
    # more likely addressed to a customer or private individual than a bill
    # this shop owes.
    inv = make_invoice(extracted_text="Factuur voor J. de Klant, Dorpsstraat 1, Amsterdam. Totaal: EUR 120,00")
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_true_when_addressed_to_own_company(session):
    inv = make_invoice(extracted_text="Factuur voor Van der Linden Tweewielers, Hoofdstraat 1. Totaal: EUR 120,00")
    assert is_payable_invoice(inv) is True


def test_is_payable_invoice_not_excluded_when_no_text_at_all(session):
    # No extracted_text to check against at all -- not enough signal to
    # exclude on the addressee-name basis alone.
    inv = make_invoice(extracted_text="")
    assert is_payable_invoice(inv) is True


def test_is_payable_invoice_false_for_incasso_specification_subjects(session):
    # Giant's English-language incasso announcement isn't matched by the
    # Dutch-only SPECIFICATION_RE in app.email_client, so it needs its own
    # subject-based exclusion here.
    dutch = make_invoice(email_subject="Specificatie automatische incasso 12345", email_message_id="<a>")
    english = make_invoice(email_subject="Advance Notification of Direct Debit", email_message_id="<b>")
    assert is_payable_invoice(dutch) is False
    assert is_payable_invoice(english) is False


# -- is_payable_invoice: PAY_FROM_DATE cutoff --

def test_is_payable_invoice_false_for_invoice_received_before_pay_from_date(session):
    inv = make_invoice(received_at=datetime(2026, 8, 31))
    assert is_payable_invoice(inv) is False


def test_is_payable_invoice_true_for_invoice_received_exactly_on_pay_from_date(session):
    inv = make_invoice(received_at=datetime(2026, 9, 1))
    assert is_payable_invoice(inv) is True


def test_is_payable_invoice_true_for_invoice_received_after_pay_from_date(session):
    inv = make_invoice(received_at=datetime(2026, 9, 2))
    assert is_payable_invoice(inv) is True


def test_get_old_unreviewed_invoices_returns_old_would_be_payable_invoices(session):
    old = make_invoice(received_at=datetime(2026, 1, 1))
    recent = make_invoice(received_at=datetime(2026, 9, 5), email_message_id="<recent>", invoice_number="F-2")
    session.add_all([old, recent])
    session.commit()

    old_ones = get_old_unreviewed_invoices(session)

    assert [inv.id for inv in old_ones] == [old.id]


def test_get_old_unreviewed_invoices_excludes_genuinely_ineligible_ones(session):
    # Old AND incasso -- never belonged on "Nog te betalen" for a reason
    # that has nothing to do with its age, so it must not show up here either.
    old_incasso = make_invoice(received_at=datetime(2026, 1, 1), payment_method=PaymentMethod.INCASSO.value)
    session.add(old_incasso)
    session.commit()

    assert get_old_unreviewed_invoices(session) == []


# -- dedupe_key / deduplicate_invoices / is_duplicate_of_existing --

def test_dedupe_key_none_without_invoice_number(session):
    assert dedupe_key(make_invoice(invoice_number="")) is None


def test_dedupe_key_same_for_matching_supplier_number_amount(session):
    a = make_invoice(supplier_name="Kruitbosch", invoice_number="F-100", amount_cents=5000)
    b = make_invoice(supplier_name="KRUITBOSCH", invoice_number="f-100", amount_cents=5000)
    assert dedupe_key(a) == dedupe_key(b)


def test_deduplicate_invoices_keeps_earliest_received(session):
    original = make_invoice(email_message_id="<a>", received_at=datetime(2026, 9, 1))
    reminder = make_invoice(email_message_id="<b>", received_at=datetime(2026, 9, 15))
    result = deduplicate_invoices([reminder, original])
    assert result == [original]


def test_deduplicate_invoices_keeps_all_without_invoice_number(session):
    a = make_invoice(email_message_id="<a>", invoice_number="")
    b = make_invoice(email_message_id="<b>", invoice_number="")
    result = deduplicate_invoices([a, b])
    assert len(result) == 2


def test_is_duplicate_of_existing_true_for_a_later_reminder(session):
    original = make_invoice(email_message_id="<a>", received_at=datetime(2026, 9, 1))
    reminder = make_invoice(email_message_id="<b>", received_at=datetime(2026, 9, 15))
    session.add_all([original, reminder])
    session.commit()

    assert is_duplicate_of_existing(session, reminder) is True
    assert is_duplicate_of_existing(session, original) is False


def test_is_duplicate_of_existing_false_without_invoice_number(session):
    inv = make_invoice(invoice_number="")
    session.add(inv)
    session.commit()
    assert is_duplicate_of_existing(session, inv) is False


# -- get_payable_invoices --

def test_get_payable_invoices_deduplicates_and_sorts_by_due_date(session):
    later = make_invoice(email_message_id="<later>", due_date=date(2026, 10, 1))
    earlier = make_invoice(email_message_id="<earlier>", due_date=date(2026, 9, 20), invoice_number="F-2")
    reminder_of_later = make_invoice(
        email_message_id="<reminder>", due_date=date(2026, 10, 1),
        invoice_number="F-1", received_at=datetime(2026, 9, 20),
    )
    session.add_all([later, earlier, reminder_of_later])
    session.commit()

    result = get_payable_invoices(session)

    assert [inv.due_date for inv in result] == [date(2026, 9, 20), date(2026, 10, 1)]
    assert len(result) == 2  # later + reminder_of_later deduped to one (earliest received)


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
