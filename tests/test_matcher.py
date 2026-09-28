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
        external_ref="tx-1",
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


def test_reference_match_groups_multiple_invoice_numbers_in_one_description(session):
    # Kruitbosch/Accell-style incasso: one payment settles several invoices
    # at once, all listed (comma-separated) in the same description.
    tx = make_transaction(
        description="Incasso factuur VFNL002275878,VFNL002277060", amount_cents=-(12345 + 6000),
    )
    inv1 = make_invoice(invoice_number="VFNL002275878", amount_cents=12345, email_message_id="<msg-1>")
    inv2 = make_invoice(invoice_number="VFNL002277060", amount_cents=6000, email_message_id="<msg-2>")
    session.add_all([tx, inv1, inv2])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert tx.status == MatchStatus.MATCHED
    assert inv1.status == MatchStatus.MATCHED
    assert inv2.status == MatchStatus.MATCHED
    assert {m.invoice_id for m in tx.matches} == {inv1.id, inv2.id}
    assert tx.matches[0].group_id == tx.matches[1].group_id


def test_reference_match_accepts_small_payment_discount(session):
    # Real Kruitbosch example: invoiced 590,10, paid 587,66 -- a 2,44
    # betalingskorting, well within the default 3%/EUR25 tolerance.
    tx = make_transaction(description="Incasso factuur VFNL002280953", amount_cents=-58766)
    inv = make_invoice(invoice_number="VFNL002280953", amount_cents=59010)
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert tx.status == MatchStatus.MATCHED
    assert tx.matches[0].discount_cents == 244


def test_reference_match_rejects_difference_beyond_discount_tolerance(session):
    tx = make_transaction(description="Incasso factuur VFNL002280953", amount_cents=-40000)
    inv = make_invoice(invoice_number="VFNL002280953", amount_cents=59010)
    session.add_all([tx, inv])
    session.commit()

    run_matching(session)
    session.commit()

    # Too far apart to be a discount -- must not auto-match.
    assert tx.status == MatchStatus.UNMATCHED
    assert inv.status == MatchStatus.UNMATCHED


def test_reference_match_ignores_unrelated_invoices_with_coincidental_short_number_substrings(session):
    # Exact production bug: an incasso description containing the incassant
    # ID/date text "306228C" and "21-08-2026" also happens to contain "28"
    # and "08" as literal substrings. Two entirely unrelated Mbps invoices
    # were numbered "28" and "08", so a naive "in haystack" substring check
    # pulled them into the match too, breaking the amount total (and so
    # matching nothing at all).
    tx = make_transaction(
        description="Kenmerk machtiging / incassant ID: 306228C NL75ZZZ050142120000 VFNL002280953",
        counterparty_name="Kruitbosch",
        booking_date=date(2026, 8, 21),
        amount_cents=-58766,
    )
    kruitbosch_inv = make_invoice(invoice_number="VFNL002280953", amount_cents=59010, supplier_name="Kruitbosch")
    mbps_inv1 = make_invoice(invoice_number="28", amount_cents=5885, supplier_name="Mbps", email_message_id="<msg-mbps-1>")
    mbps_inv2 = make_invoice(invoice_number="08", amount_cents=2771, supplier_name="Mbps", email_message_id="<msg-mbps-2>")
    session.add_all([tx, kruitbosch_inv, mbps_inv1, mbps_inv2])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert tx.status == MatchStatus.MATCHED
    assert {m.invoice_id for m in tx.matches} == {kruitbosch_inv.id}
    assert tx.matches[0].discount_cents == 244
    assert mbps_inv1.status == MatchStatus.UNMATCHED
    assert mbps_inv2.status == MatchStatus.UNMATCHED


