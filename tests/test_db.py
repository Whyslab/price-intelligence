"""Storage behaviour: idempotent upserts and change-only price history."""
from __future__ import annotations

import sqlite3

from pi import db as dbm

from .conftest import ts


def _variant(conn) -> int:
    store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
    product = dbm.upsert_product(conn, store, "p1", "Air Max", "https://shop.example/p1")
    return dbm.upsert_variant(conn, product, "v1", sku="SKU1", size="US 10", size_norm="US10")


def test_upserts_are_idempotent(conn):
    first = _variant(conn)
    second = _variant(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0] == 1


def test_a_later_fetch_without_an_image_keeps_the_one_we_had(conn):
    """Losing the picture would mean losing the photo in the notification."""
    store = dbm.upsert_store(conn, "shop.example")
    dbm.upsert_product(conn, store, "p1", "T", "https://u", image_url="https://cdn/img.jpg")
    dbm.upsert_product(conn, store, "p1", "T", "https://u", image_url=None)

    kept = conn.execute("SELECT image_url FROM products").fetchone()[0]
    assert kept == "https://cdn/img.jpg"


def test_unchanged_prices_are_not_recorded_again(conn):
    """A six-hourly sweep over a still catalogue must not grow the database."""
    variant = _variant(conn)
    assert dbm.record_price(conn, variant, 120.0, 220.0, True, "USD", 120.0, 1.0, ts=ts(2)) is True
    assert dbm.record_price(conn, variant, 120.0, 220.0, True, "USD", 120.0, 1.0, ts=ts(1)) is False
    assert conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 1


def test_every_kind_of_change_is_recorded(conn):
    variant = _variant(conn)
    dbm.record_price(conn, variant, 120.0, 220.0, True, "USD", 120.0, 1.0, ts=ts(4), compare_at_native=220.0)
    assert dbm.record_price(  # price
        conn, variant, 110.0, 220.0, True, "USD", 110.0, 1.0, ts=ts(3), compare_at_native=220.0
    )
    assert dbm.record_price(  # struck-through price
        conn, variant, 110.0, 200.0, True, "USD", 110.0, 1.0, ts=ts(2), compare_at_native=200.0
    )
    assert dbm.record_price(  # stock
        conn, variant, 110.0, 200.0, False, "USD", 110.0, 1.0, ts=ts(1), compare_at_native=200.0
    )
    assert conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 4


def test_a_moving_exchange_rate_is_not_a_price_change(conn):
    """157.50 SGD read on two days became $123.97 and $123.89, and the second was
    written down as news. That noise was most of the database."""
    variant = _variant(conn)
    assert dbm.record_price(conn, variant, 123.97, None, True, "SGD", 157.50, 1.2705, ts=ts(3))
    assert dbm.record_price(
        conn, variant, 123.89, None, True, "SGD", 157.50, 1.2713, ts=ts(0)
    ) is False
    assert conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 1


def test_a_shop_changing_currency_is_recorded(conn):
    """Same number, different money — that is a change, not a repeat."""
    variant = _variant(conn)
    assert dbm.record_price(conn, variant, 130.0, None, True, "USD", 130.0, 1.0, ts=ts(1))
    assert dbm.record_price(conn, variant, 151.0, None, True, "EUR", 130.0, 0.86, ts=ts(0))


def test_history_comes_back_oldest_first(conn):
    variant = _variant(conn)
    dbm.record_price(conn, variant, 200.0, None, True, "USD", 200.0, 1.0, ts=ts(10))
    dbm.record_price(conn, variant, 150.0, None, True, "USD", 150.0, 1.0, ts=ts(5))
    dbm.record_price(conn, variant, 120.0, None, True, "USD", 120.0, 1.0, ts=ts(0))

    prices = [row["price_usd"] for row in dbm.price_history(conn, variant)]
    assert prices == [200.0, 150.0, 120.0]
    assert dbm.latest_point(conn, variant)["price_usd"] == 120.0


def test_native_price_and_rate_are_kept_for_the_message(conn):
    """The notification quotes what the shop actually charges, not only USD."""
    variant = _variant(conn)
    dbm.record_price(conn, variant, 121.0, None, True, "EUR", 112.0, 0.857)
    point = dbm.latest_point(conn, variant)
    assert point["currency"] == "EUR"
    assert point["price_native"] == 112.0
    assert point["fx_rate"] == 0.857


def test_reopening_an_existing_database_is_a_no_op(tmp_path):
    path = tmp_path / "pi.db"
    first = dbm.connect(path)
    dbm.upsert_store(first, "shop.example", platform="shopify")
    first.close()

    second = dbm.connect(path)
    assert second.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
    assert second.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1
    second.close()


def test_a_newer_schema_is_refused_rather_than_corrupted(tmp_path):
    path = tmp_path / "pi.db"
    dbm.connect(path).close()
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 5}")
    raw.close()

    try:
        dbm.connect(path)
    except RuntimeError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("expected a refusal")


