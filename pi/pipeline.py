"""Collect prices, score them, send what is worth sending."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

import httpx

from . import db as dbm
from . import deals as dealm
from .config import Config
from .fx import Rates, load_rates
from .notify import Telegram, format_caption
from .sources import jsonld, shopify
from .sources.base import FetchResult
from .throttle import RateLimiter

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


@dataclass
class RunStats:
    stores_ok: int = 0
    stores_failed: int = 0
    products_seen: int = 0
    points_written: int = 0
    alerts_sent: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers=HEADERS,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )


def _last_run_was_capped(conn: sqlite3.Connection) -> bool:
    """Did the previous run leave deals unsent because it hit the cap?"""
    row = conn.execute(
        "SELECT note FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return bool(row and row["note"] == "capped")


def _take_shopify_slice(
    stores: list[sqlite3.Row], budget: int
) -> tuple[list[sqlite3.Row], int]:
    """Keep the first `budget` Shopify stores; jsonld stores are unaffected.

    Shopify's per-IP quota tolerates a few dozen stores at a time — measured at
    twelve to forty depending on how much the IP has already been used today.
    Charging at all 138 just means being cut off part-way, so a run takes a
    slice. Because `get_stores` orders by least-recently-collected, successive
    runs cover the whole list instead of re-collecting the same shops.
    """
    if budget <= 0:
        return stores, 0
    kept, seen_shopify = [], 0
    for store in stores:
        if store["platform"] != "shopify":
            kept.append(store)
            continue
        seen_shopify += 1
        if seen_shopify <= budget:
            kept.append(store)
    return kept, max(0, seen_shopify - budget)


# A store's own markup is a durable fact about it, unlike a network hiccup.
HOPELESS_ERRORS = ("no schema.org/Product markup found", "no product URLs in sitemap")


def _drop_hopeless(stores: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], int]:
    """Set aside stores that have never once yielded a product and said why.

    Crawling 200 product pages every six hours at a shop that publishes no
    structured data costs thousands of pointless requests and finds nothing.
    A store is only dropped if it has never succeeded, so one bad sweep cannot
    retire a working shop; naming it in --stores brings it back.
    """
    keep, dropped = [], 0
    for store in stores:
        never_worked = not store["last_ok"]
        hopeless = (store["last_error"] or "") in HOPELESS_ERRORS
        if never_worked and hopeless:
            dropped += 1
        else:
            keep.append(store)
    return keep, dropped


async def collect_store(
    client: httpx.AsyncClient, store: sqlite3.Row, jsonld_budget: int, limiter: RateLimiter
) -> tuple[FetchResult, int | None]:
    """Fetch one store with the adapter its platform calls for."""
    platform = store["platform"]
    if platform == "shopify":
        return await shopify.fetch(
            client, store["domain"], store["currency"], limiter=limiter
        ), None
    if platform == "jsonld":
        result, cursor = await jsonld.fetch(
            client, store["domain"], store["currency"],
            budget=jsonld_budget, cursor=store["sitemap_cursor"],
        )
        return result, cursor
    return FetchResult(domain=store["domain"], error=f"no adapter for platform {platform!r}"), None


def store_result(
    conn: sqlite3.Connection, store_id: int, result: FetchResult, rates: Rates
) -> tuple[int, list[int]]:
    """Persist one store's catalogue. Returns (points_written, changed_variant_ids)."""
    written = 0
    changed: list[int] = []
    currency = (result.currency or "USD").upper()
    ts = dbm.utcnow()

    for product in result.products:
        product_id = dbm.upsert_product(
            conn, store_id, product.external_id, product.title, product.url,
            brand=product.brand, image_url=product.image_url, category=product.category,
        )
        for variant in product.variants:
            converted = rates.to_usd(variant.price, currency)
            if converted is None:
                continue  # unknown currency: drop the price rather than invent one
            price_usd, rate = converted
            compare_usd = None
            if variant.compare_at:
                compare = rates.to_usd(variant.compare_at, currency)
                compare_usd = compare[0] if compare else None

            variant_id = dbm.upsert_variant(
                conn, product_id, variant.external_id, sku=variant.sku,
                size=variant.size, size_norm=variant.size_norm, color=variant.color,
            )
            if dbm.record_price(
                conn, variant_id, price_usd, compare_usd, variant.in_stock,
                currency, variant.price, rate, ts=ts,
            ):
                written += 1
                changed.append(variant_id)
    return written, changed


