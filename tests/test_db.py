"""Storage behaviour: idempotent upserts and change-only price history."""
from __future__ import annotations

import sqlite3

from pi import db as dbm
from pi import deals as dealm

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


class TestGivingBackWhatAShortPageWithdrew:
    """Version 16 undoes the marks a short Shopify page made.

    A pass writes one timestamp for everything it withdraws, so a batch of
    products sharing a shop and a moment is a pass's verdict — the kind the
    short page got wrong 110,796 times. A lone mark is a product whose own page
    answered 404, which was checked and stays.
    """

    @staticmethod
    def _product(conn, store_id, name, missing_since=None):
        product_id = dbm.upsert_product(conn, store_id, name, name, f"https://x/{name}")
        conn.execute(
            "UPDATE products SET missing_since = ? WHERE id = ?", (missing_since, product_id)
        )
        return product_id

    def test_a_batch_is_given_back_and_a_single_check_is_not(self, tmp_path):
        path = tmp_path / "old.db"
        conn = dbm.connect(path)
        shopify = dbm.upsert_store(conn, "shop.example", platform="shopify")
        crawled = dbm.upsert_store(conn, "crawl.example", platform="jsonld")
        batch = [self._product(conn, shopify, f"b{n}", "2026-09-22T18:20:50+00:00") for n in range(3)]
        single = self._product(conn, shopify, "one", "2026-09-22T19:00:00+00:00")
        crawled_batch = [
            self._product(conn, crawled, f"c{n}", "2026-09-21T10:00:00+00:00") for n in range(2)
        ]
        on_sale = self._product(conn, shopify, "fine")
        conn.execute("PRAGMA user_version = 15")
        conn.close()

        migrated = dbm.connect(path)

        def missing(product_id):
            return migrated.execute(
                "SELECT missing_since FROM products WHERE id = ?", (product_id,)
            ).fetchone()[0]

        assert all(missing(p) is None for p in batch), "the short page's verdict is undone"
        assert missing(single), "a product whose own page said 404 stays withdrawn"
        assert all(missing(p) for p in crawled_batch), "only Shopify passes had the fault"
        assert missing(on_sale) is None
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 16

    def test_stored_addresses_are_repaired_once(self, tmp_path):
        path = tmp_path / "old.db"
        conn = dbm.connect(path)
        store = dbm.upsert_store(conn, "www.43einhalb.com", platform="jsonld")
        relative = dbm.upsert_product(conn, store, "a", "Laces", "/es/p/laces-1")
        pictured = dbm.upsert_product(
            conn, store, "b", "Skirt", "https://www.43einhalb.com/p/b",
            image_url="https://img.ssensemedia.com/images/__IMAGE_PARAMS__/1_1/skirt.jpg",
        )
        conn.execute("PRAGMA user_version = 15")
        conn.close()

        migrated = dbm.connect(path)

        def column(name, product_id):
            return migrated.execute(
                f"SELECT {name} FROM products WHERE id = ?", (product_id,)
            ).fetchone()[0]

        assert column("url", relative) == "https://www.43einhalb.com/es/p/laces-1"
        assert column("image_url", pictured) == (
            "https://img.ssensemedia.com/images/b_white,g_center,f_auto,q_auto:best/1_1/skirt.jpg"
        )

    def test_a_database_already_at_16_is_not_touched_again(self, tmp_path):
        """Batch marks made after the upgrade are the fixed walk's verdict."""
        path = tmp_path / "new.db"
        conn = dbm.connect(path)
        shopify = dbm.upsert_store(conn, "shop.example", platform="shopify")
        batch = [self._product(conn, shopify, f"b{n}", "2026-09-30T10:00:00+00:00")
                 for n in range(3)]
        conn.close()

        again = dbm.connect(path)

        assert all(
            again.execute("SELECT missing_since FROM products WHERE id = ?", (p,)).fetchone()[0]
            for p in batch
        )


