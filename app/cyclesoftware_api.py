"""Skeleton for a future live CycleSoftware (kassasysteem) integration --
"CS Connect" or whatever CycleSoftware's own API/export product is called.

NOT implemented yet: CycleSoftware's API format (auth scheme, endpoint
paths, the shape of a verkoopfactuur resource) is not publicly documented
the way Ponto's or Microsoft Graph's is, and guessing at it would produce
code that looks plausible but is wrong in specific, hard-to-spot ways --
worse than an honest gap. Until Sacha has real API docs/credentials from
CycleSoftware, sales invoices come in via the manual CSV/XLSX upload on the
dashboard's upload page instead (see app/cyclesoftware_upload.py), which
covers the same need (matching a customer/HelloRider/Lease a Bike/VWPFS
bijschrijving to the actual verkoopfactuur it settles) without depending on
API access that doesn't exist yet.

Once real API details are available, this module should expose the same
shape app/email_client.py and app/bank_ponto.py already use elsewhere in
this codebase, so app/sync.py can call it the same way:

    def cs_configured() -> bool:
        return bool(settings.cs_api_key and settings.cs_api_base_url)

    def fetch_sales_invoices(since: date) -> list[SalesInvoiceRow]:
        '''Calls CycleSoftware's API for verkoopfacturen created/changed
        since `since`, returning the same SalesInvoiceRow shape
        app/cyclesoftware_upload.py already builds from a CSV/XLSX upload,
        so app/sync.py (or a dedicated _sync_cyclesoftware step) can reuse
        the exact same "turn a row into an Invoice(document_kind=
        sales_invoice, direction=incoming, ...)" logic either way.'''
        raise NotImplementedError(
            "CS Connect API-formaat nog niet bekend -- zie module-docstring. "
            "Gebruik voorlopig de handmatige upload op de dashboardpagina."
        )

TODO once CycleSoftware API access exists:
    - Auth scheme (API key header? OAuth2?) -- CS_API_KEY is reserved in
      app/config.py for this, but the exact header/scheme is unknown.
    - Base URL / endpoint path for listing verkoopfacturen -- CS_API_BASE_URL
      is reserved for this.
    - Pagination shape, rate limits, and which fields map to
      invoice_number/invoice_date/customer_name/amount_cents/
      outstanding_cents (see app/cyclesoftware_upload.py's SalesInvoiceRow
      for the target shape).
    - Whether the API can filter "since a given date/id" server-side, or
      whether this module needs to fetch everything and dedupe client-side
      the way app/cyclesoftware_upload.py already does via
      (email_message_id, attachment_filename).
"""
from __future__ import annotations

from app.config import settings


def cs_configured() -> bool:
    """Always False today -- see module docstring. Kept as a real function
    (not just a settings check inline elsewhere) so call sites don't need to
    change once this is actually implemented."""
    return False and bool(settings.cs_api_key and settings.cs_api_base_url)