def find_deals(
    conn: sqlite3.Connection, variant_ids: list[int], config: Config
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """Score the variants that moved, returning the ones worth announcing."""
    best_per_product: dict[int, tuple[dealm.Deal, sqlite3.Row]] = {}
    for variant_id in variant_ids:
        row = conn.execute(
            """
            SELECT v.id AS variant_id, v.product_id, v.sku, v.size, v.size_norm, v.color,
                   p.title, p.brand, p.url, p.image_url,
                   s.name AS store_name, s.domain, s.country, s.currency
            FROM variants v
            JOIN products p ON p.id = v.product_id
            JOIN stores   s ON s.id = p.store_id
            WHERE v.id = ?
            """,
            (variant_id,),
        ).fetchone()
        if row is None:
            continue
        if not config.filters.wants_brand(row["brand"]):
            continue
        if not config.filters.wants_size(row["size_norm"]):
            continue

        history = dbm.price_history(conn, variant_id)
        if not history:
            continue
        current = history[-1]
        deal = dealm.evaluate(
            variant_id=variant_id,
            product_id=row["product_id"],
            price_usd=current["price_usd"],
            compare_at_usd=current["compare_at_usd"],
            in_stock=bool(current["in_stock"]),
            history=history,
            filters=config.filters,
        )
        if deal is None or dealm.already_alerted(conn, deal):
            continue
        # One notification per product: the same hoodie discounted in six sizes
        # is one thing worth knowing, so keep only its best-scoring variant.
        previous = best_per_product.get(deal.product_id)
        if previous is None or deal.score > previous[0].score:
            best_per_product[deal.product_id] = (deal, row)

    found = list(best_per_product.values())
    found.sort(key=lambda pair: pair[0].score, reverse=True)
    return found


def seed_alerts(conn: sqlite3.Connection, config: Config, dry_run: bool = False) -> int:
    """Mark every discount that currently qualifies as already announced.

    A shop's standing sales are not news. The per-store baseline rule handles
    this for a shop being read for the first time, but a database that has been
    collected for a while — or rebuilt, or had its thresholds lowered — can hold
    tens of thousands of qualifying discounts that were never sent. Draining
    those at max_alerts_per_run would mean months of notifications about sales
    that started before the bot existed.

    Seeding records them without sending, so only price drops from here on are
    announced. Returns how many were suppressed.
    """
    candidates = find_deals(conn, all_scorable_variants(conn), config)
    if dry_run:
        return len(candidates)
    ts = dbm.utcnow()
    with dbm.transaction(conn):
        for deal, _ in candidates:
            dealm.record_alert(conn, deal, ts)
    return len(candidates)


def caption_for(deal: dealm.Deal, row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    point = dbm.latest_point(conn, deal.variant_id)
    return format_caption(
        deal,
        title=row["title"],
        url=row["url"],
        brand=row["brand"],
        size=row["size"],
        sku=row["sku"],
        store=row["store_name"] or row["domain"],
        country=row["country"],
        native_price=point["price_native"] if point else None,
        currency=point["currency"] if point else "USD",
    )


def all_scorable_variants(conn: sqlite3.Connection) -> list[int]:
    """Every in-stock variant with a recorded price — the --rescan candidate set."""
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT v.id FROM variants v
            JOIN products p ON p.id = v.product_id
            JOIN stores   s ON s.id = p.store_id
            JOIN price_points pp ON pp.variant_id = v.id
            WHERE pp.ts = (SELECT MAX(ts) FROM price_points WHERE variant_id = v.id)
              AND pp.in_stock = 1
              AND s.last_ok IS NOT NULL
            """
        ).fetchall()
    ]


async def run(
    config: Config,
    conn: sqlite3.Connection,
    *,
    domains: tuple[str, ...] = (),
    collect_only: bool = False,
    dry_run: bool = False,
    rescan: bool = False,
    limit: int | None = None,
    jsonld_budget: int = jsonld.DEFAULT_BUDGET,
) -> RunStats:
    """One full sweep: collect, score, notify.

    Only variants whose price moved this run are scored, which is both cheap and
    correct — a deal appears when a price changes. `rescan` scores everything
    instead, for when the filters changed rather than the prices.
    """
    stats = RunStats()
    if not rescan and _last_run_was_capped(conn):
        log.info("the previous run hit its alert cap — scoring everything this time")
        rescan = True
    stores = dbm.get_stores(conn, platforms=("shopify", "jsonld"), domains=domains)
    if not domains:
        stores, skipped = _drop_hopeless(stores)
        if skipped:
            log.info(
                "skipping %d store(s) that publish no machine-readable prices "
                "(re-check them with --stores)", skipped
            )
        stores, deferred = _take_shopify_slice(stores, config.max_shopify_stores)
        if deferred:
            log.info(
                "taking %d Shopify store(s) this run, %d deferred to the next — "
                "the per-IP quota does not stretch to all of them at once",
                config.max_shopify_stores, deferred,
            )
    if not stores:
        log.warning("no readable stores — run `detect` first")
        return stats

    run_id = conn.execute(
        "INSERT INTO runs (started_at) VALUES (?)", (dbm.utcnow(),)
    ).lastrowid

    rates = load_rates(config.db_path.parent / "fx_cache.json")
    log.info("exchange rates: %s (%s)", rates.source, rates.fetched_at.date())
    by_platform = Counter(s["platform"] for s in stores)
    log.info(
        "sweeping %d stores (%s), Shopify budget %.1f req/s overall, %.2f per shop",
        len(stores),
        ", ".join(f"{n} {p}" for p, n in by_platform.most_common()),
        config.shopify_rate, config.shopify_host_rate,
    )

    # Separate pools per platform. A jsonld store crawls hundreds of product
    # pages and holds its slot for a minute or more; sharing one pool let those
    # crawls occupy every slot and starve the Shopify stores, which are fast and
    # governed by the rate limiter anyway.
    pools = {
        "shopify": asyncio.Semaphore(config.concurrency),
        "jsonld": asyncio.Semaphore(max(2, config.concurrency // 2)),
    }
    limiter = RateLimiter(rate=config.shopify_rate, per_host_rate=config.shopify_host_rate)
    changed: list[int] = []
    # A store being read for the first time has every standing sale look brand
    # new. That first pass is a baseline, not news: record the prices, announce
    # nothing, and let the next run report what actually moved.
    first_sight = {s["id"] for s in stores if not s["last_ok"]}
    if first_sight:
        log.info("%d store(s) seen for the first time — collecting a baseline, not alerting",
                 len(first_sight))

    async with make_client() as client:

        async def one(store: sqlite3.Row):
            pool = pools.get(store["platform"], pools["jsonld"])
            async with pool:
                return store, await collect_store(client, store, jsonld_budget, limiter)

        for coro in asyncio.as_completed([one(s) for s in stores]):
            store, (result, cursor) = await coro
            if not result.ok:
                stats.stores_failed += 1
                stats.failures.append((store["domain"], result.error or "unknown"))
                dbm.upsert_store(
                    conn, store["domain"], status="error",
                    last_error=result.error, last_checked=dbm.utcnow(),
                )
                log.warning("%-38s FAILED: %s", store["domain"], result.error)
                continue

            with dbm.transaction(conn):
                written, ids = store_result(conn, store["id"], result, rates)
            if store["id"] not in first_sight:
                changed.extend(ids)
            stats.stores_ok += 1
            stats.products_seen += len(result.products)
            stats.points_written += written

            fields = {
                "status": "ok",
                "last_checked": dbm.utcnow(),
                "last_ok": dbm.utcnow(),
                "last_error": None,
                "product_count": len(result.products),
            }
            if result.currency:
                fields["currency"] = result.currency
            if cursor is not None:
                fields["sitemap_cursor"] = cursor
            dbm.upsert_store(conn, store["domain"], **fields)
            log.info(
                "%-38s %4d products, %3d price changes", store["domain"], len(result.products), written
            )

        if collect_only:
            _finish_run(conn, run_id, stats)
            return stats

        scorable = all_scorable_variants(conn) if rescan else changed
        candidates = find_deals(conn, scorable, config)
        cap = limit if limit is not None else config.filters.max_alerts_per_run
        selected = candidates[:cap]
        overflow = len(candidates) - len(selected)
        log.info("%d deals found, sending %d", len(candidates), len(selected))
        if overflow:
            # Deals past the cap are not recorded, and the next run only scores
            # variants whose price moved — so without this they would be lost
            # for good rather than merely delayed. Mark the run so the next one
            # reconsiders everything.
            log.warning(
                "%d deal(s) over the cap of %d were not sent; the next run will "
                "reconsider them (raise max_alerts_per_run to see them sooner)",
                overflow, cap,
            )
            conn.execute("UPDATE runs SET note = 'capped' WHERE id = ?", (run_id,))

        if dry_run:
            for deal, row in selected:
                print("-" * 60)
                print(caption_for(deal, row, conn))
                print(f"[score {deal.score} · image {'yes' if row['image_url'] else 'no'}]")
            _finish_run(conn, run_id, stats)
            return stats

        if not config.telegram_ready:
            log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — nothing sent")
            _finish_run(conn, run_id, stats)
            return stats

        async with Telegram(config.bot_token, config.chat_id, client) as telegram:
            for deal, row in selected:
                # Claim the alert first: a crash mid-send must not cause a repeat.
                if not dealm.record_alert(conn, deal, dbm.utcnow()):
                    continue
                if await telegram.send_deal(caption_for(deal, row, conn), row["image_url"]):
                    stats.alerts_sent += 1
                else:
                    conn.execute(
                        "DELETE FROM alerts WHERE product_id = ? AND price_bucket = ?",
                        (deal.product_id, deal.bucket),
                    )
                await asyncio.sleep(1.0)  # Telegram allows ~30 messages/second; be calm

    if limiter.blocked:
        log.error(
            "Shopify blocked this IP part-way through; %d store(s) were skipped and "
            "will be collected on the next run", stats.stores_failed,
        )
    elif limiter.penalties:
        log.warning(
            "individual shops rate limited us %d time(s); lower PI_SHOPIFY_HOST_RATE "
            "if this persists", limiter.penalties,
        )
    _finish_run(conn, run_id, stats)
    return stats


def _finish_run(conn: sqlite3.Connection, run_id: int, stats: RunStats) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, stores_ok = ?, stores_failed = ?,
                        products_seen = ?, points_written = ?, alerts_sent = ?
        WHERE id = ?
        """,
        (
            dbm.utcnow(), stats.stores_ok, stats.stores_failed,
            stats.products_seen, stats.points_written, stats.alerts_sent, run_id,
        ),
    )


def health_report(conn: sqlite3.Connection) -> str:
    """A short HTML summary of how collection is going."""
    counts = dict(
        conn.execute("SELECT platform, COUNT(*) FROM stores GROUP BY platform").fetchall()
    )
    last = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    day = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE ts > datetime('now', '-1 day')"
    ).fetchone()[0]
    products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    points = conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0]
    broken = conn.execute(
        """
        SELECT domain, last_error FROM stores
        WHERE status = 'error' AND platform IN ('shopify', 'jsonld')
        ORDER BY domain LIMIT 10
        """
    ).fetchall()

    lines = ["📊 <b>Price Intelligence — сводка</b>", ""]
    if last:
        # Say when, because this is the last *finished* run: if one is in
        # progress, or the timer has not fired for days, the figures below are
        # older than they look.
        when = (last["finished_at"] or "")[:16].replace("T", " ")
        lines += [
            f"Последний обход ({when} UTC): {last['stores_ok']} магазинов ок, "
            f"{last['stores_failed']} с ошибкой",
            f"Товаров просмотрено: {last['products_seen']:,}",
            f"Изменений цен: {last['points_written']:,}",
        ]
    lines += [
        f"Алертов за сутки: {day}",
        "",
        f"В базе: {products:,} товаров, {points:,} точек истории",
        "",
        "<b>Магазины по платформам:</b>",
    ]
    labels = {
        "shopify": "Shopify (полный API)",
        "jsonld": "schema.org (по карточкам)",
        "blocked": "закрыты анти-ботом",
        "tls": "битый TLS-сертификат",
        "dead": "не отвечают",
        "unknown": "без структурных данных",
        "new": "ещё не проверены",
    }
    for platform, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  • {labels.get(platform, platform)}: {count}")

    if broken:
        lines += ["", "<b>Сломались на последнем обходе:</b>"]
        lines += [f"  • {d}: {(e or '')[:60]}" for d, e in broken]
    return "\n".join(lines)
