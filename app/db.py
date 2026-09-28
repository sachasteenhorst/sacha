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
    ],
    "matches": [
        ("group_id", "VARCHAR DEFAULT ''"),
    ],
}


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


def init_db() -> None:
    from app import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _migrate_schema()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
