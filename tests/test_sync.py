"""Tests app/sync.py's orchestration -- specifically that a genuine invoice
match always wins over a Rule that would otherwise also claim the same
transaction (e.g. a "omzet"-rule made for a counterparty that also happens
to send real, matchable invoices, like a leasing platform)."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Invoice, MatchStatus, Rule, Transaction
from app.sync import import_bank_file

RABO_CSV_HEADER = (
    "IBAN/BBAN;Munt;BIC;Volgnr;Datum;Rentedatum;Bedrag;Saldo na trn;"
    "Tegenrekening IBAN/BBAN;Naam tegenpartij;Naam uiteindelijke partij;"
    "Naam initiërende partij;BIC tegenpartij;Code;Batch ID;Transactiereferentie;"
    "Machtigingskenmerk;Incassant ID;Betalingskenmerk;Omschrijving-1;"
    "Omschrijving-2;Omschrijving-3;Reden retour;Oorspr bedrag;Oorspr munt;Koers"
)


def _rabo_csv_row(**overrides) -> str:
    defaults = {
        "iban": "NL00RABO0123456789", "munt": "EUR", "bic": "RABONL2U", "volgnr": "1",
        "datum": "15-08-2026", "rentedatum": "15-08-2026", "bedrag": "1000,00",
        "saldo": "1000,00", "tegen_iban": "NL11ABCD0123456789", "naam": "VWPFS B.V.",
        "naam2": "", "naam3": "", "bic2": "", "code": "", "batch": "", "txref": "",
        "machtiging": "", "incassant": "", "betalingskenmerk": "",
        "oms1": "Factuur 22533", "oms2": "", "oms3": "", "retour": "",
        "oorspr_bedrag": "", "oorspr_munt": "", "koers": "",
    }
    defaults.update(overrides)
    return ";".join([
        defaults["iban"], defaults["munt"], defaults["bic"], defaults["volgnr"],
        defaults["datum"], defaults["rentedatum"], defaults["bedrag"], defaults["saldo"],
        defaults["tegen_iban"], defaults["naam"], defaults["naam2"], defaults["naam3"],
        defaults["bic2"], defaults["code"], defaults["batch"], defaults["txref"],
        defaults["machtiging"], defaults["incassant"], defaults["betalingskenmerk"],
        defaults["oms1"], defaults["oms2"], defaults["oms3"], defaults["retour"],
        defaults["oorspr_bedrag"], defaults["oorspr_munt"], defaults["koers"],
    ])


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()


def make_invoice(**kwargs):
    defaults = dict(
        email_message_id="<msg-1>",
        attachment_filename="factuur.pdf",
        email_subject="Factuur",
        email_from="support@mobility-services.bike",
        received_at=datetime(2026, 8, 1),
        invoice_number="22533",
        supplier_name="Mobility Services",
        amount_cents=100000,
        direction="incoming",
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def test_real_invoice_match_wins_over_a_conflicting_rule(session):
    # A rule someone made for this counterparty ("Omzet lease VWPFS") would
    # otherwise claim every VWPFS bijschrijving as generic omzet -- but this
    # one has a real, matchable invoice, so the match must win.
    rule = Rule(name="Omzet lease VWPFS", action="revenue", counterparty_contains="VWPFS", direction="incoming")
    invoice = make_invoice()
    session.add_all([rule, invoice])
    session.commit()

    content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row() + "\n").encode("utf-8-sig")
    result = import_bank_file(session, "export.csv", content)

    assert not result.errors
    tx = session.query(Transaction).one()
    assert tx.status == MatchStatus.MATCHED
    assert tx.applied_rule_id is None
    assert invoice.status == MatchStatus.MATCHED


def test_rule_still_catches_transactions_with_no_matching_invoice(session):
    # No invoice exists for this one -- the rule must still apply as a
    # fallback, exactly as before the reordering.
    rule = Rule(name="Omzet lease VWPFS", action="revenue", counterparty_contains="VWPFS", direction="incoming")
    session.add(rule)
    session.commit()

    content = (RABO_CSV_HEADER + "\n" + _rabo_csv_row(oms1="Geen factuurverwijzing hier") + "\n").encode("utf-8-sig")
    result = import_bank_file(session, "export.csv", content)

    assert not result.errors
    tx = session.query(Transaction).one()
    assert tx.status == MatchStatus.RULE_HANDLED
    assert tx.applied_rule_id == rule.id
