"""Rules that auto-resolve a bank transaction that will never have an
invoice or receipt (bank fees, transfers between your own accounts, card
terminal settlements that are just shop revenue, ...). Applied to every
still-open transaction before the matcher runs, and re-applied immediately
whenever a rule is created (see the /regels routes in app/main.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.matcher import _transaction_direction
from app.models import MatchStatus, Rule, Transaction


@dataclass
class RuleApplyResult:
    handled: int = 0


def _rule_matches(transaction: Transaction, rule: Rule) -> bool:
    criteria_set = False

    if rule.counterparty_contains:
        criteria_set = True
        if rule.counterparty_contains.lower() not in (transaction.counterparty_name or "").lower():
            return False
    if rule.counterparty_iban:
        criteria_set = True
        if rule.counterparty_iban.strip().upper() != (transaction.counterparty_iban or "").strip().upper():
            return False
    if rule.description_contains:
        criteria_set = True
        if rule.description_contains.lower() not in (transaction.description or "").lower():
            return False
    if rule.transaction_code:
        criteria_set = True
        if rule.transaction_code.lower() != (transaction.bank_code or "").lower():
            return False
    if rule.direction:
        criteria_set = True
        if rule.direction != _transaction_direction(transaction):
            return False

    # A rule with every criterion blank would match everything -- refuse it
    # rather than silently swallow all open transactions.
    return criteria_set


def apply_rules(session: Session) -> RuleApplyResult:
    result = RuleApplyResult()
    rules = list(session.scalars(select(Rule).where(Rule.enabled.is_(True))))
    if not rules:
        return result

    transactions = list(session.scalars(select(Transaction).where(Transaction.status == MatchStatus.UNMATCHED)))
    for transaction in transactions:
        for rule in rules:
            if _rule_matches(transaction, rule):
                transaction.status = MatchStatus.RULE_HANDLED
                transaction.applied_rule_id = rule.id
                result.handled += 1
                break
    return result
