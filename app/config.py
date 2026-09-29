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
    # "Mobility Services" (from mobility-services.bike) is the platform
    # behind Lease a Bike -- their invoices are verkoopfacturen this shop
    # issues to a leasing company (VWPFS) for a customer's lease bike, so
    # the IBAN in the document is this shop's OWN account: it's money
    # coming in, not a bill to pay.
    incoming_suppliers: str = "ENRA,HelloRider,Mobility Services"
    # A REFERENCE match (invoice number found in the transaction description)
    # doesn't have to line up exactly on amount -- some suppliers (Kruitbosch,
    # Accell) settle for slightly less than invoiced (betalingskorting /
    # early-payment discount). The accepted gap is capped at whichever of
    # these is SMALLER: a percentage of the invoice total, or a flat amount.
    payment_discount_percent: float = 3.0
    payment_discount_max_cents: int = 2500
    # Own company names (comma-separated, matched case-insensitively against
    # the sender's display name) -- a verkoopfactuur we ourselves sent to a
    # customer, that ends up in a mailbox we scan (e.g. archived/CC'd into
    # info@), is never a purchase to pay and must never count as an open
    # inkoopfactuur. Any sender on the same domain as a configured mailbox
    # (see graph_mailboxes) is always treated as "ourselves" too, with no
    # config needed.
    own_company_names: str = "Van der Linden Tweewielers,Hing B.V."
    # Suppliers/afzenders (kommagescheiden, hoofdletterongevoelig) wier
    # documenten NOOIT naar Basecone mogen, zelfs niet als extractie ze
    # herkent als een gewone uitgaande inkoopfactuur -- ze gaan al via een
    # ander kanaal de boekhouding in. Standaard: de Lease a Bike-"facturen"
    # (Mobility Services/VWPFS) en HelloRider-documenten zijn kopieën van
    # onze eigen kassaverkoop via CycleSoftware, die al automatisch via
    # Twinfield wordt geboekt -- nogmaals doorsturen zou een dubbele
    # boeking veroorzaken.
    basecone_exclude_suppliers: str = "Mobility Services,Lease a Bike,VWPFS,HelloRider,CycleSoftware"
    # Uitzondering op de incoming-documenten-worden-nooit-doorgestuurd-regel:
    # leveranciers (kommagescheiden, hoofdletterongevoelig) van wie een
    # INKOMEND document (zie INCOMING_SUPPLIERS) wél naar Basecone moet.
    # Standaard ENRA: hun rekening-courantoverzicht is, ook al is het geld
    # inkomend, wel degelijk een document dat de boekhouder moet zien.
    basecone_include_incoming: str = "ENRA"
    # Every inkoopfactuur must be forwarded here so it reaches Basecone/the
    # boekhouder -- a matched invoice that never got forwarded is still
    # effectively a vraagpost. Empty disables the whole Basecone-forward
    # check (dashboard just shows "onbekend" for everything).
    basecone_forward_address: str = "vdlg.161024@mailvanderlaangroep.nl"
    # Off by default -- flip to true only once you've added the Mail.Send /
    # Mail.Send.Shared permission in Entra and re-run scripts/graph_login.py
    # (see README). While off, nothing is ever sent automatically; invoices
    # just pile up in the dashboard's "Niet naar Basecone" list for you to
    # send by hand.
    auto_forward_basecone: bool = False
    # Only documents received on/after this date are ever auto-forwarded --
    # everything older is "backlog" (checked against Sent Items for a manual
    # forward you already did, otherwise left for you to send by hand from
    # the dashboard). Leave empty to have the app record "today" the first
    # time AUTO_FORWARD_BASECONE is turned on (see app/basecone_forward.py);
    # set explicitly (YYYY-MM-DD) to override that.
    auto_forward_from_date: str = ""
    # Safety cap: an auto-forward sync run never sends more than this many
    # documents, so a config mistake can't spam the accountant's inbox.
    auto_forward_max_per_sync: int = 50

    @property
    def graph_mailboxes(self) -> list[str]:
        return [m.strip() for m in self.graph_mailbox.split(",") if m.strip()]

    @property
    def incoming_supplier_names(self) -> list[str]:
        return [s.strip().lower() for s in self.incoming_suppliers.split(",") if s.strip()]

    @property
    def own_company_name_list(self) -> list[str]:
        return [s.strip().lower() for s in self.own_company_names.split(",") if s.strip()]

    @property
    def own_mail_domains(self) -> set[str]:
        return {m.split("@", 1)[1].lower() for m in self.graph_mailboxes if "@" in m}

    @property
    def basecone_exclude_supplier_names(self) -> list[str]:
        return [s.strip().lower() for s in self.basecone_exclude_suppliers.split(",") if s.strip()]

    @property
    def basecone_include_incoming_names(self) -> list[str]:
        return [s.strip().lower() for s in self.basecone_include_incoming.split(",") if s.strip()]

    # -- Sync scheduling --
    sync_interval_minutes: int = 60
    # How many days to look back on every sync (covers late-arriving mail/statements).
    sync_lookback_days: int = 120

    # -- Dashboard auth --
    dashboard_username: str = "sacha"
    dashboard_password: str = "changeme"


settings = Settings()
