"""End-to-end: a catalogue goes in, a Telegram photo comes out."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx

from pi import db as dbm
from pi import deals as dealm
from pi import pipeline
from pi.config import Config, Filters

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


def _mock_rates():
    respx.get("https://api.frankfurter.dev/v1/latest").mock(
        return_value=httpx.Response(200, json={"base": "USD", "rates": {"GBP": 0.73, "EUR": 0.86}})
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
    photo, _ = _mock_telegram()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    conn = dbm.connect(config.db_path)
    known_store(conn)

    first = await pipeline.run(config, conn)
    assert first.alerts_sent > 0
    sent_first_time = photo.call_count

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
async def test_brand_filter_keeps_unwanted_deals_quiet(config, shopify_payload):
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
    assert stats.alerts_sent == 0, "but nothing matches the brand filter"
    assert not photo.called and not text.called


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

    conn = dbm.connect(config.db_path)
    known_store(conn)

    first = await pipeline.run(config, conn, limit=1)
    assert first.alerts_sent == 1
    assert conn.execute("SELECT capped FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == 1

    # Nothing about the catalogue changes, so a run that only looked at moved
    # prices would send nothing at all.
    second = await pipeline.run(config, conn, limit=1)
    assert second.alerts_sent == 1, "the deferred deal goes out next time"

    third = await pipeline.run(config, conn, limit=1)
    assert third.alerts_sent == 1

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
