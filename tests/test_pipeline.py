"""End-to-end: a catalogue goes in, a Telegram photo comes out."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import respx

from pi import db as dbm
from pi import deals as dealm
from pi import pipeline, reference
from pi.config import Config, Filters
from pi.fx import Rates
from pi.sources import jsonld
from pi.sources.base import FetchResult, ScrapedProduct, ScrapedVariant

from .conftest import ts

TOKEN, CHAT = "123:AA", "42"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "pi.db",
        sites_file=tmp_path / "sites.txt",
        bot_token=TOKEN,
        chat_id=CHAT,
        concurrency=4,
        shopify_rate=10_000.0,      # the limiter is exercised in test_throttle.py
        shopify_host_rate=10_000.0,
        max_shopify_stores=0,      # no slicing unless a test asks for it
        log_level="WARNING",
        filters=Filters(min_discount_pct=30.0, min_saving_usd=40.0, min_score=50),
    )


def known_store(conn, domain="shop.example", **fields):
    """A store already collected once, so this run is not its baseline pass."""
    defaults = {
        "platform": "shopify", "currency": "GBP", "name": "Shop",
        "last_ok": ts(1), "status": "ok",
    }
    return dbm.upsert_store(conn, domain, **{**defaults, **fields})


def make_due(conn, domain="shop.example"):
    """Age a store so the queue considers it due again.

    The queue collects a shop at most once an hour (once a day if it has never
    found anything), so two runs back to back now collect once — which is the
    point of it, and something a test spanning two runs has to say out loud.
    """
    dbm.upsert_store(conn, domain, last_ok=ts(1))


def _mock_rates():
    respx.get("https://api.frankfurter.dev/v1/latest").mock(
        return_value=httpx.Response(200, json={"base": "USD", "rates": {"GBP": 0.73, "EUR": 0.86}})
    )


def _mock_product_pages(payload, domain="shop.example"):
    """Answer /products/<handle>.json the way a real Shopify shop does.

    A run verifies the oldest cards on its own shelf by opening one product at
    a time, so a fake shop that only serves its catalogue is no longer a
    complete fake shop.
    """
    for raw in payload["products"]:
        respx.get(f"https://{domain}/products/{raw['handle']}.json").mock(
            return_value=httpx.Response(200, json={"product": raw})
        )


def _mock_telegram():
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    return photo, text


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    """Skip the polite inter-message pause so the suite stays fast."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", instant)


@respx.mock
async def test_a_discounted_shopify_catalogue_produces_a_photo_alert(config, shopify_payload):
    _mock_rates()
    photo, _ = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)

    stats = await pipeline.run(config, conn)

    assert stats.stores_ok == 1
    assert stats.products_seen == len(shopify_payload["products"])
    assert stats.points_written > 0
    assert stats.alerts_sent > 0
    assert photo.called, "a deal with an image must go out as a photo"

    body = json.loads(photo.calls[0].request.content)
    assert body["photo"].startswith("http")
    assert "−" in body["caption"] and "%" in body["caption"]
    assert body["parse_mode"] == "HTML"


@pytest.mark.asyncio
@respx.mock
async def test_a_run_classifies_what_it_collected(config, shopify_payload):
    """Brand, gender and kind must be filled by the run, not wait for a command."""
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn)

    unclassified = conn.execute(
        "SELECT COUNT(*) FROM products WHERE kind IS NULL AND brand_norm IS NULL"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert total > 0
    assert unclassified < total, "the run left every product unclassified"


@respx.mock
async def test_a_sold_out_discount_is_never_announced(config, shopify_payload):
    """The fixture holds a whole product discounted 29% but out of stock in every size."""
    _mock_rates()
    photo, _ = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn)

    announced = {
        json.loads(call.request.content)["caption"] for call in photo.calls
    }
    assert announced, "the in-stock deals still go out"
    assert not any("Vomero" in caption for caption in announced)


@respx.mock
async def test_the_first_pass_over_a_new_store_is_a_baseline_not_news(config, shopify_payload):
    """Every standing sale looks new the first time a shop is read.

    Announcing them all would bury the user under months-old discounts, so the
    first collection records prices silently and the next run reports movement.
    """
    _mock_rates()
    photo, text = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    dbm.upsert_store(conn, "shop.example", platform="shopify", currency="GBP")  # never collected

    first = await pipeline.run(config, conn)
    assert first.points_written > 0, "prices are still recorded"
    assert first.alerts_sent == 0, "but nothing is announced"
    assert not photo.called and not text.called

    # Now the shop cuts a price further; that is real news.
    cheaper = json.loads(json.dumps(shopify_payload))
    for product in cheaper["products"]:
        for variant in product["variants"]:
            variant["available"] = True
            variant["price"] = f"{float(variant['price']) / 2:.2f}"
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=cheaper)
    )

    make_due(conn)
    second = await pipeline.run(config, conn)
    assert second.alerts_sent > 0
    assert photo.called or text.called


@respx.mock
async def test_prices_are_converted_from_the_shops_currency(config, shopify_payload):
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn, collect_only=True)

    row = conn.execute(
        "SELECT price_usd, price_native, currency, fx_rate FROM price_points LIMIT 1"
    ).fetchone()
    assert row["currency"] == "GBP"
    assert row["fx_rate"] == 0.73
    assert row["price_usd"] == pytest.approx(row["price_native"] / 0.73, abs=0.02)


@respx.mock
async def test_the_same_deal_is_not_sent_twice(config, shopify_payload):
    _mock_rates()
    _mock_product_pages(shopify_payload)
    photo, _ = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)

    first = await pipeline.run(config, conn)
    assert first.alerts_sent > 0
    sent_first_time = photo.call_count

    make_due(conn)
    second = await pipeline.run(config, conn)
    assert second.alerts_sent == 0
    assert photo.call_count == sent_first_time