def test_reference_match_falls_back_to_all_suppliers_when_same_supplier_total_is_wrong(session):
    # Two invoice numbers turn up in the text; only the pair together (one
    # from a different supplier) adds up to the transaction amount, so the
    # same-supplier-only attempt must fail over to the full set.
    tx = make_transaction(
        description="Betaling ref ABCD1234 en WXYZ9999",
        counterparty_name="Kruitbosch",
        amount_cents=-(10000 + 500),
    )
    same_supplier_inv = make_invoice(invoice_number="ABCD1234", amount_cents=10000, supplier_name="Kruitbosch", email_message_id="<msg-a>")
    other_supplier_inv = make_invoice(invoice_number="WXYZ9999", amount_cents=500, supplier_name="Bpost", email_message_id="<msg-b>")
    session.add_all([tx, same_supplier_inv, other_supplier_inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert {m.invoice_id for m in tx.matches} == {same_supplier_inv.id, other_supplier_inv.id}


def test_reference_match_two_comma_separated_vfnl_numbers_with_unrelated_short_number_nearby(session):
    # Combines the multi-invoice-in-one-incasso case with a coincidental
    # short-number substring elsewhere in the same description.
    tx = make_transaction(
        description="Incasso factuur VFNL002275878,VFNL002277060 dd 12-2026",
        counterparty_name="Kruitbosch",
        amount_cents=-(12345 + 6000),
    )
    inv1 = make_invoice(invoice_number="VFNL002275878", amount_cents=12345, supplier_name="Kruitbosch", email_message_id="<msg-1>")
    inv2 = make_invoice(invoice_number="VFNL002277060", amount_cents=6000, supplier_name="Kruitbosch", email_message_id="<msg-2>")
    unrelated_short = make_invoice(invoice_number="12", amount_cents=999999, supplier_name="Onbekend", email_message_id="<msg-3>")
    session.add_all([tx, inv1, inv2, unrelated_short])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.auto_matched == 1
    assert {m.invoice_id for m in tx.matches} == {inv1.id, inv2.id}
    assert unrelated_short.status == MatchStatus.UNMATCHED


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


def test_matches_get_a_shared_group_id(session):
    tx = make_transaction(description="Betaling factuur F-2024-001")
    inv = make_invoice()
    session.add_all([tx, inv])
    session.commit()

    run_matching(session)
    session.commit()

    assert tx.matches[0].group_id  # non-empty


def test_opposite_direction_never_matches_on_amount_alone(session):
    # Same |amount| and date, but the transaction is money coming IN while
    # the invoice is an outgoing (normal) supplier bill -- must not match.
    tx = make_transaction(amount_cents=12345, booking_date=date(2024, 3, 15))
    inv = make_invoice(invoice_number="", invoice_date=date(2024, 3, 1), direction="outgoing")
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.suggested == 0
    assert tx.status == MatchStatus.UNMATCHED
    assert inv.status == MatchStatus.UNMATCHED


def test_incoming_transaction_matches_incoming_invoice(session):
    tx = make_transaction(amount_cents=25000, booking_date=date(2024, 3, 15), counterparty_name="ENRA")
    inv = make_invoice(
        invoice_number="", invoice_date=date(2024, 3, 1), amount_cents=25000,
        direction="incoming", supplier_name="ENRA",
    )
    session.add_all([tx, inv])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.suggested == 1
    assert tx.status == MatchStatus.SUGGESTED


def test_specification_match_also_links_referenced_invoices(session):
    tx = make_transaction(amount_cents=-12500, description="Incasso specificatie 999", counterparty_name="Accell")
    spec = make_invoice(
        invoice_number="", supplier_name="Accell", amount_cents=12500,
        document_kind="specification", referenced_invoice_numbers=["F-100", "F-200"],
        attachment_filename="specificatie.pdf",
    )
    inv_a = make_invoice(invoice_number="F-100", supplier_name="Accell", amount_cents=5000, attachment_filename="f100.pdf", email_message_id="<m2>")
    inv_b = make_invoice(invoice_number="F-200", supplier_name="Accell", amount_cents=7500, attachment_filename="f200.pdf", email_message_id="<m3>")
    session.add_all([tx, spec, inv_a, inv_b])
    session.commit()

    run_matching(session)
    session.commit()

    assert spec.status == MatchStatus.SUGGESTED
    assert inv_a.status == MatchStatus.SUGGESTED
    assert inv_b.status == MatchStatus.SUGGESTED
    group_ids = {m.group_id for m in tx.matches}
    assert len(group_ids) == 1  # all three share one match group
    assert len(tx.matches) == 3


def test_combination_match_finds_exact_sum(session):
    tx = make_transaction(amount_cents=-12500, description="Verzamelbetaling", counterparty_name="Kruitbosch")
    inv_a = make_invoice(
        invoice_number="F-A", supplier_name="Kruitbosch", amount_cents=5000,
        invoice_date=date(2024, 3, 5), attachment_filename="a.pdf", email_message_id="<a>",
    )
    inv_b = make_invoice(
        invoice_number="F-B", supplier_name="Kruitbosch", amount_cents=7500,
        invoice_date=date(2024, 3, 8), attachment_filename="b.pdf", email_message_id="<b>",
    )
    # A decoy that alone doesn't sum correctly with either -- shouldn't be pulled in.
    inv_c = make_invoice(
        invoice_number="F-C", supplier_name="Kruitbosch", amount_cents=999,
        invoice_date=date(2024, 3, 6), attachment_filename="c.pdf", email_message_id="<c>",
    )
    session.add_all([tx, inv_a, inv_b, inv_c])
    session.commit()

    summary = run_matching(session)
    session.commit()

    assert summary.suggested == 1
    assert tx.status == MatchStatus.SUGGESTED
    assert inv_a.status == MatchStatus.SUGGESTED
    assert inv_b.status == MatchStatus.SUGGESTED
    assert inv_c.status == MatchStatus.UNMATCHED
    assert len(tx.matches) == 2