class TestPruning:
    """Keeping the database from growing without bound."""

    def test_old_history_goes_but_the_current_price_stays(self, conn):
        variant = _variant(conn)
        for days in (400, 300, 200, 100, 10):
            price = 100.0 + days
            dbm.record_price(conn, variant, price, None, True, "USD", price, 1.0, ts=ts(days))

        removed = dbm.prune_history(conn, keep_days=180)
        assert removed == 3

        kept = [r["price_usd"] for r in dbm.price_history(conn, variant)]
        assert kept == [200.0, 110.0]

    def test_a_variant_whose_every_point_is_old_keeps_its_latest(self, conn):
        """Otherwise a slow-moving product would look unpriced."""
        variant = _variant(conn)
        dbm.record_price(conn, variant, 100.0, None, True, "USD", 100.0, 1.0, ts=ts(900))
        dbm.record_price(conn, variant, 120.0, None, True, "USD", 120.0, 1.0, ts=ts(800))

        dbm.prune_history(conn, keep_days=180)
        remaining = dbm.price_history(conn, variant)
        assert len(remaining) == 1
        assert remaining[0]["price_usd"] == 120.0
        assert dbm.latest_point(conn, variant) is not None

    def test_delisted_products_are_dropped(self, conn):
        """A shop that stops returning a product leaves rows with no history."""
        live = _variant(conn)
        dbm.record_price(conn, live, 100.0, None, True, "USD", 100.0, 1.0)

        store = dbm.upsert_store(conn, "shop.example")
        gone_product = dbm.upsert_product(conn, store, "old", "Gone", "https://u")
        dbm.upsert_variant(conn, gone_product, "oldv")

        removed = dbm.drop_orphans(conn)
        assert removed["variants"] == 1
        assert removed["products"] == 1
        assert dbm.latest_point(conn, live) is not None
        assert conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0] == 1

    def test_pruning_an_empty_database_is_harmless(self, conn):
        assert dbm.prune_history(conn, keep_days=180) == 0
        assert dbm.drop_orphans(conn) == {"variants": 0, "products": 0}


class TestSweepOrder:
    """A run that cannot finish must still make progress across the whole list."""

    def test_never_collected_stores_come_first(self, conn):
        dbm.upsert_store(conn, "old.example", platform="shopify", last_ok=ts(1))
        dbm.upsert_store(conn, "fresh.example", platform="shopify", last_ok=ts(0))
        dbm.upsert_store(conn, "never.example", platform="shopify")

        order = [s["domain"] for s in dbm.get_stores(conn, platforms=("shopify",))]
        assert order == ["never.example", "old.example", "fresh.example"]

    def test_the_next_run_resumes_where_the_last_one_stopped(self, conn):
        """Alphabetical order meant the same first shops were collected every
        run while the tail of the list never was."""
        for name in "abcdef":
            dbm.upsert_store(conn, f"{name}.example", platform="shopify")

        first_half = dbm.get_stores(conn, platforms=("shopify",))[:3]
        for store in first_half:
            dbm.upsert_store(conn, store["domain"], last_ok=ts(0))

        nxt = [s["domain"] for s in dbm.get_stores(conn, platforms=("shopify",))][:3]
        assert set(nxt).isdisjoint({s["domain"] for s in first_half})


class TestMigrationCoverage:
    """If a column were missing from an old database, would migration restore it?

    Asked this way round on purpose. The obvious test — make a database, stamp
    an old version on it, migrate — proves nothing: the database was built from
    schema.sql and already has every column, so it passes whether or not the
    migration knows about them. This one takes columns *away* and checks they
    come back, which is the thing that actually happens to a database that has
    been in use since before they existed.

    Both bugs it guards against were live. A new index was created over a column
    that did not exist yet, because schema.sql is replayed before the ALTERs
    run. And a column added to schema.sql was never registered as one an
    existing database needs, so `runs.scope` never appeared in the live
    database while every test passed.
    """

    @staticmethod
    def _columns(conn, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _tables(conn) -> list[str]:
        return [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]

    def test_a_column_taken_away_is_put_back(self, tmp_path):
        reference = dbm.connect(tmp_path / "reference.db")
        wanted = {table: self._columns(reference, table) for table in self._tables(reference)}
        reference.close()

        checked = 0
        for table, columns in wanted.items():
            for column in columns:
                path = tmp_path / f"{table}_{column}.db"
                conn = dbm.connect(path)
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except sqlite3.OperationalError:
                    # Primary keys, indexed and UNIQUE columns cannot be dropped.
                    # They are also the original ones, so no migration adds them.
                    conn.close()
                    continue
                conn.execute("PRAGMA user_version = 1")
                conn.close()
                checked += 1

                migrated = dbm.connect(path)
                assert column in self._columns(migrated, table), (
                    f"migration did not restore {table}.{column} — a database in "
                    f"use since before it existed will not have it either"
                )
                migrated.close()

        assert checked > 0, "the test dropped nothing, so it proved nothing"

    def test_migrating_leaves_the_data_alone(self, tmp_path):
        conn = dbm.connect(tmp_path / "old.db")
        conn.execute("INSERT INTO stores (domain) VALUES ('shop.example')")
        conn.execute("PRAGMA user_version = 1")
        conn.close()

        migrated = dbm.connect(tmp_path / "old.db")
        assert migrated.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