@respx.mock
async def test_one_product_discounted_in_many_sizes_is_announced_once(config, shopify_payload):
    """Eight sizes of the same hoodie on sale is one piece of news, not eight."""
    _mock_rates()
    photo, text = _mock_telegram()

    product = json.loads(json.dumps(shopify_payload["products"][2]))  # the ASICS
    for index, variant in enumerate(product["variants"]):
        variant["id"] = 900_000 + index
        variant["available"] = True
        variant["option1"] = f"{4 + index}"
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json={"products": [product]})
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    stats = await pipeline.run(config, conn)

    assert conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0] == len(product["variants"])
    assert stats.alerts_sent == 1
    assert photo.call_count + text.call_count == 1


@respx.mock
async def test_a_failed_send_is_not_recorded_as_sent(config, shopify_payload):
    """A rejected message must be retried next run, not silently swallowed."""
    _mock_rates()
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "bad image"})
    )
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(403, json={"ok": False, "description": "blocked"})
    )
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    stats = await pipeline.run(config, conn)

    assert stats.alerts_sent == 0
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


@respx.mock
async def test_a_store_failure_is_reported_not_hidden(config):
    _mock_rates()
    _mock_telegram()
    respx.get("https://broken.example/products.json?limit=250").mock(
        return_value=httpx.Response(500)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn, "broken.example", currency="USD")
    stats = await pipeline.run(config, conn)

    assert stats.stores_ok == 0
    assert stats.stores_failed == 1
    assert stats.failures[0][0] == "broken.example"
    assert conn.execute("SELECT status FROM stores").fetchone()[0] == "error"


@respx.mock
async def test_dry_run_sends_nothing(config, shopify_payload, capsys):
    _mock_rates()
    photo, text = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn, dry_run=True)

    assert not photo.called and not text.called
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert "%" in capsys.readouterr().out


@respx.mock
async def test_a_brand_you_did_not_name_still_reaches_you(config, shopify_payload):
    """Named brands are a priority, not a gate.

    A hard list fails precisely on what is not in it: a find in a brand you had
    not thought of would never arrive, and you would never learn that it had not.
    So naming brands moves the bar and the ordering, and everything else still
    gets through on the strength of the discount alone.
    """
    _mock_rates()
    photo, text = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    config = Config(**{**config.__dict__, "filters": Filters(brands_allow=("no-such-brand",))})
    conn = dbm.connect(config.db_path)
    known_store(conn)
    stats = await pipeline.run(config, conn)

    assert stats.points_written > 0, "collection still happens"
    assert stats.alerts_sent > 0, "an unnamed brand is not silenced"
    assert photo.called or text.called


def test_health_report_names_what_is_wrong(config):
    conn = dbm.connect(config.db_path)
    dbm.upsert_store(conn, "good.example", platform="shopify", status="ok")
    dbm.upsert_store(conn, "walled.example", platform="blocked")
    dbm.upsert_store(
        conn, "broken.example", platform="shopify", status="error", last_error="HTTP 500"
    )
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, stores_ok, stores_failed, products_seen)"
        " VALUES (?, ?, 2, 1, 1234)",
        (ts(0), ts(0)),
    )

    report = pipeline.health_report(conn)
    assert "UTC" in report, "the summary must date its figures"
    assert "broken.example" in report
    assert "HTTP 500" in report
    assert "закрыты анти-ботом" in report
    assert "1,234" in report


@respx.mock
async def test_a_shop_with_no_machine_readable_prices_is_not_recrawled(config):
    """Some shops publish no structured data at all — nativeskatestore.co.uk is one.

    Crawling 200 of its product pages every six hours costs thousands of
    requests and finds nothing, so a store that has never once yielded a
    product and said why is set aside.
    """
    _mock_rates()
    _mock_telegram()
    conn = dbm.connect(config.db_path)
    dbm.upsert_store(
        conn, "bare.example", platform="jsonld", status="error",
        last_error="no schema.org/Product markup found",
    )
    # Not mocked: any request at all would fail the test.
    stats = await pipeline.run(config, conn)
    assert stats.stores_ok == 0 and stats.stores_failed == 0


@respx.mock
async def test_a_store_that_has_worked_before_is_always_retried(config, shopify_payload):
    """One bad sweep must not retire a shop that is merely having a bad day."""
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    conn = dbm.connect(config.db_path)
    known_store(conn, status="error", last_error="no product URLs in sitemap")

    stats = await pipeline.run(config, conn)
    assert stats.stores_ok == 1


@respx.mock
async def test_naming_a_store_explicitly_overrides_the_skip(config, shopify_payload):
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    conn = dbm.connect(config.db_path)
    dbm.upsert_store(
        conn, "shop.example", platform="shopify", currency="GBP", status="error",
        last_error="no product URLs in sitemap",
    )
    stats = await pipeline.run(config, conn, domains=("shop.example",))
    assert stats.stores_ok == 1


@respx.mock
async def test_deals_past_the_cap_are_reconsidered_not_lost(config, shopify_payload):
    """A good deal must not vanish because fifteen better ones arrived with it.

    Over-cap deals are never recorded in `alerts`, and a normal run only scores
    variants whose price moved — so without a carry-over they would be lost for
    good rather than merely delayed.
    """
    _mock_rates()
    photo, text = _mock_telegram()

    # Three products on sale, but only one notification allowed per run.
    product = json.loads(json.dumps(shopify_payload["products"][2]))
    catalogue = []
    for n in range(3):
        copy = json.loads(json.dumps(product))
        copy["id"] = 700_000 + n
        copy["handle"] = f"shoe-{n}"
        copy["variants"] = [copy["variants"][0]]
        copy["variants"][0]["id"] = 800_000 + n
        copy["variants"][0]["available"] = True
        catalogue.append(copy)
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json={"products": catalogue})
    )
    _mock_product_pages({"products": catalogue})

    conn = dbm.connect(config.db_path)
    known_store(conn)

    first = await pipeline.run(config, conn, limit=1)
    assert first.alerts_sent == 1
    assert conn.execute("SELECT capped FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == 1

    # Nothing about the catalogue changes, so a run that only looked at moved
    # prices would send nothing at all.
    make_due(conn)
    second = await pipeline.run(config, conn, limit=1)
    assert second.alerts_sent == 1, "the deferred deal goes out next time"

    make_due(conn)
    third = await pipeline.run(config, conn, limit=1)
    assert third.alerts_sent == 1

    make_due(conn)
    fourth = await pipeline.run(config, conn, limit=1)
    assert fourth.alerts_sent == 0, "and then it goes quiet"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 3
    assert photo.call_count + text.call_count == 3


@respx.mock
async def test_seeding_silences_the_backlog_of_standing_sales(config, shopify_payload):
    """A shop's existing sales are not news, and there can be tens of thousands.

    Measured on the real database: 29,853 products qualified at once. Draining
    that at max_alerts_per_run would mean months of notifications about sales
    that started before the bot existed.
    """
    _mock_rates()
    photo, text = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn, collect_only=True)

    counted = pipeline.seed_alerts(conn, config, dry_run=True)
    assert counted > 0, "the fixture holds qualifying discounts"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0, "dry run writes nothing"

    seeded = pipeline.seed_alerts(conn, config)
    assert seeded == counted
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == seeded

    # Nothing is announced now, because none of it is new.
    stats = await pipeline.run(config, conn, rescan=True)
    assert stats.alerts_sent == 0
    assert not photo.called and not text.called