def _a_card(conn, store_id, name, checked, tried=None, score=50, source="market"):
    """One product with one offer on the shelf, stamped as given."""
    product = dbm.upsert_product(
        conn, store_id, name, name, f"https://shop.example/products/{name}"
    )
    variant = dbm.upsert_variant(conn, product, name)
    conn.execute(
        """
        INSERT INTO offers (variant_id, product_id, found_at, checked_at, price_usd,
                            reference_usd, reference_source, discount_pct, saving_usd,
                            score, all_time_low, tried_at)
        VALUES (?, ?, ?, ?, 100, 200, ?, 50, 100, ?, 0, ?)
        """,
        (variant, product, checked, checked, source, score, tried),
    )
    return product


class TestTheCheckingQueue:
    """Which cards `pi verify` opens next.

    Ordered by the last answer alone, a card whose page never loads stayed the
    oldest thing on the shelf for good: 34 kickz.com cards took 45 of every 60
    checks for three weeks.
    """

    def test_a_card_that_did_not_answer_goes_to_the_back(self, conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", status="ok")
        stuck = _a_card(conn, store, "stuck", checked=ts(20), tried=ts(0))
        waiting = _a_card(conn, store, "waiting", checked=ts(2))

        rows = dbm.stale_offers(conn, 10, ("shopify",))

        assert [row["product_id"] for row in rows] == [waiting, stuck]

    def test_an_attempt_is_written_down_whatever_the_answer(self, conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", status="ok")
        card = _a_card(conn, store, "card", checked=ts(5))

        dbm.note_attempt(conn, card, ts(0))

        tried, checked = conn.execute(
            "SELECT tried_at, checked_at FROM offers WHERE product_id = ?", (card,)
        ).fetchone()
        assert tried == ts(0)
        assert checked == ts(5), "asking is not the shop answering"

    def test_a_shop_whose_last_read_failed_is_not_asked_one_card_at_a_time(self, conn):
        broken = dbm.upsert_store(conn, "broken.example", platform="jsonld", status="error")
        _a_card(conn, broken, "old", checked=ts(20))

        assert dbm.stale_offers(conn, 10, ("jsonld",)) == []


class TestAShopNobodyCouldReadLeavesThePage:
    """Cards stay in the table and come back the moment the shop reads again;
    they are only kept off the page while nobody can vouch for them."""

    def _shelf(self, conn, last_ok_days):
        store = dbm.upsert_store(
            conn, "shop.example", platform="shopify", status="ok", last_ok=ts(last_ok_days)
        )
        return _a_card(conn, store, "card", checked=ts(last_ok_days))

    def test_a_shop_silent_for_days_is_not_on_the_page(self, conn):
        self._shelf(conn, last_ok_days=4)

        rows, total = dbm.offers_for(conn)
        facets = dbm.shelf_facets(conn)

        assert rows == [] and total == 0
        assert facets["total"] == 0, "the counts promise what the page will show"

    def test_a_shop_read_yesterday_is(self, conn):
        card = self._shelf(conn, last_ok_days=1)

        rows, total = dbm.offers_for(conn)

        assert [row["product_id"] for row in rows] == [card] and total == 1
        assert dbm.shelf_facets(conn)["total"] == 1

    def test_the_cards_come_back_when_the_shop_does(self, conn):
        card = self._shelf(conn, last_ok_days=4)
        dbm.upsert_store(conn, "shop.example", last_ok=ts(0))

        rows, _ = dbm.offers_for(conn)

        assert [row["product_id"] for row in rows] == [card]


class TestRecordingWhatIsOnOffer:
    """The shelf's two dates both used to be the clock, and both were wrong.

    A run scores the whole catalogue but reads a slice of it, so "now" is the
    answer to neither "when did this price drop?" nor "when did the shop last
    confirm it?".
    """

    @staticmethod
    def _deal(variant_id, product_id, price=100.0, dropped_hours_ago=None):
        return dealm.Deal(
            variant_id=variant_id, product_id=product_id, price_usd=price,
            reference_usd=200.0, reference_source="market", discount_pct=50.0,
            saving_usd=100.0, score=70, all_time_low=False, fake_sale=False,
            dropped_hours_ago=dropped_hours_ago, history_points=2,
        )

    @staticmethod
    def _shop(conn, domain, last_ok):
        store = dbm.upsert_store(
            conn, domain, platform="shopify", currency="USD", last_ok=last_ok
        )
        product = dbm.upsert_product(conn, store, f"p-{domain}", "Air Max",
                                     f"https://{domain}/p")
        variant = dbm.upsert_variant(conn, product, f"v-{domain}", sku="SKU",
                                     size="US 10", size_norm="US10")
        return product, variant

    def test_checked_at_is_when_the_shop_last_confirmed_it(self, conn):
        """Not when we last scored it. A shop read on Tuesday says Tuesday.

        allikestore.com's -93% Wotherspoon was stamped as checked minutes ago
        while the shop had last been read the day before and the URL had since
        started answering 404.
        """
        product, variant = self._shop(conn, "stale.example", last_ok=ts(4))
        dbm.record_offers(conn, [variant], [self._deal(variant, product)], ts())

        checked = conn.execute("SELECT checked_at FROM offers").fetchone()["checked_at"]
        assert checked == ts(4), "the shop's last successful read, not the clock"

    def test_a_shop_read_this_run_is_fresh(self, conn):
        product, variant = self._shop(conn, "fresh.example", last_ok=ts(0))
        dbm.record_offers(conn, [variant], [self._deal(variant, product)], ts())

        checked = conn.execute("SELECT checked_at FROM offers").fetchone()["checked_at"]
        assert checked == ts(0)

    def test_a_shop_never_read_falls_back_to_the_run(self, conn):
        """A first fill has no `last_ok` to go on, and saying nothing is worse."""
        moment = ts()
        product, variant = self._shop(conn, "new.example", last_ok=None)
        dbm.record_offers(conn, [variant], [self._deal(variant, product)], moment)

        checked = conn.execute("SELECT checked_at FROM offers").fetchone()["checked_at"]
        assert checked == moment

    def test_found_at_is_when_the_price_fell(self, conn):
        """A month-old sale is not a fresh find."""
        product, variant = self._shop(conn, "old.example", last_ok=ts(0))
        dbm.record_offers(
            conn, [variant],
            [self._deal(variant, product, dropped_hours_ago=72)], ts(),
        )
        found = conn.execute("SELECT found_at FROM offers").fetchone()["found_at"]
        assert found < ts(2), "three days ago, not now"

    def test_confirming_the_same_price_does_not_make_it_new_again(self, conn):
        product, variant = self._shop(conn, "same.example", last_ok=ts(2))
        dbm.record_offers(
            conn, [variant], [self._deal(variant, product, dropped_hours_ago=48)], ts())
        first = conn.execute("SELECT found_at FROM offers").fetchone()["found_at"]

        dbm.upsert_store(conn, "same.example", last_ok=ts(0))
        dbm.record_offers(conn, [variant], [self._deal(variant, product)], ts())
        row = conn.execute("SELECT found_at, checked_at FROM offers").fetchone()

        assert row["found_at"] == first, "the same price is the same find"
        assert row["checked_at"] == ts(0), "but it has been confirmed since"

    def test_a_sale_that_ended_leaves_the_shelf(self, conn):
        product, variant = self._shop(conn, "gone.example", last_ok=ts(0))
        dbm.record_offers(conn, [variant], [self._deal(variant, product)], ts())
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1

        written, withdrawn = dbm.record_offers(conn, [variant], [], ts())
        assert (written, withdrawn) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


class TestTheWriteAheadLogIsBounded:
    """A checkpoint moves the journal into the database and leaves the file the
    size it grew to. On the live database that was 1,061 MB of already-written
    log beside a 1,492 MB database — a gigabyte of disk holding nothing."""

    def test_a_connection_caps_how_much_journal_stays_on_disk(self, conn):
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit == dbm.WAL_SIZE_LIMIT

    def test_the_limit_leaves_room_for_a_sweep_to_write(self):
        """Small enough not to dominate the data directory, big enough that a
        run writing a hundred thousand price points does not stall on it."""
        assert 16 * 1024 * 1024 <= dbm.WAL_SIZE_LIMIT <= 256 * 1024 * 1024

    def test_the_journal_is_still_a_write_ahead_log(self, conn):
        """The limit must not have cost the mode it exists to bound."""
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


class TestLookingUpAnArticle:
    """The question the shelf cannot answer: what does this cost anywhere.

    `same_article` starts from a product id, and the page only has ids for what
    is already discounted — so 95% of the catalogue was unreachable.
    """

    @staticmethod
    def _stock(conn, domain, title, price, *, style=None, sku=None, in_stock=True):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, f"p-{domain}-{title}", title, f"https://{domain}/p"
        )
        keys = set()
        if style:
            keys.add(("style", style))
        if sku:
            keys.add(("sku", sku))
        if keys:
            dbm.set_product_keys(conn, product, keys)
        variant = dbm.upsert_variant(conn, product, f"v-{domain}", sku=sku)
        dbm.record_price(conn, variant, price, None, in_stock, "USD", price, 1.0)
        return product

    def test_an_article_number_is_priced_across_shops_cheapest_first(self, conn):
        self._stock(conn, "dear.example", "AF1", 150.0, style="CW2288-111")
        self._stock(conn, "cheap.example", "Air Force 1", 90.0, style="CW2288-111")

        found = dbm.lookup_article(conn, "CW2288-111")

        assert found["matched_by"] == "style"
        assert found["same_thing"] is True
        assert [row["price_usd"] for row in found["shops"]] == [90.0, 150.0]

    def test_it_reaches_what_is_not_discounted(self, conn):
        """The whole point: nothing here is on the shelf, and it still answers."""
        self._stock(conn, "shop.example", "AF1", 110.0, style="CW2288-111")

        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert len(dbm.lookup_article(conn, "CW2288-111")["shops"]) == 1

    def test_one_shop_is_said_plainly_rather_than_implied(self, conn):
        """83% of articles are stocked by nobody else, so this is the usual answer."""
        self._stock(conn, "alone.example", "AF1", 110.0, style="CW2288-111")

        found = dbm.lookup_article(conn, "CW2288-111")
        assert found["found"] == 1

    def test_a_chain_across_countries_is_one_opinion(self, conn):
        self._stock(conn, "footlocker.com", "AF1", 150.0, style="CW2288-111")
        self._stock(conn, "footlocker.de", "AF1", 140.0, style="CW2288-111")

        found = dbm.lookup_article(conn, "CW2288-111")
        assert found["found"] == 1, "one merchant, not two"
        assert found["shops"][0]["price_usd"] == 140.0, "at its cheapest"

    def test_a_price_nobody_can_pay_is_not_a_price(self, conn):
        self._stock(conn, "gone.example", "AF1", 50.0, style="CW2288-111", in_stock=False)
        self._stock(conn, "here.example", "AF1", 120.0, style="CW2288-111")

        found = dbm.lookup_article(conn, "CW2288-111")
        assert [row["domain"] for row in found["shops"]] == ["here.example"]

    def test_a_name_returns_products_not_a_comparison(self, conn):
        """Folding 242 different shoes by merchant would call the cheapest a saving."""
        self._stock(conn, "one.example", "Salomon XT-6 Ember", 140.0)
        self._stock(conn, "one.example", "Salomon XT-6 Skyline", 160.0)

        found = dbm.lookup_article(conn, "Salomon XT-6")

        assert found["matched_by"] == "title"
        assert found["same_thing"] is False
        assert found["found"] == 2, "both products, though one merchant"

    def test_a_word_in_the_sku_field_is_not_an_article_number(self, conn):
        """One shop writes its brand there: `DIME` is 413 unrelated products."""
        for n in range(dbm.MAX_KEY_FANOUT + 1):
            self._stock(conn, "junk.example", f"Thing {n}", 20.0 + n, sku="DIME")

        found = dbm.lookup_article(conn, "DIME")

        assert found["too_common"] == "DIME"
        assert found["matched_by"] != "sku"

    def test_a_word_in_one_shops_sku_field_does_not_shadow_the_name(self, conn):
        """`nike` is one shop's SKU and everybody's brand.

        Taking the SKU would hand back a single shop to somebody who plainly
        asked about a brand, and the answer would look authoritative.
        """
        self._stock(conn, "odd.example", "Some Thing", 30.0, sku="NIKE")
        self._stock(conn, "a.example", "Nike Air Max", 120.0)
        self._stock(conn, "b.example", "Nike Dunk", 110.0)

        found = dbm.lookup_article(conn, "nike")

        assert found["matched_by"] == "title"
        assert found["found"] >= 2

    def test_a_code_nobody_stocks_falls_through_to_the_words(self, conn):
        """A cascade: a step coming up empty is not the end of the search."""
        self._stock(conn, "shop.example", "Zoom Fly ZF1234", 120.0)

        assert dbm.lookup_article(conn, "ZF1234")["found"] == 1

    def test_too_short_to_mean_anything_is_not_a_search(self, conn):
        self._stock(conn, "shop.example", "AF1", 110.0, style="CW2288-111")
        assert dbm.lookup_article(conn, "CW")["shops"] == []
        assert dbm.lookup_article(conn, "")["shops"] == []


class TestFollowingAProduct:
    """Favourites: the one thing the page writes, and what the run reads back."""

    @staticmethod
    def _stock(conn, domain="shop.example", price=180.0, in_stock=True):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, f"p-{domain}", "Salomon XT-6", f"https://{domain}/p",
            brand="Salomon", image_url="https://img.example/x.jpg",
        )
        variant = dbm.upsert_variant(conn, product, f"v-{domain}", size_norm="EU44")
        dbm.record_price(conn, variant, price, None, in_stock, "USD", price, 1.0)
        return product, variant

    def test_starring_writes_down_what_it_costs_today(self, conn):
        """What "it got cheaper" will be measured against later."""
        product, variant = self._stock(conn, price=180.0)

        assert dbm.add_favorite(conn, 7, product, variant) is True

        row = conn.execute("SELECT * FROM favorites").fetchone()
        assert (row["user_id"], row["product_id"], row["variant_id"]) == (7, product, variant)
        assert row["last_price_usd"] == 180.0
        assert row["notify"] == 1

    def test_starring_twice_is_the_same_wish_stated_twice(self, conn):
        product, _ = self._stock(conn)
        dbm.add_favorite(conn, 7, product)

        assert dbm.add_favorite(conn, 7, product) is False
        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 1

    def test_something_sold_out_is_measured_from_what_it_last_cost(self, conn):
        """One of the two ordinary reasons to follow a thing is that it is gone."""
        product, _ = self._stock(conn, price=180.0, in_stock=False)

        dbm.add_favorite(conn, 7, product)

        assert conn.execute("SELECT last_price_usd FROM favorites").fetchone()[0] == 180.0

    def test_a_product_never_priced_at_all_records_nothing(self, conn):
        store = dbm.upsert_store(conn, "quiet.example", platform="shopify")
        product = dbm.upsert_product(conn, store, "q1", "Never priced", "https://q/1")
        dbm.upsert_variant(conn, product, "qv1")

        dbm.add_favorite(conn, 7, product)

        assert conn.execute("SELECT last_price_usd FROM favorites").fetchone()[0] is None

    def test_unstarring_removes_it_and_says_so(self, conn):
        product, _ = self._stock(conn)
        dbm.add_favorite(conn, 7, product)

        assert dbm.remove_favorite(conn, 7, product) is True
        assert dbm.remove_favorite(conn, 7, product) is False
        assert dbm.favorite_ids(conn, 7) == set()

    def test_one_readers_list_is_not_anothers(self, conn):
        mine, _ = self._stock(conn, "mine.example")
        theirs, _ = self._stock(conn, "theirs.example")
        dbm.add_favorite(conn, 7, mine)
        dbm.add_favorite(conn, 9, theirs)

        assert dbm.favorite_ids(conn, 7) == {mine}
        assert dbm.following(conn) == {7: {mine}, 9: {theirs}}

    def test_the_run_reads_everybodys_in_one_query(self, conn):
        one, _ = self._stock(conn, "one.example")
        two, _ = self._stock(conn, "two.example")
        dbm.add_favorite(conn, 7, one)
        dbm.add_favorite(conn, 7, two)
        dbm.add_favorite(conn, 9, two)

        assert dbm.following(conn) == {7: {one, two}, 9: {two}}

    def test_deleting_a_product_takes_its_stars_with_it(self, conn):
        product, _ = self._stock(conn)
        dbm.add_favorite(conn, 7, product)

        conn.execute("DELETE FROM products WHERE id = ?", (product,))

        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0

    def test_the_list_carries_the_price_now_and_the_price_then(self, conn):
        product, _ = self._stock(conn, price=180.0)
        dbm.add_favorite(conn, 7, product)
        conn.execute("UPDATE favorites SET last_price_usd = 220.0")

        item = dbm.favorites_for(conn, 7)[0]

        assert item["price_usd"] == 180.0
        assert item["since_usd"] == 220.0, "what it cost when they were last told"
        assert item["title"] == "Salomon XT-6"
        assert item["domain"] == "shop.example"

    def test_something_sold_out_keeps_its_place_in_the_list(self, conn):
        """The most useful row there is — the thing you are waiting for, gone."""
        product, _ = self._stock(conn, price=180.0, in_stock=False)
        dbm.add_favorite(conn, 7, product)

        item = dbm.favorites_for(conn, 7)[0]

        assert item["product_id"] == product
        assert item["price_usd"] is None

    def test_a_starred_thing_that_is_also_on_the_shelf_says_so(self, conn):
        product, variant = self._stock(conn, price=120.0)
        conn.execute(
            """
            INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                price_usd, reference_usd, reference_source,
                                discount_pct, saving_usd, score, all_time_low)
            VALUES (?, ?, ?, ?, 120.0, 200.0, 'market', 40.0, 80.0, 77, 0)
            """,
            (variant, product, ts(1), ts(0)),
        )
        dbm.add_favorite(conn, 7, product)

        assert dbm.favorites_for(conn, 7)[0]["discount_pct"] == 40.0

    def test_the_price_reported_is_updated_after_a_notification(self, conn):
        product, _ = self._stock(conn, price=180.0)
        dbm.add_favorite(conn, 7, product)

        dbm.record_favorite_price(conn, 7, product, 149.0)

        assert dbm.favorite_prices(conn, 7) == {product: 149.0}


