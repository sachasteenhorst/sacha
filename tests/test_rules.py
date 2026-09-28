from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.matcher import run_matching
from app.models import MatchStatus, Rule, Transaction
from app.rules import _rule_matches, apply_rules


@pytest.fixture()
def session():
    # autoflush=False to match app.db.SessionLocal exactly -- the
    # apply_rules()+run_matching() regression below only reproduces the real
    # bug under that setting (see its docstring/comment).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def make_transaction(**kwargs):
    defaults = dict(
        external_ref="tx-1",
        booking_date=date(2024, 3, 10),
        amount_cents=-1234,
        description="Bankkosten",
        counterparty_name="Rabobank",
        counterparty_iban="NL00RABO0123456789",
        reference="",
        bank_code="db",
        raw_data={},
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_rule(**kwargs):
    defaults = dict(name="Test regel", action="no_invoice_needed")
    defaults.update(kwargs)
    return Rule(**defaults)


# -- _rule_matches: individual criteria --

def test_counterparty_contains_matches_case_insensitively():
    tx = make_transaction(counterparty_name="Stichting Pay.nl Clearing")
    rule = make_rule(counterparty_contains="pay.nl")
    assert _rule_matches(tx, rule) is True


def test_counterparty_contains_rejects_non_match():
    tx = make_transaction(counterparty_name="Belastingdienst")
    rule = make_rule(counterparty_contains="Rabobank")
    assert _rule_matches(tx, rule) is False


def test_iban_must_match_exactly():
    tx = make_transaction(counterparty_iban="NL44RABO0199970777")
    rule = make_rule(counterparty_iban="NL44RABO0199970777")
    assert _rule_matches(tx, rule) is True

    rule_wrong = make_rule(counterparty_iban="NL96RABO0112409008")
    assert _rule_matches(tx, rule_wrong) is False


def test_description_contains_matches():
    tx = make_transaction(description="Afschrijving Provisie augustus")
    rule = make_rule(description_contains="Provisie")
    assert _rule_matches(tx, rule) is True


def test_transaction_code_matches_case_insensitively():
    tx = make_transaction(bank_code="tb")
    rule = make_rule(transaction_code="TB")
    assert _rule_matches(tx, rule) is True

    rule_wrong = make_rule(transaction_code="ba")
    assert _rule_matches(tx, rule_wrong) is False


def test_direction_matches_by_amount_sign():
    incoming = make_transaction(amount_cents=5000)
    outgoing = make_transaction(amount_cents=-5000)
    rule_incoming = make_rule(direction="incoming")
    assert _rule_matches(incoming, rule_incoming) is True
    assert _rule_matches(outgoing, rule_incoming) is False


def test_all_set_criteria_must_match_and_combination():
    tx = make_transaction(counterparty_name="Rabobank", description="Kosten pakket", amount_cents=-500)
    rule = make_rule(counterparty_contains="Rabobank", description_contains="Kosten", direction="outgoing")
    assert _rule_matches(tx, rule) is True

    # One criterion no longer matches -> whole rule fails, even though
    # the others still do.
    rule_broken = make_rule(counterparty_contains="Rabobank", description_contains="Rente", direction="outgoing")
    assert _rule_matches(tx, rule_broken) is False


def test_blank_rule_never_matches_anything():
    tx = make_transaction()
    rule = make_rule()  # no criteria set at all
    assert _rule_matches(tx, rule) is False


# -- apply_rules --

def test_apply_rules_marks_matching_transaction_rule_handled(session):
    tx = make_transaction(counterparty_name="Belastingdienst")
    rule = make_rule(name="Belastingdienst", counterparty_contains="Belastingdienst")
    session.add_all([tx, rule])
    session.commit()

    result = apply_rules(session)
    session.commit()

    assert result.handled == 1
    assert tx.status == MatchStatus.RULE_HANDLED
    assert tx.applied_rule_id == rule.id


def test_apply_rules_ignores_disabled_rules(session):
    tx = make_transaction(counterparty_name="Belastingdienst")
    rule = make_rule(name="Belastingdienst", counterparty_contains="Belastingdienst", enabled=False)
    session.add_all([tx, rule])
    session.commit()

    result = apply_rules(session)
    session.commit()

    assert result.handled == 0
    assert tx.status == MatchStatus.UNMATCHED


def test_apply_rules_only_touches_unmatched_transactions(session):
    tx = make_transaction(counterparty_name="Belastingdienst", status=MatchStatus.MATCHED)
    rule = make_rule(name="Belastingdienst", counterparty_contains="Belastingdienst")
    session.add_all([tx, rule])
    session.commit()

    result = apply_rules(session)
    session.commit()

    assert result.handled == 0
    assert tx.status == MatchStatus.MATCHED
    assert tx.applied_rule_id is None


def test_apply_rules_leaves_non_matching_transactions_untouched(session):
    tx = make_transaction(counterparty_name="Huur pand BV")
    rule = make_rule(name="Belastingdienst", counterparty_contains="Belastingdienst")
    session.add_all([tx, rule])
    session.commit()

    result = apply_rules(session)
    session.commit()

    assert result.handled == 0
    assert tx.status == MatchStatus.UNMATCHED


# -- Regression: apply_rules() immediately followed by run_matching() in the
# same (autoflush=False) session, exactly like sync.py's import_bank_file()
# and run_sync() do. Without a flush in apply_rules(), run_matching()'s own
# status.in_([UNMATCHED, SUGGESTED]) query used to hit the DB before the
# pending RULE_HANDLED change reached it, pull the same (still-UNMATCHED in
# the DB) transaction back in from the identity map, find no invoice for it,
# and silently reset it to UNMATCHED again -- wiping out every rule just
# applied. Seen in production: 271 rule-handled transactions stayed
# UNMATCHED with applied_rule_id set.

def test_apply_rules_survives_run_matching_in_the_same_uncommitted_session(session):
    tx = make_transaction(counterparty_name="Belastingdienst")
    rule = make_rule(name="Belastingdienst", counterparty_contains="Belastingdienst")
    session.add_all([tx, rule])
    session.commit()

    apply_rules(session)
    run_matching(session)  # no commit in between, exactly like sync.py

    assert tx.status == MatchStatus.RULE_HANDLED
    assert tx.applied_rule_id == rule.id