@respx.mock
async def test_a_price_drop_after_seeding_is_still_announced(config, shopify_payload):
    """Seeding must silence the backlog without deafening the bot."""
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn, collect_only=True)
    pipeline.seed_alerts(conn, config)

    cheaper = json.loads(json.dumps(shopify_payload))
    for product in cheaper["products"]:
        for variant in product["variants"]:
            variant["available"] = True
            variant["price"] = f"{float(variant['price']) / 3:.2f}"
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=cheaper)
    )

    make_due(conn)
    stats = await pipeline.run(config, conn)
    assert stats.alerts_sent > 0, "a genuine further drop still gets through"


def test_a_run_takes_a_slice_of_shopify_stores_not_all_of_them(config, conn):
    """Shopify's per-IP quota tolerates a few dozen stores at a time — measured
    between twelve and forty depending on how much the IP has been used. Charging
    at all 138 only means being cut off part-way through."""
    for n in range(10):
        dbm.upsert_store(conn, f"shop{n}.example", platform="shopify")
    for n in range(3):
        dbm.upsert_store(conn, f"other{n}.example", platform="jsonld")

    stores = dbm.get_stores(conn, platforms=("shopify", "jsonld"))
    kept, deferred = pipeline._take_shopify_slice(stores, budget=4)

    assert deferred == 6
    assert sum(1 for s in kept if s["platform"] == "shopify") == 4
    assert sum(1 for s in kept if s["platform"] == "jsonld") == 3, "jsonld is not sliced"


def test_successive_runs_cover_every_shopify_store(config, conn):
    """The slice is only useful if it moves along; ordering is what makes it."""
    for n in range(9):
        dbm.upsert_store(conn, f"shop{n}.example", platform="shopify")

    seen: set[str] = set()
    for _ in range(3):
        stores = dbm.get_stores(conn, platforms=("shopify",))
        kept, _ = pipeline._take_shopify_slice(stores, budget=3)
        for store in kept:
            seen.add(store["domain"])
            dbm.upsert_store(conn, store["domain"], last_ok=dbm.utcnow())

    assert len(seen) == 9, "three runs of three cover all nine"


def test_a_budget_of_zero_means_no_slicing(config, conn):
    for n in range(5):
        dbm.upsert_store(conn, f"shop{n}.example", platform="shopify")
    stores = dbm.get_stores(conn, platforms=("shopify",))
    kept, deferred = pipeline._take_shopify_slice(stores, budget=0)
    assert deferred == 0 and len(kept) == 5


def test_the_summary_counts_notifications_not_seeded_rows(config, conn):
    """`pi seed` writes tens of thousands of rows to suppress notifications.

    Counting them as alerts made the daily summary report 29,855 messages that
    nobody received — the one number the summary exists to convey.
    """
    dbm.upsert_store(conn, "shop.example", platform="shopify")
    product = dbm.upsert_product(conn, 1, "p", "T", "https://u")
    variant = dbm.upsert_variant(conn, product, "v")

    deal = dealm.Deal(
        variant_id=variant, product_id=product, price_usd=100.0, reference_usd=200.0,
        reference_source="tag", discount_pct=50.0, saving_usd=100.0, score=80,
        all_time_low=False, fake_sale=False, dropped_hours_ago=None, history_points=1,
    )
    dealm.record_alert(conn, deal, dbm.utcnow(), sent=False)
    dealm.record_alert(conn, replace(deal, price_usd=50.0), dbm.utcnow(), sent=True)

    report = pipeline.health_report(conn)
    assert "Уведомлений за сутки: 1" in report


def test_seeding_is_not_limited_by_the_per_store_cap(config, conn):
    """The cap stops one shop filling a notification run. Seeding is not a run:
    anything it trims comes straight back as news on the next sweep."""
    store = dbm.upsert_store(conn, "sale.example", platform="shopify", currency="USD")
    for n in range(8):
        product = dbm.upsert_product(conn, store, f"p{n}", f"Shoe {n}", f"https://u/{n}")
        variant = dbm.upsert_variant(conn, product, f"v{n}")
        dbm.record_price(
            conn, variant, 100.0, 300.0, True, "USD", 100.0, 1.0,
            ts=dbm.utcnow(), compare_at_native=300.0,
        )
    conn.execute("UPDATE stores SET last_ok = ?", (dbm.utcnow(),))

    capped = pipeline.find_deals(conn, pipeline.all_scorable_variants(conn), config)
    assert len(capped) == config.filters.max_alerts_per_store

    seeded = pipeline.seed_alerts(conn, config)
    assert seeded == 8, "every qualifying deal is accounted for"
    assert pipeline.seed_alerts(conn, config, dry_run=True) == 0, "and nothing is left over"


def test_the_summary_says_when_a_run_was_cut_short(config, conn):
    """Otherwise "21 ok, 18 failed" reads like a bad day rather than a block."""
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, stores_ok, stores_failed, blocked)"
        " VALUES (?, ?, 21, 18, 1)",
        (ts(0), ts(0)),
    )
    assert "Shopify заблокировал IP" in pipeline.health_report(conn)