class TestAShopThatAskedNotToBeVisited:
    """`data/excluded.txt` is the answer to being asked, and it is not advisory."""

    @staticmethod
    def _excluded(tmp_path, *domains) -> None:
        (tmp_path / "excluded.txt").write_text(
            "# comment\n" + "\n".join(domains) + "\n", encoding="utf-8"
        )

    def test_an_excluded_shop_is_not_returned(self, conn, tmp_path, monkeypatch):
        dbm.upsert_store(conn, "keep.example", platform="shopify")
        dbm.upsert_store(conn, "gone.example", platform="shopify")
        self._excluded(tmp_path, "gone.example")
        monkeypatch.setattr(dbm, "load_excluded", lambda: frozenset({"gone.example"}))

        assert [s["domain"] for s in dbm.get_stores(conn)] == ["keep.example"]

    def test_naming_it_by_hand_does_not_override_the_list(
        self, conn, tmp_path, monkeypatch
    ):
        dbm.upsert_store(conn, "gone.example", platform="shopify")
        monkeypatch.setattr(dbm, "load_excluded", lambda: frozenset({"gone.example"}))

        # `--stores gone.example` goes through the same door. "We do not want you
        # here" is not a preference a debug flag gets to override.
        assert dbm.get_stores(conn, domains=("gone.example",)) == []

    def test_www_is_the_same_shop(self, conn, monkeypatch):
        dbm.upsert_store(conn, "www.gone.example", platform="shopify")
        monkeypatch.setattr(dbm, "load_excluded", lambda: frozenset({"gone.example"}))

        assert dbm.get_stores(conn) == []

    def test_an_empty_list_changes_nothing(self, conn, monkeypatch):
        dbm.upsert_store(conn, "keep.example", platform="shopify")
        monkeypatch.setattr(dbm, "load_excluded", lambda: frozenset())

        assert [s["domain"] for s in dbm.get_stores(conn)] == ["keep.example"]


