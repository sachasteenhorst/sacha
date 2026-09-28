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


def _migrate_schema() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            if table not in existing_tables:
                continue  # a fresh install's create_all() already has everything
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in columns:
                if name not in existing_columns:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


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


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