def test_the_summary_shows_how_much_of_the_list_is_going_stale(config, conn):
    """Coverage is the thing to watch when runs are sliced."""
    dbm.upsert_store(conn, "fresh.example", platform="shopify", last_ok=dbm.utcnow())
    dbm.upsert_store(conn, "old.example", platform="shopify", last_ok=ts(3))
    dbm.upsert_store(conn, "never.example", platform="shopify")

    assert "Shopify не обновлялись сутки: 2 из 3" in pipeline.health_report(conn)


class TestOneAlertPerArticle:
    """The same shoe on offer in three shops used to be three notifications.

    Alerts are unique per product, and a product is a row in one shop's
    catalogue — so nothing stopped the same article arriving three times over
    with three different shop names on it.
    """

    @staticmethod
    def _deal(product_id: int, score: int) -> dealm.Deal:
        return dealm.Deal(
            variant_id=product_id, product_id=product_id, price_usd=100.0,
            reference_usd=200.0, reference_source="market", discount_pct=50.0,
            saving_usd=100.0, score=score, all_time_low=False, fake_sale=False,
            dropped_hours_ago=1.0, history_points=2,
        )

    class _Market:
        """Stands in for the index: products 1-3 are the same article, 4 is not."""

        def identity(self, product_id: int):
            return ("style", "CW2288-111") if product_id in (1, 2, 3) else None

    def test_three_shops_selling_one_article_produce_one_notification(self):
        found = [(self._deal(pid, score), None) for pid, score in ((1, 90), (2, 80), (3, 70))]
        kept = pipeline._one_alert_per_article(found, self._Market())

        assert len(kept) == 1
        deal, _ = kept[0]
        assert deal.product_id == 1, "the best-scoring one is the one that goes out"
        assert deal.also_in_shops == 2, "and it says how many others had it"

    def test_a_product_nobody_else_stocks_is_left_alone(self):
        found = [(self._deal(4, 90), None), (self._deal(5, 80), None)]
        kept = pipeline._one_alert_per_article(found, self._Market())

        assert len(kept) == 2
        assert all(deal.also_in_shops == 0 for deal, _ in kept)

    def test_seeding_still_accounts_for_every_shop_selling_it(self, config, conn):
        """A duplicate folded away at seeding time comes back as news later.

        Seeding records what is already on offer so it is never announced. A
        deal it dropped into another's count was never recorded, so the next
        sweep finds it standing there and calls it new.
        """
        for n, domain in enumerate(("one.example", "two.example", "three.example")):
            store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
            product = dbm.upsert_product(
                conn, store, f"p{n}", "Nike Air Force 1 CW2288-111", f"https://{domain}/x"
            )
            variant = dbm.upsert_variant(conn, product, f"v{n}", sku="CW2288-111")
            dbm.set_product_keys(
                conn, product,
                reference.keys_for("Nike", "Nike Air Force 1 CW2288-111", ["CW2288-111"]),
            )
            dbm.record_price(
                conn, variant, 100.0, 300.0, True, "USD", 100.0, 1.0,
                ts=dbm.utcnow(), compare_at_native=300.0,
            )
        conn.execute("UPDATE stores SET last_ok = ?", (dbm.utcnow(),))

        sent = pipeline.find_deals(conn, pipeline.all_scorable_variants(conn), config)
        assert len(sent) == 1, "one article, one notification"
        assert sent[0][0].also_in_shops == 2

        assert pipeline.seed_alerts(conn, config) == 3, "but all three are suppressed"
        assert pipeline.seed_alerts(conn, config, dry_run=True) == 0


class TestStoreResult:
    """Persisting one shop's catalogue — the path everything else is built on."""

    @staticmethod
    def _result(domain="shop.com"):
        return FetchResult(
            domain=domain,
            currency="USD",
            products=[
                ScrapedProduct(
                    external_id="p1",
                    title="Wmns Air Force 1",
                    url="https://shop.com/p1",
                    brand="Nike",
                    category="Sneakers",
                    variants=[
                        ScrapedVariant(external_id="v1", price=100.0, size="US 7"),
                        ScrapedVariant(external_id="v2", price=100.0, size="US 7.5"),
                    ],
                )
            ],
        )

    def test_it_reports_the_products_it_touched(self, conn):
        """The run classifies these afterwards, so losing one loses its brand and kind."""
        store_id = dbm.upsert_store(conn, "shop.com")
        rates = Rates({"USD": 1.0}, fetched_at=datetime.now(UTC), source="test")
        written, changed, products = pipeline.store_result(
            conn, store_id, self._result(), rates
        )
        assert written == 2
        assert len(changed) == 2
        assert len(products) == 1
        stored = conn.execute("SELECT id FROM products").fetchone()[0]
        assert products == [stored]

    def test_a_second_pass_over_unchanged_prices_writes_no_points(self, conn):
        """The reason hourly collection costs almost nothing in disk."""
        store_id = dbm.upsert_store(conn, "shop.com")
        rates = Rates({"USD": 1.0}, fetched_at=datetime.now(UTC), source="test")
        pipeline.store_result(conn, store_id, self._result(), rates)
        written, changed, products = pipeline.store_result(
            conn, store_id, self._result(), rates
        )
        assert written == 0
        assert changed == []
        assert len(products) == 1  # still touched, still worth classifying


