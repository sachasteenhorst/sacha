"""Tests scripts/cleanup_ponto_duplicates.py's one-off remediation logic
against an isolated in-memory database -- no real script subprocess is
spawned; main() is called directly with argparse args monkeypatched via
sys.argv, exactly like the script itself parses them.
"""
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Match, MatchMethod, MatchStatus, Transaction


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _run_main(monkeypatch, session, session_local, *, apply: bool):
    import scripts.cleanup_ponto_duplicates as cleanup

    monkeypatch.setattr(cleanup, "SessionLocal", session_local)
    argv = ["cleanup_ponto_duplicates.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    cleanup.main()


def _make_session_local(session):
    # main() calls SessionLocal() itself and closes it -- give it a factory
    # that always hands back the SAME already-populated session, so the
    # test can inspect the exact objects it touched afterwards.
    return lambda: session


def make_csv_tx(**kwargs):
    defaults = dict(
        external_ref="csv:1",
        booking_date=date(2026, 7, 10),
        amount_cents=-3960,
        currency="EUR",
        description="Factuur F-1 Kruitbosch",
        counterparty_name="Kruitbosch B.V.",
        counterparty_iban="NL00RABO0123456789",
        own_account_iban="NL44RABO0199970777",
        source="csv",
        status=MatchStatus.MATCHED,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_ponto_tx(**kwargs):
    defaults = dict(
        external_ref="ponto:tx-1",
        booking_date=date(2026, 7, 11),  # a day off, like the real production pairs
        amount_cents=-3960,
        currency="EUR",
        description="KRUITBOSCH FACT F1",  # different wording, on purpose
        counterparty_name="Kruitbosch",
        counterparty_iban="NL00RABO0123456789",
        own_account_iban="NL44RABO0199970777",
        source="ponto",
        external_id="tx-1",
        status=MatchStatus.UNMATCHED,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def test_apply_merges_a_genuine_duplicate_pair(monkeypatch, session, capsys):
    csv_tx = make_csv_tx()
    ponto_tx = make_ponto_tx()
    session.add_all([csv_tx, ponto_tx])
    session.commit()
    ponto_id = ponto_tx.id
    csv_id = csv_tx.id

    session.add(Match(transaction_id=ponto_id, invoice_id=0, method=MatchMethod.MANUAL))
    session.commit()

    _run_main(monkeypatch, session, _make_session_local(session), apply=True)

    session.expire_all()
    remaining = session.query(Transaction).filter(Transaction.id == ponto_id).first()
    assert remaining is None
    kept = session.get(Transaction, csv_id)
    assert kept.external_id == "tx-1"
    assert session.query(Match).filter(Match.transaction_id == ponto_id).count() == 0


def test_dry_run_changes_nothing(monkeypatch, session):
    csv_tx = make_csv_tx()
    ponto_tx = make_ponto_tx()
    session.add_all([csv_tx, ponto_tx])
    session.commit()
    ponto_id = ponto_tx.id
    csv_id = csv_tx.id

    _run_main(monkeypatch, session, _make_session_local(session), apply=False)

    session.expire_all()
    assert session.get(Transaction, ponto_id) is not None
    assert session.get(Transaction, csv_id).external_id == ""


def test_ponto_row_without_a_twin_is_left_untouched(monkeypatch, session):
    ponto_tx = make_ponto_tx(
        external_ref="ponto:tx-2", external_id="tx-2",
        counterparty_iban="NL99RABO0000000001", counterparty_name="Onbekende Leverancier",
    )
    session.add(ponto_tx)
    session.commit()
    ponto_id = ponto_tx.id

    _run_main(monkeypatch, session, _make_session_local(session), apply=True)

    session.expire_all()
    assert session.get(Transaction, ponto_id) is not None
