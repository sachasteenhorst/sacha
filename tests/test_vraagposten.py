import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BaseconeForwardStatus, DocumentKind, Invoice, Match, MatchMethod, MatchStatus, Transaction
from app.vraagposten import _extract_reference_suffixes, build_vraagposten


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
        external_ref="tx-1",
        booking_date=date(2026, 1, 1),
        amount_cents=-90000,
        description="",
        counterparty_name="Leverancier",
        counterparty_iban="",
        reference="",
        raw_data={},
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        email_subject="Factuur",
        email_from="verkoop@leverancier.nl",
        received_at=datetime(2026, 1, 1),
        invoice_number="F-1",
        supplier_name="Leverancier",
        amount_cents=12000,
        document_kind=DocumentKind.INVOICE.value,
        status=MatchStatus.UNMATCHED,
        in_basecone=BaseconeForwardStatus.UNKNOWN.value,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_extract_reference_suffixes_reassembles_split_group():
    # Real production description: "0723 3" is "07233" split by a stray
    # space (PDF line-wrap artifact).
    desc = (
        "ID ACCELL NL42375PMTINF42375TRANS2 2967 Descr. 01123 03005 05120 "
        "0723 3 09087 Kenmerk machtiging / incassant ID: 3458514 NL68ZZZ010542980000"
    )
    assert _extract_reference_suffixes(desc) == ["01123", "03005", "05120", "07233", "09087"]


def test_extract_reference_suffixes_empty_without_marker():
    assert _extract_reference_suffixes("Gewone omschrijving zonder incasso-referenties") == []


def test_incasso_with_missing_invoice_suggests_opvragen_with_specific_numbers(session):
    # Two invoices referenced, only one exists in the database -- the other
    # was apparently never received by e-mail.
    tx = make_transaction(
        description=(
            "ID ACCELL NL42375PMTINF42375TRANS2 2967 Descr. 01123 03005 "
            "Kenmerk machtiging / incassant ID: 3458514 NL68ZZZ010542980000"
        ),
        counterparty_name="ACCELL NEDERLAND",
        amount_cents=-50000,
    )
    known_invoice = make_invoice(invoice_number="251101123", amount_cents=50000, supplier_name="Accell")
    session.add_all([tx, known_invoice])
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "opvragen"
    assert "03005" in items[0].label
    assert "01123" not in items[0].label
    assert "03005" in items[0].mailto


def test_incasso_with_all_invoices_known_falls_through_to_normal_categorization(session):
    # All referenced invoices exist -- this isn't a "missing invoice" case,
    # so the usual controleren/regel/bon/opvragen logic decides instead
    # (here: same-supplier candidate with a different amount -- controleren).
    tx = make_transaction(
        description=(
            "ID ACCELL NL42375PMTINF42375TRANS2 2967 Descr. 01123 "
            "Kenmerk machtiging / incassant ID: 3458514 NL68ZZZ010542980000"
        ),
        counterparty_name="ACCELL NEDERLAND",
        amount_cents=-9999,
    )
    known_invoice = make_invoice(invoice_number="251101123", amount_cents=50000, supplier_name="Accell")
    session.add_all([tx, known_invoice])
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "controleren"


def test_unmatched_with_same_supplier_candidate_suggests_controleren(session):
    tx = make_transaction(counterparty_name="Kruitbosch", amount_cents=-9999)
    inv = make_invoice(supplier_name="Kruitbosch", amount_cents=12345)  # different amount -- not auto-matched
    session.add_all([tx, inv])
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "controleren"
    assert items[0].invoice.id == inv.id


def test_recurring_counterparty_suggests_regel(session):
    tx1 = make_transaction(external_ref="tx-1", counterparty_name="Huisbaas BV", booking_date=date(2026, 1, 1))
    tx2 = make_transaction(external_ref="tx-2", counterparty_name="Huisbaas BV", booking_date=date(2026, 2, 1))
    session.add_all([tx1, tx2])
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 2
    assert all(i.kind == "regel" for i in items)


def test_truncated_name_variants_are_recognized_as_one_recurring_supplier(session):
    # Real production case: Rabobank truncates the counterparty field
    # differently across transactions from the same supplier.
    tx1 = make_transaction(external_ref="tx-1", counterparty_name="Tenways Technovation Europe B.", booking_date=date(2026, 1, 1))
    tx2 = make_transaction(external_ref="tx-2", counterparty_name="Tenways Technovation Europe BV", booking_date=date(2026, 2, 1))
    tx3 = make_transaction(external_ref="tx-3", counterparty_name="Tenways Technovation Europe Ke...", booking_date=date(2026, 3, 1))
    session.add_all([tx1, tx2, tx3])
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 3
    assert all(i.kind == "regel" for i in items)


def test_small_one_off_amount_suggests_bon(session):
    tx = make_transaction(counterparty_name="Parkeergarage", amount_cents=-1500)
    session.add(tx)
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "bon"


def test_large_one_off_no_candidate_suggests_opvragen(session):
    tx = make_transaction(counterparty_name="Onbekende Leverancier", amount_cents=-90000)
    session.add(tx)
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "opvragen"
    assert items[0].mailto is not None
    assert "mailto:" in items[0].mailto


def test_matched_transaction_with_unforwarded_invoice_suggests_doorsturen(session):
    tx = make_transaction(status=MatchStatus.MATCHED)
    inv = make_invoice(status=MatchStatus.MATCHED, in_basecone=BaseconeForwardStatus.UNKNOWN.value)
    session.add_all([tx, inv])
    session.flush()
    session.add(Match(transaction_id=tx.id, invoice_id=inv.id, method=MatchMethod.REFERENCE, confirmed=True, group_id=uuid.uuid4().hex[:12]))
    session.commit()

    items = build_vraagposten(session)

    assert len(items) == 1
    assert items[0].kind == "doorsturen"
    assert items[0].invoice.id == inv.id


def test_matched_transaction_already_in_basecone_is_not_a_vraagpost(session):
    tx = make_transaction(status=MatchStatus.MATCHED)
    inv = make_invoice(status=MatchStatus.MATCHED, in_basecone=BaseconeForwardStatus.YES.value)
    session.add_all([tx, inv])
    session.flush()
    session.add(Match(transaction_id=tx.id, invoice_id=inv.id, method=MatchMethod.REFERENCE, confirmed=True, group_id=uuid.uuid4().hex[:12]))
    session.commit()

    items = build_vraagposten(session)

    assert items == []


def test_ignored_and_rule_handled_transactions_are_not_vraagposten(session):
    session.add(make_transaction(status=MatchStatus.IGNORED))
    session.add(make_transaction(external_ref="tx-2", status=MatchStatus.RULE_HANDLED))
    session.add(make_transaction(external_ref="tx-3", status=MatchStatus.RECEIPT_ELSEWHERE))
    session.commit()

    items = build_vraagposten(session)

    assert items == []


def test_sorted_oldest_first_then_largest_amount(session):
    old_small = make_transaction(external_ref="tx-1", booking_date=date(2026, 1, 1), amount_cents=-1000, counterparty_name="A")
    old_large = make_transaction(external_ref="tx-2", booking_date=date(2026, 1, 1), amount_cents=-90000, counterparty_name="B")
    recent = make_transaction(external_ref="tx-3", booking_date=date(2026, 6, 1), amount_cents=-90000, counterparty_name="C")
    session.add_all([old_small, old_large, recent])
    session.commit()

    items = build_vraagposten(session)

    assert [i.transaction.external_ref for i in items] == ["tx-2", "tx-1", "tx-3"]