class TestAProductInItsOwnCurrency:
    """A shop need not price its whole catalogue in one currency."""

    @staticmethod
    def _mixed():
        """One shop, two currencies — the shape www.ssense.com actually serves."""
        return FetchResult(
            domain="ssense.test",
            currency="USD",  # what the shop is usually in, and what it says on the store row
            products=[
                ScrapedProduct(
                    external_id="us", title="Sweatshirt", url="https://ssense.test/en-us/us",
                    currency="USD",
                    variants=[ScrapedVariant(external_id="v1", price=100.0)],
                ),
                ScrapedProduct(
                    external_id="ca", title="Bottle", url="https://ssense.test/en-ca/ca",
                    currency="CAD",
                    variants=[ScrapedVariant(external_id="v2", price=100.0)],
                ),
            ],
        )

    @staticmethod
    def _rates():
        return Rates({"USD": 1.0, "CAD": 1.37}, fetched_at=datetime.now(UTC), source="test")

    def test_each_product_is_priced_in_the_currency_its_page_named(self, conn):
        store_id = dbm.upsert_store(conn, "ssense.test")
        pipeline.store_result(conn, store_id, self._mixed(), self._rates())
        rows = dict(
            conn.execute(
                """
                SELECT p.external_id, pp.currency FROM price_points pp
                  JOIN variants v ON v.id = pp.variant_id
                  JOIN products p ON p.id = v.product_id
                """
            ).fetchall()
        )
        assert rows == {"us": "USD", "ca": "CAD"}

    def test_the_canadian_price_is_converted_not_taken_at_face_value(self, conn):
        """The whole point: 100 CAD is not 100 dollars, and the shelf compares dollars."""
        store_id = dbm.upsert_store(conn, "ssense.test")
        pipeline.store_result(conn, store_id, self._mixed(), self._rates())
        usd = dict(
            conn.execute(
                """
                SELECT p.external_id, pp.price_usd FROM price_points pp
                  JOIN variants v ON v.id = pp.variant_id
                  JOIN products p ON p.id = v.product_id
                """
            ).fetchall()
        )
        assert usd["us"] == pytest.approx(100.0)
        assert usd["ca"] == pytest.approx(100.0 / 1.37, rel=1e-3)

    def test_a_product_that_named_nothing_falls_back_to_the_shop(self, conn):
        """Shopify sets no per-product currency, and must keep working unchanged."""
        store_id = dbm.upsert_store(conn, "shopify.test")
        result = FetchResult(
            domain="shopify.test", currency="GBP",
            products=[
                ScrapedProduct(
                    external_id="p", title="Tee", url="https://shopify.test/p",
                    variants=[ScrapedVariant(external_id="v", price=50.0)],
                )
            ],
        )
        rates = Rates({"USD": 1.0, "GBP": 0.8}, fetched_at=datetime.now(UTC), source="test")
        pipeline.store_result(conn, store_id, result, rates)
        assert conn.execute("SELECT currency FROM price_points").fetchone()[0] == "GBP"


class TestRetiringAHopelessShop:
    """The pipeline drops shops that will never publish a price — if it recognises them."""

    def test_the_adapter_and_the_pipeline_agree_on_the_wording(self):
        """They kept separate copies of these strings, and the copies drifted apart."""
        assert jsonld.NO_PRODUCT_URLS in pipeline.HOPELESS_ERRORS
        assert jsonld.NO_MARKUP in pipeline.HOPELESS_ERRORS

    def test_a_shop_with_no_product_urls_is_set_aside(self, conn):
        """www.pace-sneakers.de is a one-page site. It was crawled hourly for a week."""
        known_store(conn, "one-pager.test", last_ok=None)
        conn.execute(
            "UPDATE stores SET platform = 'jsonld', last_error = ?, last_ok = NULL",
            (jsonld.NO_PRODUCT_URLS,),
        )
        stores = dbm.get_stores(conn, platforms=("shopify", "jsonld"))
        kept, dropped = pipeline._drop_hopeless(stores)
        assert dropped == 1
        assert kept == []

    def test_a_shop_that_once_worked_is_kept(self, conn):
        """One bad sweep must not retire a shop that has produced prices before."""
        known_store(conn, "was-fine.test", last_ok=ts(1))
        conn.execute(
            "UPDATE stores SET platform = 'jsonld', last_error = ?",
            (jsonld.NO_PRODUCT_URLS,),
        )
        stores = dbm.get_stores(conn, platforms=("shopify", "jsonld"))
        kept, dropped = pipeline._drop_hopeless(stores)
        assert dropped == 0
        assert len(kept) == 1


class TestUnevenQueue:
    """Which shops get collected this hour, and which wait their turn."""

    @staticmethod
    def _store(conn, domain, hours_ago):
        return known_store(conn, domain, last_ok=ts(hours_ago / 24))

    def test_a_shop_that_finds_things_is_due_every_hour(self, conn):
        store = self._store(conn, "good.example", hours_ago=2)
        stores = dbm.get_stores(conn)
        due, waiting = pipeline.due_stores(stores, productive={store})
        assert [s["domain"] for s in due] == ["good.example"]
        assert waiting == 0

    def test_a_shop_that_never_finds_anything_waits_a_day(self, conn):
        self._store(conn, "quiet.example", hours_ago=2)
        due, waiting = pipeline.due_stores(dbm.get_stores(conn), productive=set())
        assert due == []
        assert waiting == 1

    def test_it_comes_round_once_the_day_has_passed(self, conn):
        self._store(conn, "quiet.example", hours_ago=30)
        due, _ = pipeline.due_stores(dbm.get_stores(conn), productive=set())
        assert [s["domain"] for s in due] == ["quiet.example"]

    def test_a_productive_shop_outranks_a_quiet_one_that_is_older(self, conn):
        """Two hours into a one-hour interval beats thirty into a twenty-four."""
        good = self._store(conn, "good.example", hours_ago=2)
        self._store(conn, "quiet.example", hours_ago=30)
        due, _ = pipeline.due_stores(dbm.get_stores(conn), productive={good})
        assert [s["domain"] for s in due] == ["good.example", "quiet.example"]

    def test_a_shop_never_collected_goes_first(self, conn):
        self._store(conn, "old.example", hours_ago=100)
        dbm.upsert_store(conn, "new.example", platform="shopify", status="ok")
        due, _ = pipeline.due_stores(dbm.get_stores(conn), productive=set())
        assert due[0]["domain"] == "new.example"


