"""Unit tests for small helper functions in app/main.py used by the
redesigned dashboard: pagination, the time-of-day greeting, and the
month-closing checklist."""
from datetime import datetime

from starlette.requests import Request

from app.main import PAGE_SIZE, _greeting, _month_closing_progress, _paginate


def _fake_request(query_string: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/dashboard",
        "query_string": query_string.encode(),
        "headers": [],
    }
    return Request(scope)


def test_paginate_slices_to_page_size():
    items = list(range(230))
    page, pagination = _paginate(items, "tx_page", _fake_request())
    assert page == items[:PAGE_SIZE]
    assert pagination["page"] == 1
    assert pagination["total"] == 230
    assert pagination["has_prev"] is False
    assert pagination["has_next"] is True


def test_paginate_respects_page_query_param():
    items = list(range(230))
    page, pagination = _paginate(items, "tx_page", _fake_request("tx_page=2"))
    assert page == items[PAGE_SIZE:PAGE_SIZE * 2]
    assert pagination["page"] == 2
    assert pagination["has_prev"] is True


def test_paginate_clamps_out_of_range_page_to_last_page():
    items = list(range(10))
    page, pagination = _paginate(items, "tx_page", _fake_request("tx_page=999"))
    assert pagination["page"] == 1  # only one page's worth of data
    assert pagination["has_next"] is False
    assert page == items


def test_paginate_ignores_garbage_page_param():
    items = list(range(10))
    page, pagination = _paginate(items, "tx_page", _fake_request("tx_page=not-a-number"))
    assert pagination["page"] == 1
    assert page == items


def test_paginate_different_params_are_independent():
    # betalingen-zonder-factuur and facturen-niet-betaald paginate
    # separately -- flipping one page must never affect the other's.
    items = list(range(10))
    _, tx_pagination = _paginate(items, "tx_page", _fake_request("inv_page=3"))
    assert tx_pagination["page"] == 1


def test_greeting_morning():
    assert _greeting(datetime(2026, 10, 13, 8, 0)) == "Goedemorgen"


def test_greeting_afternoon():
    assert _greeting(datetime(2026, 10, 13, 14, 0)) == "Goedemiddag"


def test_greeting_evening():
    assert _greeting(datetime(2026, 10, 13, 20, 0)) == "Goedenavond"


def test_month_closing_progress_nothing_done_yet():
    steps = _month_closing_progress([], datetime(2026, 10, 13), niet_naar_basecone_count=5)
    assert steps[0]["label"] == "Afschrift binnen"
    assert steps[0]["done"] is False
    assert steps[0]["current"] is True
    assert all(not s["done"] for s in steps)


def test_month_closing_progress_all_steps_done():
    monthly_progress = [{"_key": "2026-10", "total": 10, "resolved": 10, "pct": 100}]
    steps = _month_closing_progress(monthly_progress, datetime(2026, 10, 13), niet_naar_basecone_count=0)
    assert all(s["done"] for s in steps)
    assert steps[-1]["current"] is True


def test_month_closing_progress_partial():
    # Statement in, but not every transaction linked yet -- "Klaar voor
    # boekhouder" can never be done before that, whatever Basecone says.
    monthly_progress = [{"_key": "2026-10", "total": 10, "resolved": 4, "pct": 40}]
    steps = _month_closing_progress(monthly_progress, datetime(2026, 10, 13), niet_naar_basecone_count=0)
    assert steps[0]["done"] is True  # afschrift binnen
    assert steps[1]["done"] is False  # transacties gekoppeld
    assert steps[1]["current"] is True
    assert steps[3]["done"] is False  # klaar voor boekhouder
