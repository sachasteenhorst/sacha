"""Configuration loaded from environment variables (see .env.example)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # -- Database --
    database_url: str = "sqlite:///./data/admin.db"

    # -- Basecone API --
    # Basecone only has purchase/sales bookings and documents, not bank
    # statement lines -- it's not a usable source for the account's
    # transactions (bank statements come from an upload instead, see
    # app/bank_import.py). Kept optional/off by default so a sync never
    # reports a Basecone error when nobody configured it; flip this on only
    # if you get real API access for something Basecone-specific later.
    basecone_enabled: bool = False
    # NOTE: Basecone's exact API base URL / token URL / response field names
    # depend on your account and API product version. Verify these against
    # the API docs or support contact you get when registering for API
    # access, then adjust here (or via .env) rather than in code.
    basecone_token_url: str = "https://identity.basecone.com/connect/token"
    basecone_api_base_url: str = "https://api.basecone.com"
    basecone_client_id: str = ""
    basecone_client_secret: str = ""
    basecone_administration_id: str = ""
    # Path (relative to api_base_url) used to list bank statement lines.
    # Basecone historically exposes these under an "administrations" or
    # "bankstatements" resource; confirm the exact path for your account.
    basecone_bank_transactions_path: str = "/v1/administrations/{administration_id}/bankstatementlines"

    # -- Mailbox (Microsoft Graph API) for invoice PDFs --
    # Microsoft 365 disables classic IMAP username/password login by default,
    # so this reads mail through Graph instead: a public-client Azure AD app
    # registration using the delegated Mail.Read.Shared permission (device
    # code login, see scripts/graph_login.py). Public clients don't use a
    # client secret. See README for the exact setup steps.
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    # One address, or a comma-separated list (e.g.
    # "facturen@shop.nl,info@shop.nl") -- see graph_mailboxes below.
    graph_mailbox: str = ""
    graph_mail_folder: str = "inbox"
    # Only attachments with this extension are treated as invoices.
    invoice_attachment_extension: str = ".pdf"

    # -- Matching --
    match_amount_tolerance_cents: int = 1
    match_date_window_days: int = 60
    # Supplier names (matched case-insensitively, comma-separated) whose
    # documents represent money coming INTO the account rather than a bill
    # to pay -- e.g. ENRA settlement statements, HelloRider payouts. A bank
    # transaction only ever matches documents of the same direction
    # (positive amount = incoming).
    incoming_suppliers: str = "ENRA,HelloRider"

    @property
    def graph_mailboxes(self) -> list[str]:
        return [m.strip() for m in self.graph_mailbox.split(",") if m.strip()]

    @property
    def incoming_supplier_names(self) -> list[str]:
        return [s.strip().lower() for s in self.incoming_suppliers.split(",") if s.strip()]

    # -- Sync scheduling --
    sync_interval_minutes: int = 60
    # How many days to look back on every sync (covers late-arriving mail/statements).
    sync_lookback_days: int = 120

    # -- Dashboard auth --
    dashboard_username: str = "sacha"
    dashboard_password: str = "changeme"


settings = Settings()