class TestAdaptiveBudget:
    """The slice fits a quota that changes through the day, so it changes too."""

    @staticmethod
    def _run(conn, budget, blocked):
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, shopify_budget, blocked) "
            "VALUES (?, ?, ?, ?)",
            (ts(), ts(), budget, blocked),
        )

    def test_with_no_history_it_starts_at_the_ceiling(self, conn):
        assert pipeline._adaptive_budget(conn, ceiling=45) == 45

    def test_a_clean_run_earns_a_few_more_shops(self, conn):
        self._run(conn, budget=20, blocked=0)
        assert pipeline._adaptive_budget(conn, ceiling=45) == 25

    def test_a_block_costs_more_than_a_clean_run_earns(self, conn):
        """Retreat faster than you advance: a block is expensive, a short run is not."""
        self._run(conn, budget=30, blocked=1)
        assert pipeline._adaptive_budget(conn, ceiling=45) == 19

    def test_it_never_falls_below_the_floor(self, conn):
        self._run(conn, budget=10, blocked=1)
        assert pipeline._adaptive_budget(conn, ceiling=45) == pipeline.BUDGET_FLOOR

    def test_it_never_climbs_past_the_ceiling(self, conn):
        self._run(conn, budget=45, blocked=0)
        assert pipeline._adaptive_budget(conn, ceiling=45) == 45

    def test_a_hand_run_sweep_does_not_count_as_evidence(self, conn):
        """`--stores` visits what it was told to, so its budget measures nothing.

        Live 29.08: a `--stores` run of eight shops recorded a budget of 45, and
        the next scheduled sweep read that back as forty-five shops having just
        gone through cleanly and set off at full width into a quota that was
        still exhausted.
        """
        self._run(conn, budget=10, blocked=1)
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, shopify_budget, blocked, scope) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts(), ts(), 45, 0, "stores"),
        )
        assert pipeline._adaptive_budget(conn, ceiling=45) == pipeline.BUDGET_FLOOR


class TestDegradationNotice:
    """Saying so when the collector quietly stops working."""

    @staticmethod
    def _runs(conn, seen: list[int], scope="sweep", blocked=0):
        for products in seen:
            conn.execute(
                "INSERT INTO runs (started_at, finished_at, products_seen, scope, blocked) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts(), ts(), products, scope, blocked),
            )

    def test_healthy_collection_says_nothing(self, conn):
        self._runs(conn, [8000] * 25)
        assert pipeline.degradation_notice(conn) is None

    def test_it_speaks_up_after_a_run_of_weak_sweeps(self, conn):
        self._runs(conn, [8000] * 20)
        self._runs(conn, [1000] * 5)
        notice = pipeline.degradation_notice(conn)
        assert notice is not None
        assert "меньше обычного" in notice

    def test_one_short_run_is_not_news(self, conn):
        """The quota varies through the day and shops go down on their own."""
        self._runs(conn, [8000] * 24)
        self._runs(conn, [1000])
        assert pipeline.degradation_notice(conn) is None

    def test_it_says_it_once_and_not_every_run_after(self, conn):
        self._runs(conn, [8000] * 20)
        self._runs(conn, [1000] * 5)
        assert pipeline.degradation_notice(conn) is not None
        self._runs(conn, [1000])
        assert pipeline.degradation_notice(conn) is None

    def test_a_hand_run_sweep_of_one_shop_is_not_a_collapse(self, conn):
        """`pi run --stores one.com` reads one shop on purpose."""
        self._runs(conn, [8000] * 20)
        self._runs(conn, [500] * 5, scope="stores")
        assert pipeline.degradation_notice(conn) is None

    def test_it_waits_until_it_knows_what_normal_is(self, conn):
        self._runs(conn, [100] * 6)
        assert pipeline.degradation_notice(conn) is None

    def test_blocks_are_named_as_the_likely_cause(self, conn):
        self._runs(conn, [8000] * 20)
        self._runs(conn, [1000] * 5, blocked=1)
        notice = pipeline.degradation_notice(conn)
        assert "квота" in notice


@pytest.mark.asyncio
@respx.mock
async def test_a_run_leaves_what_is_on_offer_on_the_shelf(config, shopify_payload):
    """The bot reads this table; searching the database instead takes minutes."""
    _mock_rates()
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn)

    offers = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM alerts WHERE sent = 1").fetchone()[0]
    assert offers >= sent > 0, "everything announced is also on the shelf"


@pytest.mark.asyncio
@respx.mock
async def test_an_offer_is_withdrawn_when_the_sale_ends(config, shopify_payload):
    _mock_rates()
    _mock_product_pages(shopify_payload)
    _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    conn = dbm.connect(config.db_path)
    known_store(conn)
    await pipeline.run(config, conn)
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] > 0

    # The shop puts its prices back up: same catalogue, no discount left.
    full_price = json.loads(json.dumps(shopify_payload))
    for product in full_price["products"]:
        for variant in product["variants"]:
            variant["compare_at_price"] = None
            variant["price"] = f"{float(variant['price']) * 4:.2f}"
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=full_price)
    )
    # The product's own page says the same thing as the catalogue, because it is
    # the same shop: a run also checks cards one at a time, and a mock still
    # serving yesterday's price would put the sale straight back on the shelf.
    _mock_product_pages(full_price)
    make_due(conn)
    await pipeline.run(config, conn)

    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0, (
        "a sale that ended must leave the shelf, or the bot shows prices that are gone"
    )


class TestRecordingABlock:
    """Being shut out has to be written down on every way out of a run.

    It was not written down on the `--collect-only` path, which returns before
    the bookkeeping. Live 29.08: a collect-only run lost 22 stores to the block
    and still recorded `blocked = 0`, so the next sweep read the quota as healthy
    and set off at full width.
    """

    @staticmethod
    def _shut_out(monkeypatch):
        class ShutOut(pipeline.RateLimiter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.abandoned = 3

        monkeypatch.setattr(pipeline, "RateLimiter", ShutOut)

    @respx.mock
    async def test_a_collect_only_run_still_records_it(
        self, config, shopify_payload, monkeypatch
    ):
        _mock_rates()
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        self._shut_out(monkeypatch)

        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn, collect_only=True)

        assert conn.execute("SELECT blocked FROM runs").fetchone()["blocked"] == 1

    @respx.mock
    async def test_a_full_run_records_it_too(self, config, shopify_payload, monkeypatch):
        _mock_rates()
        _mock_telegram()
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        self._shut_out(monkeypatch)

        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn)

        assert conn.execute("SELECT blocked FROM runs").fetchone()["blocked"] == 1

    @respx.mock
    async def test_an_untroubled_run_records_nothing(self, config, shopify_payload):
        _mock_rates()
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn, collect_only=True)

        assert conn.execute("SELECT blocked FROM runs").fetchone()["blocked"] == 0


