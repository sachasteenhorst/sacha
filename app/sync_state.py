"""In-memory record of the most recent sync run, for the dashboard status
bar. Deliberately not persisted -- it only needs to answer "how did the
last run go", and resets (harmlessly) on a restart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class LastSyncInfo:
    ran_at: datetime | None = None
    new_transactions: int = 0
    new_invoices: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def basecone_error(self) -> str | None:
        return next((e for e in self.errors if e.startswith("Basecone:")), None)

    @property
    def email_error(self) -> str | None:
        return next((e for e in self.errors if e.startswith("E-mail:")), None)


_last_sync = LastSyncInfo()


def record(result) -> None:
    global _last_sync
    _last_sync = LastSyncInfo(
        ran_at=datetime.now(timezone.utc),
        new_transactions=result.new_transactions,
        new_invoices=result.new_invoices,
        errors=list(result.errors),
    )


def get() -> LastSyncInfo:
    return _last_sync
