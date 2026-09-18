from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pi import db as dbm
from pi.config import Filters

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = dbm.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def filters() -> Filters:
    return Filters()


@pytest.fixture
def shopify_payload() -> dict:
    return json.loads((FIXTURES / "shopify_products.json").read_text(encoding="utf-8"))


def ts(days_ago: float = 0) -> str:
    """Timestamp N days in the past, in the format the database stores."""
    moment = datetime.now(UTC) - timedelta(days=days_ago)
    return moment.isoformat(timespec="seconds")


def make_history(
    points: list[tuple[float, float | None, float]], currency: str = "USD"
) -> list[sqlite3.Row]:
    """Build price history rows from (price, compare_at, days_ago) tuples.

    Returned oldest-first, the order pi.deals.evaluate expects. A point may carry
    its own currency as a fourth element, for the cases where a shop switched.
    """
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.execute(
        "CREATE TABLE p (variant_id INT, ts TEXT, price_usd REAL, compare_at_usd REAL,"
        " in_stock INT, currency TEXT, price_native REAL, compare_at_native REAL,"
        " fx_rate REAL)"
    )
    for point in points:
        price, compare, days_ago = point[:3]
        money = point[3] if len(point) > 3 else currency
        # A rate of 1.0, so the native and dollar columns coincide and a test can
        # keep talking in the one set of numbers it cares about.
        scratch.execute(
            "INSERT INTO p VALUES (1, ?, ?, ?, 1, ?, ?, ?, 1.0)",
            (ts(days_ago), price, compare, money, price, compare),
        )
    return scratch.execute("SELECT * FROM p ORDER BY ts").fetchall()


@pytest.fixture(autouse=True)
def _shopify_unstated_currency_keeps_the_record(request, monkeypatch):
    """Most tests stand in a shop that names no currency, and are about
    something else. Asking /meta.json again would be a request they never
    mocked, so they keep the recorded currency; tests of that very path mark
    themselves `asks_meta` and get the real thing."""
    if request.node.get_closest_marker("asks_meta"):
        return
    from pi.sources import shopify

    async def keep(client, base, limiter, recorded):
        return recorded

    monkeypatch.setattr(shopify, "currency_when_unstated", keep)