class TestTheJournalKeepsErrorsForThingsThatNeedSomebody:
    """A platform block happens on every sweep and the design absorbs it: the
    skipped shops head the next run's queue, and 143 of the 146 Shopify shops
    are still read within a day. Logged at ERROR it filled the journal a hundred
    lines at a time and buried the failures that do need a person."""

    def test_a_shopify_block_is_a_warning_not_an_error(self, conn, caplog):
        class Blocked:
            abandoned = 7
            penalties = 0

        run_id = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (dbm.utcnow(),)
        ).lastrowid
        with caplog.at_level(logging.DEBUG, logger="pi.pipeline"):
            pipeline._record_block(conn, run_id, Blocked())

        said = [r for r in caplog.records if "blocked this IP" in r.getMessage()]
        assert said, "the block is still reported"
        assert [r.levelno for r in said] == [logging.WARNING]

    def test_the_run_is_still_recorded_as_blocked(self, conn):
        """The level changed; the bookkeeping the next run adapts from did not."""
        class Blocked:
            abandoned = 7
            penalties = 0

        run_id = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (dbm.utcnow(),)
        ).lastrowid
        pipeline._record_block(conn, run_id, Blocked())
        assert conn.execute(
            "SELECT blocked FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0] == 1


class TestAPriceThatCannotBeAPrice:
    """One shop published t-shirts at 333,085,723 and the shelf believed it."""

    def _catalogue(self, prices, currency=None):
        from pi.sources.base import ScrapedProduct, ScrapedVariant

        return [
            ScrapedProduct(
                external_id=f"p{i}", title=f"Thing {i}", url=f"https://s.example/{i}",
                currency=currency,
                variants=[ScrapedVariant(external_id=f"v{i}", price=price)],
            )
            for i, price in enumerate(prices)
        ]

    def test_a_figure_a_thousand_times_the_shop_is_not_a_price(self):
        products = self._catalogue([100.0] * 40 + [333_085_723.0])
        ceilings = pipeline.price_ceilings(products)
        assert ceilings[""] == 100.0 * pipeline.IMPOSSIBLE_MULTIPLE
        assert ceilings[""] < 333_085_723.0

    def test_an_expensive_shop_is_measured_against_itself(self):
        """A €125,000 handbag at a shop whose median is €299 must survive."""
        products = self._catalogue([299.0] * 40 + [125_000.0])
        assert pipeline.price_ceilings(products)[""] >= 125_000.0

    def test_too_small_a_catalogue_says_nothing_about_what_is_normal(self):
        assert pipeline.price_ceilings(self._catalogue([100.0] * 5)) == {}

    def test_each_currency_is_judged_on_its_own_scale(self):
        products = self._catalogue([100.0] * 25, currency="USD")
        products += self._catalogue([1_200_000.0] * 25, currency="KRW")
        ceilings = pipeline.price_ceilings(products)
        assert ceilings["USD"] < ceilings["KRW"], (
            "a shop quoting won must not be capped at a dollar shop's ceiling"
        )

    def test_the_impossible_variant_never_reaches_the_database(self, conn):
        store_id = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
        rates = Rates({"USD": 1.0}, fetched_at=datetime.now(UTC), source="test")
        result = FetchResult(
            domain="shop.example", currency="USD",
            products=self._catalogue([100.0] * 40 + [333_085_723.0]),
        )

        written, _, _ = pipeline.store_result(conn, store_id, result, rates)

        assert written == 40, "the impossible one is dropped, the other forty are kept"
        highest = conn.execute("SELECT MAX(price_native) FROM price_points").fetchone()[0]
        assert highest == 100.0


class TestWhatBelongsOnAShelfButNotInAMessage:
    """A notification interrupts somebody; a page they opened does not."""

    def _with_saving(self, config, saving):
        return replace(
            config, filters=replace(config.filters, min_saving_usd=saving, sizes=("L",))
        )

    def test_the_shelf_asks_less_of_a_saving_than_an_alert_does(self, config):
        shelf = pipeline.shelf_config(self._with_saving(config, 40.0))
        assert shelf.filters.min_saving_usd == pipeline.SHELF_MIN_SAVING_USD
        assert shelf.filters.min_saving_usd < 40.0

    def test_a_stricter_setting_of_your_own_is_not_overruled(self, config):
        """Somebody who asked for $5 gets $5, not the shelf's $10."""
        shelf = pipeline.shelf_config(self._with_saving(config, 5.0))
        assert shelf.filters.min_saving_usd == 5.0

    def test_the_discount_threshold_is_not_touched(self, config):
        shelf = pipeline.shelf_config(self._with_saving(config, 40.0))
        assert shelf.filters.min_discount_pct == config.filters.min_discount_pct


class TestAProductThatStoppedBeingSold:
    """Three steps between "the shop stopped listing it" and "delete it".

    The card leaves the shelf at once, the row is held while the product might
    come back, and only then is it deleted. Live 04.09: allikestore.com's −93%
    Wotherspoon had been a 404 for a fortnight and was still the first tile on
    the page, because nothing in the project ever asked whether a product was
    still for sale.
    """

    @staticmethod
    def _without(payload, index):
        """The same catalogue with one product taken out of it."""
        short = json.loads(json.dumps(payload))
        del short["products"][index]
        return short

    @respx.mock
    async def test_a_full_catalogue_read_marks_what_is_missing_from_it(
        self, config, shopify_payload
    ):
        _mock_rates()
        _mock_telegram()
        _mock_product_pages(shopify_payload)
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn)
        gone = shopify_payload["products"][0]
        (product_id,) = conn.execute(
            "SELECT id FROM products WHERE external_id = ?", (str(gone["id"]),)
        ).fetchone()

        short = self._without(shopify_payload, 0)
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=short)
        )
        _mock_product_pages(short)
        make_due(conn)
        stats = await pipeline.run(config, conn)

        assert stats.withdrawn == 1
        missing = conn.execute(
            "SELECT missing_since FROM products WHERE id = ?", (product_id,)
        ).fetchone()[0]
        assert missing, "a product the shop no longer lists is marked, not left alone"
        assert conn.execute(
            "SELECT COUNT(*) FROM offers WHERE product_id = ?", (product_id,)
        ).fetchone()[0] == 0, "and its card leaves the shelf the same run"

    @respx.mock
    async def test_a_partial_read_marks_nothing(self, config, shopify_payload):
        """Absence from a slice of a catalogue means nothing at all.

        A pass resuming at page three and reaching the end has read the tail of
        a shop, not the shop. Marking everything it did not contain would
        withdraw the first two pages.
        """
        _mock_rates()
        _mock_telegram()
        _mock_product_pages(shopify_payload)
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn)

        # The next run resumes part-way in, and the shop answers with one
        # product — a short page, so the pass ends "complete" without ever
        # having been an enumeration.
        one = {"products": shopify_payload["products"][1:2]}
        respx.get("https://shop.example/products.json?limit=250&page=3").mock(
            return_value=httpx.Response(200, json=one)
        )
        _mock_product_pages(one)
        dbm.upsert_store(conn, "shop.example", sitemap_cursor=3, last_ok=ts(1))
        stats = await pipeline.run(config, conn)

        assert stats.stores_ok == 1, "the shop answered — this is a real pass, not a failure"
        assert stats.products_seen == 1, "and it read the one product page three holds"
        assert stats.withdrawn == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM products WHERE missing_since IS NOT NULL"
        ).fetchone()[0] == 0

    @respx.mock
    async def test_a_product_that_comes_back_is_no_longer_missing(
        self, config, shopify_payload
    ):
        _mock_rates()
        _mock_telegram()
        short = self._without(shopify_payload, 0)
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=short)
        )
        _mock_product_pages(short)
        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn)

        gone = shopify_payload["products"][0]
        product_id = dbm.upsert_product(
            conn, store_id=known_store(conn), external_id=str(gone["id"]),
            title=gone["title"], url=f"https://shop.example/products/{gone['handle']}",
        )
        with dbm.transaction(conn):
            assert dbm.mark_product_missing(conn, product_id, dbm.utcnow())

        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        _mock_product_pages(shopify_payload)
        make_due(conn)
        await pipeline.run(config, conn)

        assert conn.execute(
            "SELECT missing_since FROM products WHERE id = ?", (product_id,)
        ).fetchone()[0] is None, "a restocked product is on sale again, not half deleted"

    def test_the_grace_period_is_honoured(self, conn):
        """Deleted after the grace period, kept before it. This one is final."""
        store_id = dbm.upsert_store(conn, "shop.example", platform="shopify")
        kept, dropped = (
            dbm.upsert_product(
                conn, store_id=store_id, external_id=name, title=name,
                url=f"https://shop.example/products/{name}",
            )
            for name in ("recent", "old")
        )
        with dbm.transaction(conn):
            dbm.mark_product_missing(conn, kept, ts(13))
            dbm.mark_product_missing(conn, dropped, ts(15))

        assert dbm.drop_delisted(conn, grace_days=14) == 1
        left = {row[0] for row in conn.execute("SELECT id FROM products")}
        assert left == {kept}


