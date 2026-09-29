"""SQLAlchemy engine/session setup."""
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# Ensure the sqlite data directory exists before the engine tries to open it.
if settings.database_url.startswith("sqlite:///"):
    db_path = settings.database_url.replace("sqlite:///", "", 1)
    if db_path not in (":memory:",):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# Columns added to existing tables after the first release. create_all()
# only creates missing TABLES -- it never alters one that already exists,
# so an upgrade of a live database (with real invoices already in it)
# needs these added by hand. Safe to run every startup: each column is
# only added if it isn't there yet.
_COLUMN_MIGRATIONS = {
    "invoices": [
        ("direction", "VARCHAR DEFAULT 'outgoing'"),
        ("document_kind", "VARCHAR DEFAULT 'invoice'"),
        ("referenced_invoice_numbers", "TEXT DEFAULT '[]'"),
        ("content_hash", "VARCHAR DEFAULT ''"),
        ("graph_mailbox", "VARCHAR DEFAULT ''"),
        ("graph_message_id", "VARCHAR DEFAULT ''"),
        ("in_basecone", "VARCHAR DEFAULT 'unknown'"),
        ("basecone_forwarded_at", "DATETIME"),
        ("basecone_forward_method", "VARCHAR DEFAULT ''"),
        ("basecone_forwarded_to", "VARCHAR DEFAULT ''"),
        ("manually_ignored", "BOOLEAN DEFAULT 0"),
        ("due_date", "DATE"),
        ("due_date_estimated", "BOOLEAN DEFAULT 0"),
        ("payment_method", "VARCHAR DEFAULT 'onbekend'"),
        ("paid_at", "DATETIME"),
        ("notified_new_invoice", "BOOLEAN DEFAULT 0"),
    ],
    "matches": [
        ("group_id", "VARCHAR DEFAULT ''"),
        ("discount_cents", "INTEGER DEFAULT 0"),
    ],
    "transactions": [
        ("bank_code", "VARCHAR DEFAULT ''"),
        ("applied_rule_id", "INTEGER"),
    ],
}

# Applied once, only if the rules table is still empty -- so deleting a
# seed rule you don't want sticks, instead of it reappearing on restart.
_DEFAULT_RULES = [
    dict(name="Rabo Smart Pay omzet", action="revenue", counterparty_contains="Rabo Smart Pay", direction="incoming"),
    dict(name="Rabobank Smart Pay omzet", action="revenue", counterparty_contains="Rabobank Smart Pay", direction="incoming"),
    dict(name="Stichting Pay.nl clearing", action="revenue", counterparty_contains="Stichting Pay.nl", description_contains="Clearing", direction="incoming"),
    dict(name="Rabobank kosten", action="no_invoice_needed", counterparty_contains="Rabobank", description_contains="Kosten"),
    dict(name="Rabobank provisie", action="no_invoice_needed", counterparty_contains="Rabobank", description_contains="Provisie"),
    dict(name="Rabobank rente", action="no_invoice_needed", counterparty_contains="Rabobank", description_contains="Rente"),
    dict(name="Overboeking eigen rekening", action="no_invoice_needed", transaction_code="tb"),
    dict(name="Belastingdienst", action="no_invoice_needed", counterparty_contains="Belastingdienst"),
]


def _seed_default_rules() -> None:
    from app.models import Rule

    with SessionLocal() as session:
        if session.query(Rule).count() > 0:
            return
        for defaults in _DEFAULT_RULES:
            session.add(Rule(**defaults))
        session.commit()


def _backfill_manually_ignored() -> None:
    """The manually_ignored column is brand new -- for invoices that were
    already IGNORED before it existed, infer which ones were a deliberate
    manual choice: the ONLY code path that ever sets IGNORED on a document
    NOT classified as "other" is the dashboard's manual "Negeren" button
    (see app/main.py's ignore_invoice; app/sync.py and
    scripts/reparse_invoices.py only ever auto-set IGNORED when
    document_kind IS "other"). So an already-ignored invoice whose
    document_kind isn't "other" must have been ignored by hand -- protect
    it. One where document_kind IS "other" was almost certainly
    auto-ignored, so it's left free for reparse's reclassify-and-reopen
    logic to reconsider. Runs exactly once, right when the column is added
    (see _migrate_schema) -- never again, so it can't later overwrite a
    real manual choice made after this ran."""
    from app.models import DocumentKind, Invoice, MatchStatus

    with SessionLocal() as session:
        rows = session.query(Invoice).filter(Invoice.status == MatchStatus.IGNORED).all()
        for row in rows:
            row.manually_ignored = row.document_kind != DocumentKind.OTHER.value
        if rows:
            session.commit()


def _migrate_schema() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    invoices_had_manually_ignored = True
    if "invoices" in existing_tables:
        invoices_had_manually_ignored = "manually_ignored" in {c["name"] for c in inspector.get_columns("invoices")}

    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            if table not in existing_tables:
                continue  # a fresh install's create_all() already has everything
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in columns:
                if name not in existing_columns:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))

    if "invoices" in existing_tables and not invoices_had_manually_ignored:
        _backfill_manually_ignored()


NOTIFIED_NEW_INVOICE_BACKFILL_MARKER = "./data/notified_new_invoice_backfilled.marker"


def _backfill_notified_new_invoice() -> None:
    """notified_new_invoice shipped with a plain "ADD COLUMN ... DEFAULT 0"
    migration and no backfill -- on a database that already had invoices,
    every one of them (1253 in production) came back as "unnotified",
    which would have fired a push per invoice on the very next sync. Unlike
    manually_ignored's backfill, this can't key off "column didn't exist
    yet" (it already shipped once without this fix, so the column now
    exists everywhere) -- a marker file instead records whether this
    specific backfill has run, so it still applies exactly once on an
    already-upgraded database. Every invoice that exists the moment this
    finally runs predates the push-notification feature entirely, so all of
    them are marked notified; only a genuinely new invoice fetched AFTER
    this point starts at False."""
    import os

    from app.models import Invoice

    if os.path.exists(NOTIFIED_NEW_INVOICE_BACKFILL_MARKER):
        return
    with SessionLocal() as session:
        session.query(Invoice).filter(Invoice.notified_new_invoice.is_(False)).update(
            {Invoice.notified_new_invoice: True}
        )
        session.commit()
    os.makedirs(os.path.dirname(NOTIFIED_NEW_INVOICE_BACKFILL_MARKER), exist_ok=True)
    with open(NOTIFIED_NEW_INVOICE_BACKFILL_MARKER, "w") as fh:
        fh.write("done")


def _backfill_bank_code_from_raw_data() -> None:
    """Transactions uploaded before the bank_code column existed have it
    empty even though the Rabobank CSV's "Code" column is sitting right
    there in raw_data -- fill it in once, idempotently (only rows still
    empty are touched, so this is cheap on every startup once it's done)."""
    from app.models import Transaction

    with SessionLocal() as session:
        rows = session.query(Transaction).filter(Transaction.bank_code == "").all()
        updated = 0
        for row in rows:
            code = (row.raw_data or {}).get("Code") or (row.raw_data or {}).get("code")
            if code and code.strip():
                row.bank_code = code.strip()
                updated += 1
        if updated:
            session.commit()


def init_db() -> None:
    from app import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    _seed_default_rules()
    _backfill_bank_code_from_raw_data()
    _backfill_notified_new_invoice()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