def test_the_shipped_exclusion_file_parses(tmp_path):
    from pi.domains import load_excluded

    path = tmp_path / "excluded.txt"
    path.write_text(
        "# a comment\n\nWWW.Gone.Example  # trailing\nother.example\n", encoding="utf-8"
    )
    assert load_excluded(path) == frozenset({"gone.example", "other.example"})


class TestHowFreshTheShelfIs:
    def test_the_share_confirmed_within_two_days_is_counted(self, conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", status="ok",
                                 last_ok=ts(0))
        _a_card(conn, store, "today", checked=ts(0.5))
        _a_card(conn, store, "stale", checked=ts(3))
        _a_card(conn, store, "tagged", checked=ts(0), source="tag")  # not on the default shelf

        assert dbm.shelf_freshness(conn) == (1, 2)


class TestWhenAProductWasLastSeen:
    """A large shop is read a slice at a time, so its last read says nothing
    about a product outside the slice. shop.simon.com holds 77,000 products in
    the database and pages through 25,000; a rebuilt shelf put back cards for
    products it no longer lists, each saying «проверено сегодня»."""

    def _card(self, conn, seen_days_ago):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", status="ok",
                                 last_ok=ts(0))
        card = _a_card(conn, store, "card", checked=ts(0))
        conn.execute("UPDATE products SET last_seen = ? WHERE id = ?", (ts(seen_days_ago), card))
        return card

    def test_a_product_nobody_has_seen_for_a_week_is_off_the_page(self, conn):
        self._card(conn, seen_days_ago=8)
        assert dbm.offers_for(conn) == ([], 0)
        assert dbm.shelf_facets(conn)["total"] == 0
        assert dbm.shelf_freshness(conn) == (0, 0)

    def test_one_seen_this_week_is_on_it(self, conn):
        card = self._card(conn, seen_days_ago=6)
        rows, total = dbm.offers_for(conn)
        assert [row["product_id"] for row in rows] == [card] and total == 1

    def test_a_card_written_now_says_when_its_product_was_seen(self, conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", last_ok=ts(0))
        product = dbm.upsert_product(conn, store, "p", "Shoe", "https://shop.example/p")
        variant = dbm.upsert_variant(conn, product, "v")
        seen = ts(5)
        conn.execute("UPDATE products SET last_seen = ? WHERE id = ?", (seen, product))
        deal = dealm.Deal(
            variant_id=variant, product_id=product, price_usd=50.0, reference_usd=100.0,
            reference_source="history", discount_pct=50.0, saving_usd=50.0, score=80,
            all_time_low=False, fake_sale=False, dropped_hours_ago=None, history_points=2,
        )

        dbm.record_offers(conn, [variant], [deal], ts(0))

        assert conn.execute("SELECT checked_at FROM offers").fetchone()[0] == seen, (
            "not the shop's last read, which never looked at this product"
        )

    def test_the_migration_starts_it_from_the_last_recorded_price(self, tmp_path):
        path = tmp_path / "old.db"
        conn = dbm.connect(path)
        store = dbm.upsert_store(conn, "shop.example", platform="shopify")
        product = dbm.upsert_product(conn, store, "p", "Shoe", "https://shop.example/p")
        variant = dbm.upsert_variant(conn, product, "v")
        dbm.record_price(conn, variant, 50.0, None, True, "USD", 50.0, 1.0, ts=ts(9))
        latest = ts(3)
        dbm.record_price(conn, variant, 45.0, None, True, "USD", 45.0, 1.0, ts=latest)
        conn.execute("UPDATE products SET last_seen = NULL")
        conn.execute("PRAGMA user_version = 15")
        conn.close()

        migrated = dbm.connect(path)

        assert migrated.execute("SELECT last_seen FROM products").fetchone()[0] == latest