class TestOpeningACardToSeeIfItIsStillThere:
    """What the free signal cannot cover, `pi verify` asks about directly.

    A shop too large to read in one pass, and every jsonld shop, never produce
    an enumeration — so their cards would sit on the shelf forever on the
    strength of the day they were first seen.
    """

    @respx.mock
    async def _shelf_with_one_card(self, config, shopify_payload):
        _mock_rates()
        _mock_telegram()
        _mock_product_pages(shopify_payload)
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        conn = dbm.connect(config.db_path)
        known_store(conn)
        await pipeline.run(config, conn)
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] > 0
        return conn

    @respx.mock
    async def test_a_dead_page_takes_the_card_off_the_shelf(self, config, shopify_payload):
        conn = await self._shelf_with_one_card(config, shopify_payload)
        _mock_rates()
        _mock_telegram()
        for raw in shopify_payload["products"]:
            respx.get(f"https://shop.example/products/{raw['handle']}.json").mock(
                return_value=httpx.Response(404)
            )

        stats = await pipeline.run(config, conn, collect=False, verify_budget=50)

        assert stats.withdrawn > 0
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM products WHERE missing_since IS NOT NULL"
        ).fetchone()[0] > 0

    @respx.mock
    async def test_a_shop_that_is_merely_down_loses_nothing(self, config, shopify_payload):
        """503 is "ask me later", not "this product is gone".

        The difference is the whole safety of the feature: a shop having a bad
        afternoon must not have its catalogue marked for deletion.
        """
        conn = await self._shelf_with_one_card(config, shopify_payload)
        before = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        _mock_rates()
        _mock_telegram()
        pages = [
            respx.get(f"https://shop.example/products/{raw['handle']}.json").mock(
                return_value=httpx.Response(503)
            )
            for raw in shopify_payload["products"]
        ]

        stats = await pipeline.run(config, conn, collect=False, verify_budget=50)

        assert any(page.called for page in pages), "the cards were asked about"
        assert stats.withdrawn == 0
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM products WHERE missing_since IS NOT NULL"
        ).fetchone()[0] == 0

    @respx.mock
    async def test_a_price_found_by_opening_a_card_is_judged_the_same_run(
        self, config, shopify_payload
    ):
        """Verification writes prices, so verification has to lead to scoring.

        Running it after the scoring step would leave a drop it found sitting
        unnoticed until the price moved a second time.
        """
        conn = await self._shelf_with_one_card(config, shopify_payload)
        _mock_rates()
        photo, _ = _mock_telegram()
        deeper = json.loads(json.dumps(shopify_payload))
        for product in deeper["products"]:
            for variant in product["variants"]:
                variant["price"] = f"{float(variant['price']) / 3:.2f}"
        _mock_product_pages(deeper)

        stats = await pipeline.run(config, conn, collect=False, verify_budget=50)

        assert stats.verified > 0
        assert stats.alerts_sent > 0, "a drop found one card at a time still goes out"
        assert photo.called
