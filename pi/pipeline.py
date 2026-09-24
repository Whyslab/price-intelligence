"""Collect prices, score them, send what is worth sending."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html import escape
from itertools import groupby
from pathlib import Path

import httpx

from . import db as dbm
from . import deals as dealm
from . import landed, personal, reference, taxonomy, tls, watch
from .config import Config
from .domains import same_shop, shop_link
from .fx import Rates, load_rates
from .notify import Telegram, format_caption
from .sources import asos, impersonate, jsonld, shopify
from .sources.base import FetchResult
from .throttle import RateLimiter

log = logging.getLogger(__name__)

# A browser's string with a contact appended. The browser half is not a
# disguise that works — measured: shops answering 403 keep answering 403 to any
# headers at all, and the only thing that gets past them is a TLS fingerprint
# (see pi/sources/impersonate.py). It stays because some servers are picky about
# unfamiliar agents, and the contact is added because a shop that would rather
# not be read needs somewhere to say so. `data/excluded.txt` is where the answer
# goes.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "(+https://github.com/Whyslab/price-intelligence)"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


@dataclass
class RunStats:
    stores_ok: int = 0
    stores_failed: int = 0
    products_seen: int = 0
    points_written: int = 0
    alerts_sent: int = 0
    # Products a shop stopped listing this run, and products a targeted check
    # confirmed are still there. Both are about the shelf being a shelf rather
    # than a graveyard, so both belong in the run's own summary.
    withdrawn: int = 0
    verified: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    # Shops whose full read claimed much of the catalogue had gone and was not
    # believed: (domain, "when · how many of how many").
    held: list[tuple[str, str]] = field(default_factory=list)
    # "Back in stock" and "no longer sold" messages to readers following a product.
    notices_sent: int = 0


def make_client(timeout: float = 30.0, ca_cache: Path | None = None) -> httpx.AsyncClient:
    """The shared HTTP client, trusting any intermediates we have had to fetch.

    Verification is the stock one; `ca_cache` only adds certificates that shops'
    own servers omit from their chains. See pi.tls.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        headers=HEADERS,
        follow_redirects=True,
        verify=tls.context_with(ca_cache),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )


def _last_run_was_capped(conn: sqlite3.Connection) -> bool:
    """Did the previous run leave deals unsent because it hit the cap?

    Read from its own column. While this shared `runs.note` with the blocked
    flag, a run that was both capped and cut off by Shopify — which is every run
    that has ever been capped — recorded only the second fact, so the deals over
    the cap were never reconsidered.
    """
    row = conn.execute(
        "SELECT capped FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return bool(row and row["capped"])


# How long a shop waits its turn, by whether it has ever been worth collecting.
# A shop that produced a notification in the last PRODUCTIVE_DAYS is collected
# every round; the rest once a day. This is what lets the list grow without the
# sweep getting slower for the shops that matter: at 400 shops an even rotation
# would put a full circle 20 hours away and make "hourly" a label rather than a
# fact.
PRODUCTIVE_DAYS = 30
PRODUCTIVE_INTERVAL_HOURS = 1.0
QUIET_INTERVAL_HOURS = 24.0


def _hours(since: str | None, now: datetime) -> float:
    if not since:
        return float("inf")
    return (now - datetime.fromisoformat(since)).total_seconds() / 3600


def _retry_interval(failing_hours: float) -> float:
    """How long a failing shop waits before it is tried again, in hours.

    A quarter of how long it has been failing, between an hour and a day. A
    shop that failed once is asked again next hour, which is what a network
    blip deserves; one that has failed for a week is asked once a day, which
    is what it deserves. Before this, a failing shop was the most overdue shop
    there was — its last success only ever got older — so www.kickz.com,
    lerayonfrais.fr and five others went first in every run for weeks, each
    one a crawl or a slot of the Shopify budget spent on a certain failure.
    """
    return min(QUIET_INTERVAL_HOURS, max(PRODUCTIVE_INTERVAL_HOURS, failing_hours / 4))


def _overdue(store: sqlite3.Row, productive: set[int], now: datetime) -> float:
    """How many of this shop's own intervals have passed since it was collected.

    Overdue-ness rather than plain age, because the two groups are on different
    clocks. A productive shop two hours old is at 2.0 and a quiet shop thirty
    hours old is at 1.25, so the productive one goes first — which is the point.
    Sorting by age alone would let a day's worth of quiet shops crowd out every
    shop that has ever found anything.

    A shop whose last attempt failed is on a third clock: time since that
    attempt, against an interval that grows the longer it has been failing.
    """
    if store["status"] == "error" and store["last_checked"]:
        failing = _hours(store["last_ok"], now)
        waited = _hours(store["last_checked"], now)
        return waited / _retry_interval(failing)
    interval = (
        PRODUCTIVE_INTERVAL_HOURS if store["id"] in productive else QUIET_INTERVAL_HOURS
    )
    if not store["last_ok"]:
        return float("inf")  # never collected: always first in line
    return _hours(store["last_ok"], now) / interval


def due_stores(
    stores: list[sqlite3.Row], productive: set[int], now: datetime | None = None
) -> tuple[list[sqlite3.Row], int]:
    """The shops whose turn it is, most overdue first. Returns (due, not_due)."""
    now = now or datetime.now(UTC)
    scored = sorted(
        ((_overdue(store, productive, now), store) for store in stores),
        key=lambda pair: -pair[0],
    )
    due = [store for score, store in scored if score >= 1.0]
    return due, len(scored) - len(due)


# The slice adapts instead of being a constant, because the quota it is fitting
# into is not one. Measured over a day: a cold IP took all 196 shops once, while
# by the afternoon the same sweep was cut off at twelve. A fixed number is
# therefore either wasteful in the morning or self-defeating after lunch.
BUDGET_FLOOR = 10
BUDGET_STEP = 5
# A block costs more than an under-full run, so the retreat is faster than the
# advance: five shops added per clean run, a third taken off after a block.
BUDGET_RETREAT = 0.66


def _adaptive_budget(conn: sqlite3.Connection, ceiling: int) -> int:
    """How many Shopify shops to attempt, learned from how the last run went."""
    # Only full sweeps count. A `--stores` run visits whatever was named, so its
    # budget is a label rather than a measurement: reading it back taught the
    # next sweep that 45 shops had just gone through fine when nobody had tried.
    row = conn.execute(
        """
        SELECT shopify_budget, blocked FROM runs
         WHERE finished_at IS NOT NULL AND shopify_budget IS NOT NULL
           AND scope = 'sweep'
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return ceiling
    previous = int(row["shopify_budget"])
    if row["blocked"]:
        return max(BUDGET_FLOOR, int(previous * BUDGET_RETREAT))
    return min(ceiling, previous + BUDGET_STEP)


def _record_block(conn: sqlite3.Connection, run_id: int, limiter) -> None:
    """Write down whether Shopify shut this run out, and say so in the log.

    Every exit from a run goes through here, `--collect-only` included. When it
    did not, a collect-only run that had been cut off still recorded `blocked =
    0`, and the next sweep read that as proof the quota was healthy and raised
    its budget on the strength of it.

    Counted off `abandoned`, not `limiter.blocked`: a success late in the sweep
    lifts the block, and a run that had already given up on nineteen stores
    would then report a clean sheet.
    """
    if limiter.abandoned:
        conn.execute("UPDATE runs SET blocked = 1 WHERE id = ?", (run_id,))
        # Warning rather than error, for the same reason as the block itself: the
        # run is recorded as blocked, the next one adapts its budget from that,
        # and the skipped shops are collected then. Degradation that actually
        # needs a person is reported separately by `degradation_notice`.
        log.warning(
            "Shopify blocked this IP part-way through; %d store(s) waited it out and "
            "were skipped, and will be collected on the next run", limiter.abandoned,
        )
    elif limiter.penalties:
        log.warning(
            "individual shops rate limited us %d time(s); lower PI_SHOPIFY_HOST_RATE "
            "if this persists", limiter.penalties,
        )


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
# Taken from the adapter rather than spelled out again — see the note there.
HOPELESS_ERRORS = (jsonld.NO_MARKUP, jsonld.NO_PRODUCT_URLS)


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
) -> FetchResult:
    """Fetch one store with the adapter its platform calls for.

    Both adapters read a slice and say where to resume, so a large catalogue is
    covered over successive runs rather than truncated to whatever one pass got.
    """
    platform = store["platform"]
    if platform == "shopify":
        return await shopify.fetch(
            client, store["domain"], store["currency"],
            limiter=limiter, cursor=store["sitemap_cursor"],
        )
    if platform == "jsonld":
        return await jsonld.fetch(
            client, store["domain"], store["currency"],
            budget=jsonld_budget, cursor=store["sitemap_cursor"],
        )
    if platform == "asos":
        return await asos.fetch(
            client, store["domain"], store["currency"],
            cursor=store["sitemap_cursor"],
        )
    return FetchResult(domain=store["domain"], error=f"no adapter for platform {platform!r}")


# How many product pages one verification pass opens. Its own budget, deliberately
# separate from the collector's: both spend the same per-IP Shopify quota, and a
# check that came out of the collector's allowance would quietly shrink the sweep
# every hour to keep the shelf tidy. Sixty an hour is about 1,400 a day, which
# covers the 731 jsonld offers twice over and takes a steady bite out of the
# large Shopify catalogues that a single pass never finishes.
VERIFY_BUDGET = 60

# Which platforms can answer a question about one product. ASOS is left out: its
# adapter reads sale sections, so there is no per-product page to ask.
VERIFIABLE = ("shopify", "jsonld")


async def _fetch_one(client, row, limiter):
    """Ask one shop about one product, the way its platform allows.

    Split out because three callers need it: `verify_offers`, which walks the
    oldest cards, the check that runs on a find before it is announced, and the
    sample `withdraw_missing` opens before believing a full read.

    Anything unexpected is "unreachable": these run inside the collection loop,
    where one shop's odd answer used to end the whole run — and, since that
    shop was then due again, the next one too.
    """
    try:
        if row["platform"] == "shopify":
            handle = (row["url"] or "").rstrip("/").rsplit("/", 1)[-1]
            if not handle:
                return "skipped", None
            return await shopify.fetch_product(client, row["domain"], handle, limiter)
        return await jsonld.fetch_product(client, row["url"])
    except Exception:
        log.exception("%s: asking about one product failed unexpectedly", row["domain"])
        return "unreachable", None


async def verify_offers(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    rates: Rates,
    limiter: RateLimiter | None = None,
    limit: int = VERIFY_BUDGET,
) -> dict:
    """Open the least recently checked cards on the shelf and see if they exist.

    The free signal — absence from a full catalogue read — covers the shops
    whose catalogue fits in one pass, which is most of them. It cannot cover a
    shop too large to read in one go, because the pass that would have listed
    the product never got that far, and it cannot cover a jsonld shop at all,
    because those are crawled page by page and a failed page is indistinguishable
    from a deleted one. What is left is asked directly, oldest first.

    Everything here is one request per product, so the order is the whole
    design: least recently confirmed first, and among equals the highest score,
    because a dead card at -80% on the first screen does more damage than a dead
    one at -31% on the fifth.
    """
    rows = dbm.stale_offers(conn, limit, VERIFIABLE)
    report = {"checked": 0, "gone": 0, "unreachable": 0, "changed": []}
    for row in rows:
        status, product = await _fetch_one(client, row, limiter)
        if status == "skipped":
            continue
        # Asked, whatever the answer: this is what moves a card that never
        # answers to the back of the queue instead of the front.
        with dbm.transaction(conn):
            dbm.note_attempt(conn, row["product_id"], dbm.utcnow())

        if status == "unreachable":
            report["unreachable"] += 1
            continue
        if status == "gone":
            with dbm.transaction(conn):
                dbm.mark_product_missing(conn, row["product_id"], dbm.utcnow())
            report["gone"] += 1
            log.info("%-38s no longer sells %s", row["domain"], row["url"])
            continue

        with dbm.transaction(conn):
            _, changed, _ = store_result(
                conn,
                row["store_id"],
                FetchResult(
                    domain=row["domain"], products=[product], currency=row["currency"]
                ),
                rates,
            )
            # The one place allowed to say a *product* was checked rather than
            # its shop. Everywhere else that would be a lie, and it was.
            dbm.confirm_offer(conn, row["product_id"], dbm.utcnow())
        report["changed"] += changed
        report["checked"] += 1
    return report


# When a full read is not taken at its word. A shop does lose a few products
# between two reads — measured on ordinary days, a handful to a few dozen —
# but a read claiming more than that is as likely to be a read that stopped
# early, and believing one wrongly deletes a shop's history two weeks later.
# That is not hypothetical: a short page on 22.09.2026 marked 76,062 of
# shop.simon.com's 77,329 products as withdrawn, and every one sampled was
# still for sale. Asked in absolute numbers rather than as a share of the shop:
# a fifth of a 15,000-product shop is 3,000 products, and letting that many
# through unchecked is exactly the damage the check exists to stop.
WITHDRAWAL_ALARM_FLOOR = 25
# How many of the supposedly withdrawn products are opened to check. All of
# them must answer "gone": one still for sale means the read was wrong.
WITHDRAWAL_SAMPLE = 3


def _spread(rows: list, count: int) -> list:
    """`count` rows taken evenly across the list, first included."""
    if len(rows) <= count:
        return list(rows)
    step = len(rows) / count
    return [rows[int(i * step)] for i in range(count)]


async def withdraw_missing(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    store: sqlite3.Row,
    seen: list[int],
    limiter: RateLimiter | None,
) -> tuple[int, str | None]:
    """Mark what a full read of this shop did not list. Returns (marked, held).

    `held` is None normally. When the read would withdraw more than
    WITHDRAWAL_ALARM_FLOOR products, a few of them are opened first, the way
    any such claim should be checked: open some of what was marked and see. If
    any of them is still for sale, nothing is marked, the shop row says why,
    and the daily summary shows it. A genuine clearance still goes through —
    its sampled products answer 404.
    """
    candidates = dbm.withdrawal_candidates(conn, store["id"], seen)
    if not candidates:
        if _col(store, "withdrawal_held"):
            # A read that misses nothing is the clearest all-clear there is.
            with dbm.transaction(conn):
                dbm.upsert_store(conn, store["domain"], withdrawal_held=None)
        return 0, None
    live = dbm.live_products(conn, store["id"])
    if len(candidates) > WITHDRAWAL_ALARM_FLOOR:
        answers = []
        for row in _spread(candidates, WITHDRAWAL_SAMPLE):
            status, _ = await _fetch_one(
                client,
                {"platform": store["platform"], "domain": store["domain"], "url": row["url"]},
                limiter,
            )
            answers.append(status)
        if any(answer != "gone" for answer in answers):
            held = f"{dbm.utcnow()} · {len(candidates)} из {live}"
            with dbm.transaction(conn):
                dbm.upsert_store(conn, store["domain"], withdrawal_held=held)
            log.warning(
                "%-38s a full read left out %d of %d products, but %s — "
                "not marking any of them as withdrawn",
                store["domain"], len(candidates), live,
                ", ".join(answers),
            )
            return 0, held
    with dbm.transaction(conn):
        marked = dbm.mark_missing(conn, store["id"], seen, dbm.utcnow())
        if _col(store, "withdrawal_held"):
            dbm.upsert_store(conn, store["domain"], withdrawal_held=None)
    return marked, None


# A figure this far above everything else the same shop quotes is not a price.
# Measured on the live catalogue: www.stadiumgoods.com published a run of
# t-shirts at 333,085,723 — 165,000 times its own median — and because that was
# the only earlier point those variants had, it became their 30-day floor. The
# shelf then offered a $151 shirt at −100% off $35,684,060, seven such tiles in
# the first screen. 1000× is deliberately far past anything a shop really sells:
# it rejects the 105 impossible points in the database and keeps a €125,000
# handbag at a shop whose median is €299.
IMPOSSIBLE_MULTIPLE = 1000


def price_ceilings(products: list) -> dict[str, float]:
    """Per currency, the figure above which a number cannot be a price here.

    Taken from the fetch itself rather than from the database, because it has
    to hold on a shop's very first pass — that is when the poison arrives, and
    a shop with no history has nothing to compare against yet.
    """
    seen: dict[str, list[float]] = {}
    for product in products:
        currency = (product.currency or "").upper()
        for variant in product.variants:
            if variant.price > 0:
                seen.setdefault(currency, []).append(variant.price)
    return {
        currency: statistics.median(prices) * IMPOSSIBLE_MULTIPLE
        for currency, prices in seen.items()
        if len(prices) >= 20  # too few to say what this shop's prices look like
    }


def store_result(
    conn: sqlite3.Connection,
    store_id: int,
    result: FetchResult,
    rates: Rates,
    restocked: list[int] | None = None,
) -> tuple[int, list[int], list[int]]:
    """Persist one store's catalogue.

    Returns (points_written, changed_variant_ids, product_ids). The product ids
    are what the run classifies afterwards: brand, gender and kind are derived
    from the title, the category and the sizes, so they can only be worked out
    once all three are in the database. Variants back in stock are appended to
    `restocked`, for pi.watch.
    """
    written = 0
    changed: list[int] = []
    touched: list[int] = []
    fallback = (result.currency or "USD").upper()
    ts = dbm.utcnow()
    # Keyed the way the products are: a product that names no currency of its
    # own is measured against the rest of the shop, which is the same bucket.
    ceilings = price_ceilings(result.products)
    impossible = 0
    seen: list[int] = []

    for product in result.products:
        # A product that named its own currency is priced in that one. Only a
        # product that named none falls back to the shop's.
        currency = (product.currency or fallback).upper()
        product_id = dbm.upsert_product(
            conn, store_id, product.external_id, product.title, product.url,
            brand=product.brand, image_url=product.image_url, category=product.category,
        )
        touched.append(product_id)
        # The handles this product can be recognised by in other shops. Written
        # every time because titles and SKUs get edited, and a stale key would
        # quietly match the wrong shoe.
        dbm.set_product_keys(
            conn, product_id,
            reference.keys_for(
                product.brand, product.title, [v.sku for v in product.variants]
            ),
        )
        ceiling = ceilings.get((product.currency or "").upper())
        for variant in product.variants:
            if ceiling is not None and variant.price > ceiling:
                impossible += 1
                continue
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
            # Seen is not the same as changed, and the card claims the first.
            seen.append(variant_id)
            if dbm.record_price(
                conn, variant_id, price_usd, compare_usd, variant.in_stock,
                currency, variant.price, rate, ts=ts,
                compare_at_native=variant.compare_at, restocked=restocked,
            ):
                written += 1
                changed.append(variant_id)
    if touched:
        # Listed just now, which is what a card's «проверено» claims.
        dbm.mark_products_seen(conn, touched, ts)
    if seen:
        dbm.mark_offers_seen(conn, seen, ts or dbm.utcnow())
    if impossible:
        log.warning(
            "%d price(s) discarded as impossible — more than %d times what this "
            "shop usually charges", impossible, IMPOSSIBLE_MULTIPLE,
        )
    return written, changed, touched


CANDIDATES_SQL = """
    SELECT v.id AS variant_id, v.product_id, v.sku, v.size, v.size_norm, v.color,
           p.title, p.brand, p.url, p.image_url, p.store_id,
           p.brand_norm, p.brand_family, p.gender, p.kind, p.audience, p.missing_since,
           s.name AS store_name, s.domain, s.country, s.currency
    FROM pi_candidates c
    JOIN variants v ON v.id = c.id
    JOIN products p ON p.id = v.product_id
    JOIN stores   s ON s.id = p.store_id
"""


def _candidates(
    conn: sqlite3.Connection,
    variant_ids: list[int],
    config: Config,
    watched: set[int] | None = None,
) -> dict[int, sqlite3.Row]:
    """Metadata for every variant worth scoring, in one query, filters applied.

    Asking per variant costs a query each, which is invisible on the few hundred
    that move in a normal run and ruinous on the 2.7 million that `pi seed` and
    `--rescan` look at — and seed is a required step before the first live run.
    """
    conn.execute("DROP TABLE IF EXISTS temp.pi_candidates")
    conn.execute("CREATE TEMP TABLE pi_candidates (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO pi_candidates (id) VALUES (?)",
        ((variant_id,) for variant_id in variant_ids),
    )
    filters = config.filters
    kept: dict[int, sqlite3.Row] = {}
    rejected: list[tuple[int]] = []
    for row in conn.execute(CANDIDATES_SQL):
        # A product the shop has stopped listing cannot be a find, however good
        # its last price was. Belt as well as braces: its variants are not in
        # the changed set either, because nothing fetched them.
        if row["missing_since"]:
            rejected.append((row["variant_id"],))
            continue
        wanted = filters.wants_brand(row["brand"]) and filters.wants_size(row["size_norm"])
        if wanted or (watched and row["product_id"] in watched):
            kept[row["variant_id"]] = row
        else:
            rejected.append((row["variant_id"],))
    if rejected:
        conn.executemany("DELETE FROM pi_candidates WHERE id = ?", rejected)
    return kept


def _histories(conn: sqlite3.Connection) -> Iterator[tuple[int, list[sqlite3.Row]]]:
    """Every candidate's price history, oldest first, in one ordered pass."""
    rows = conn.execute(
        """
        SELECT pp.* FROM price_points pp
        JOIN pi_candidates c ON c.id = pp.variant_id
        ORDER BY pp.variant_id, pp.ts
        """
    )
    for variant_id, points in groupby(rows, key=lambda row: row["variant_id"]):
        yield variant_id, list(points)


def _cap_per_store(
    found: list[tuple[dealm.Deal, sqlite3.Row]], limit: int
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """Keep at most `limit` deals from any one shop, best first.

    A shop running one promotion across its catalogue otherwise takes the whole
    run: the last twelve notifications sent were the same shop at the same -40%.
    """
    if limit <= 0:
        return found
    seen: Counter[str] = Counter()
    kept = []
    for deal, row in found:
        shop = same_shop(row["domain"])
        if seen[shop] >= limit and not deal.watched:
            continue
        seen[shop] += 1
        kept.append((deal, row))
    return kept


@dataclass(frozen=True)
class Watchlist:
    """Что отслеживать: номера моделей и названия. Это разные вопросы.

    Номер — это то, к чему два магазина приходят независимо, и совпадение по
    нему есть совпадение. Название — это поиск: «air force 1 07» найдёт 242
    разных товара. Поэтому они и хранятся врозь, как и в `lookup_article`.
    """

    articles: frozenset[str] = frozenset()
    names: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.articles or self.names)


def read_watchlist(path: Path) -> Watchlist:
    """Что отслеживать вне зависимости от порогов.

    По строке на запись, пустые строки и # — комментарии.

    Строка читается как **номер модели**, если она написана так, как их пишут
    производители: без пробелов и хотя бы с одной цифрой — CW2288-111, M2002RDB.
    Такие сверяются и с извлечёнными артикулами, и с собственными SKU магазинов,
    потому что магазин, кладущий номер производителя в своё поле SKU, — обычное
    дело.

    Всё остальное читается как **название**: «Salomon XT-6», «прострочка».
    Слово без цифр тоже название — `nike` стоит в поле SKU ровно у одного
    магазина, и принять это за номер значило бы отдать один магазин тому, кто
    явно спросил про марку.
    """
    if not path.exists():
        return Watchlist()
    articles: set[str] = set()
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if " " not in entry and any(ch.isdigit() for ch in entry):
            articles.add(entry.upper())
        else:
            names.append(entry)
    return Watchlist(frozenset(articles), tuple(names))


def watched_products(conn: sqlite3.Connection, watch: Watchlist) -> set[int]:
    """Какие товары имеются в виду — в любом магазине.

    Номера сверяются по ключам, названия ищутся тем же запросом, что и ручной
    поиск `pi price`: одна и та же строка должна находить одно и то же, где бы
    её ни набрали.
    """
    if not watch:
        return set()
    found: set[int] = set()

    if watch.articles:
        placeholders = ",".join("?" * len(watch.articles))
        rows = conn.execute(
            f"""
            SELECT product_id FROM product_keys
            WHERE key_type IN ('style', 'sku') AND key IN ({placeholders})
            """,
            sorted(watch.articles),
        )
        found.update(row[0] for row in rows)

    for name in watch.names:
        found.update(dbm.products_by_name(conn, name))
    return found


def _one_alert_per_article(
    found: list[tuple[dealm.Deal, sqlite3.Row]], market: reference.MarketIndex
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """Fold the same article discounted in several shops into one notification.

    Alerts are unique per product, and a product is a row in one shop's
    catalogue — so the same shoe on offer in three shops is three products and
    three messages saying the same thing. Grouped on the manufacturer's article
    number, the reader hears about the shoe once, at the best price found, and
    is told how many other shops were also selling it.

    Only articles other shops actually stock are grouped: `identity` returns
    None for a product nobody else has, and those pass through untouched.
    """
    best: dict[tuple[str, str], int] = {}
    kept: list[tuple[dealm.Deal, sqlite3.Row]] = []
    for deal, row in found:
        key = market.identity(deal.product_id)
        if key is None:
            kept.append((deal, row))
            continue
        at = best.get(key)
        if at is None:
            best[key] = len(kept)
            kept.append((deal, row))
            continue
        winner, winner_row = kept[at]
        # The list arrives sorted by score, so the first one seen is the one to
        # keep; the rest only raise the count.
        kept[at] = (replace(winner, also_in_shops=winner.also_in_shops + 1), winner_row)
    return kept


# What a find has to save before it is worth putting on a shelf somebody chose
# to open. `min_saving_usd` is an interruption threshold: it stops a $4 saving
# arriving as a Telegram notification, which is the right call for a message
# and the wrong one for a page. Measured: at $40 the shelf can draw on 123,737
# discounted variants, at $10 on 163,667 — a third more, almost all of it
# clothing under $40 that is genuinely a third off. ASOS is the extreme case,
# where exactly one variant in 15,855 clears both $40 and 30%.
SHELF_MIN_SAVING_USD = 10.0


def shelf_config(config: Config) -> Config:
    """The same thresholds, with the personal filters taken back out.

    Sizes and brands are facts about a reader, not about a discount, and the
    shelf is read by every reader. Leaving them in makes the whole per-person
    filter meaningless: the shelf would already contain nothing but one person's
    sizes, so asking it for those sizes changes nothing — measured on the live
    database, 7,938 offers became 7,938. It also makes "show me every size", the
    thing the list exists for when buying a present, impossible to answer.

    They still apply to notifications, which are addressed to somebody.
    """
    return replace(
        config,
        filters=replace(
            config.filters,
            sizes=(), brands_allow=(), brands_deny=(),
            min_saving_usd=min(config.filters.min_saving_usd, SHELF_MIN_SAVING_USD),
        ),
    )


def _resolve_indexes(
    conn: sqlite3.Connection,
    market: reference.MarketIndex | None,
    trust: dict[int, reference.Trust] | None,
) -> tuple[reference.MarketIndex, dict[int, reference.Trust]]:
    if market is None:
        market = reference.build_market_index(conn)
    if trust is None:
        trust = reference.store_trust(conn)
        reference.record_trust(conn, trust)
    return market, trust


def score_variants(
    conn: sqlite3.Connection,
    variant_ids: list[int],
    config: Config,
    market: reference.MarketIndex | None = None,
    trust: dict[int, reference.Trust] | None = None,
    watched: set[int] | None = None,
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """Every variant that qualifies as a discount, unfolded and addressed to nobody.

    This is the expensive half of finding deals — reading each variant's price
    history and judging it against a reference — and none of it depends on who
    is going to be told. Separating it from `arrange_for` is what stops a run
    with twenty subscribers reading two million price points twenty times.

    Unfolded on purpose. Which variant of a product is the one worth announcing
    depends on what has already been said to a particular reader, so folding
    here would decide it once for everybody.
    """
    if not variant_ids:
        return []
    watched = watched or set()
    rows = _candidates(conn, variant_ids, config, watched)
    if not rows:
        return []
    market, trust = _resolve_indexes(conn, market, trust)

    scored: list[tuple[dealm.Deal, sqlite3.Row]] = []
    for variant_id, history in _histories(conn):
        row = rows.get(variant_id)
        if row is None:
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
            market=market.look_up(row["product_id"], same_shop(row["domain"])),
            trust=trust.get(row["store_id"]),
            watched=row["product_id"] in watched,
        )
        if deal is not None:
            scored.append((deal, row))
    return scored


def _col(row, name: str):
    """One column, or None when this row simply does not carry it.

    Rows reach `arrange_for` from two queries and from tests that build their
    own, and not all of them select every column. A missing one means "nothing
    is claimed", which is the same answer as NULL and must not be a crash.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def arrange_for(
    conn: sqlite3.Connection,
    scored: list[tuple[dealm.Deal, sqlite3.Row]],
    config: Config,
    market: reference.MarketIndex,
    *,
    user_id: int = 0,
    rank: Callable[[dealm.Deal, sqlite3.Row], float | None] | None = None,
    cap_per_store: bool = True,
    fold_duplicates: bool = True,
    skip_alerted: bool = True,
    kids: bool = False,
    watched: set[int] | None = None,
    per_product: bool = True,
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """One reader's list, out of deals already scored. The cheap half.

    Everything here is a question about the reader rather than about the price:
    what they have already been told, how much they should care, and how much of
    it they can stand in one hour. Run once per subscriber.

    Children's clothing is dropped here rather than at scoring, and the
    distinction matters: scoring feeds the browsable shelf as well, and a shelf
    that cannot be asked to show what it hid has no way to reveal a
    misclassification. So the deal is computed either way and withheld only
    from the list somebody is interrupted with.

    `watched` is this reader's own list — the owner's watchlist file plus what
    they starred. Scoring was told the union of everybody's, because a product
    has to be judged before anyone can be told about it and judging is the
    shared half; which of them belongs to *this* reader is decided here, and the
    deal carries the answer so the notification does not tell somebody they are
    following a thing they never heard of.

    `per_product` keeps one variant per product, which is right for a message —
    the same hoodie in six sizes is one thing to hear about — and wrong for the
    shelf. The shelf is filtered by size against the variant on offer, so a
    shelf holding only each product's best-scoring size answered "nothing in
    EU44" for a shoe discounted in EU44 whenever EU42 had scored a point higher.
    It writes every qualifying size and folds them into one card when it draws
    the page (pi.db.offers_for).
    """
    best_per_product: dict[int, tuple[dealm.Deal, sqlite3.Row]] = {}
    every_variant: list[tuple[dealm.Deal, sqlite3.Row]] = []
    for deal, row in scored:
        follows = bool(watched and deal.product_id in watched)
        if skip_alerted and dealm.already_alerted(
            conn,
            deal,
            user_id,
            dealm.FAVORITE_RE_ALERT_DROP if follows else dealm.RE_ALERT_DROP,
        ):
            continue
        # A watched article still arrives: naming an article is a clearer
        # statement of intent than any classifier's reading of a title.
        if not kids and row["audience"] == "kids" and not follows:
            continue

        if deal.watched != follows:
            deal = replace(deal, watched=follows)
        if not per_product:
            every_variant.append((deal, row))
            continue
        # One notification per product: the same hoodie discounted in six sizes
        # is one thing worth knowing, so keep only its best-scoring variant.
        previous = best_per_product.get(deal.product_id)
        if previous is None or deal.score > previous[0].score:
            best_per_product[deal.product_id] = (deal, row)

    found = list(best_per_product.values()) if per_product else every_variant
    if rank is None:
        found.sort(key=lambda pair: pair[0].score, reverse=True)
    else:
        ranked = ((rank(deal, row), (deal, row)) for deal, row in found)
        found = [
            pair for value, pair in sorted(
                ((value, pair) for value, pair in ranked if value is not None),
                key=lambda item: -item[0],
            )
        ]
    if fold_duplicates:
        found = _one_alert_per_article(found, market)
    if cap_per_store:
        found = _cap_per_store(found, config.filters.max_alerts_per_store)
    return found


def find_deals(
    conn: sqlite3.Connection,
    variant_ids: list[int],
    config: Config,
    market: reference.MarketIndex | None = None,
    trust: dict[int, reference.Trust] | None = None,
    cap_per_store: bool = True,
    fold_duplicates: bool = True,
    skip_alerted: bool = True,
    watched: set[int] | None = None,
    rank: Callable[[dealm.Deal, sqlite3.Row], float | None] | None = None,
    user_id: int = 0,
    kids: bool = False,
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """Score the variants that moved, returning the ones worth announcing.

    Both halves in one call, for everything that has a single answer in mind:
    `pi find`, `pi seed`, and the tests. A sweep with subscribers calls
    `score_variants` once and `arrange_for` per reader instead.

    `cap_per_store` and `fold_duplicates` are what `seed` turns off. Both exist
    so one shop's promotion, or one shoe stocked everywhere, cannot fill a
    notification run — but seeding is not a run. It has to account for every
    qualifying deal, or the ones it trimmed come back as news on the next sweep.

    `skip_alerted` is what `pi find` turns off: a search of what is on offer
    right now should show a deal whether or not it was announced last week.

    `rank` is the second scale — how much *this reader* should care, as opposed
    to how good the discount is. It orders the result and can drop a find below
    that reader's bar by returning None. It runs before folding and capping, so
    the trimming happens on the list the reader would actually be sent. Without
    it the order is the discount's own score, which is what seeding and `pi find`
    want.
    """
    market, trust = _resolve_indexes(conn, market, trust)
    scored = score_variants(conn, variant_ids, config, market, trust, watched)
    return arrange_for(
        conn, scored, config, market, user_id=user_id, rank=rank,
        cap_per_store=cap_per_store, fold_duplicates=fold_duplicates,
        skip_alerted=skip_alerted, kids=kids, watched=watched,
    )


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
    candidates = find_deals(
        conn, all_scorable_variants(conn), config,
        cap_per_store=False, fold_duplicates=False,
    )
    if dry_run:
        return len(candidates)
    ts = dbm.utcnow()
    with dbm.transaction(conn):
        for deal, _ in candidates:
            dealm.record_alert(conn, deal, ts, sent=False)
    return len(candidates)


def caption_for(
    deal: dealm.Deal,
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    shipping: landed.Rules = landed.EMPTY,
    eur_usd: float | None = None,
    since_usd: float | None = None,
) -> str:
    point = dbm.latest_point(conn, deal.variant_id)
    return format_caption(
        deal,
        title=row["title"],
        url=shop_link(row["url"], row["domain"]) or row["url"],
        brand=row["brand"],
        size=row["size"],
        sku=row["sku"],
        store=row["store_name"] or row["domain"],
        country=row["country"],
        native_price=point["price_native"] if point else None,
        currency=point["currency"] if point else "USD",
        landed=landed.landed_all(
            shipping, deal.price_usd, row["kind"], row["domain"],
            row["country"], eur_usd,
        ),
        since_usd=since_usd,
    )


def all_scorable_variants(
    conn: sqlite3.Connection, domains: tuple[str, ...] = ()
) -> list[int]:
    """Every in-stock variant with a recorded price — the --rescan candidate set.

    `domains` narrows it to those shops, for rebuilding one shop's part of the
    shelf without scoring everybody else's.
    """
    shops = ""
    if domains:
        shops = f" AND s.domain IN ({','.join('?' * len(domains))})"
    return [
        row[0]
        for row in conn.execute(
            f"""
            SELECT v.id FROM variants v
            JOIN products p ON p.id = v.product_id
            JOIN stores   s ON s.id = p.store_id
            JOIN price_points pp ON pp.variant_id = v.id
            WHERE pp.ts = (SELECT MAX(ts) FROM price_points WHERE variant_id = v.id)
              AND pp.in_stock = 1
              AND s.last_ok IS NOT NULL
              AND p.missing_since IS NULL{shops}
            """,
            list(domains),
        ).fetchall()
    ]


# How many variants one pass of `reshelve` scores at a time. Scoring keeps a row
# per candidate in memory, and the whole catalogue is 2.7 million of them on a
# machine with 8 GB; a slice keeps the peak to what an hourly run already uses.
RESHELVE_CHUNK = 100_000


def reshelve(
    conn: sqlite3.Connection, config: Config, domains: tuple[str, ...] = ()
) -> tuple[int, int]:
    """Score every priced variant again and rewrite the shelf from it.

    Reads no shop and sends nothing. The shelf is normally kept by the hourly
    run, which only rescores what moved; this is for when the shelf itself is
    wrong — after products a bad read had withdrawn were given back (schema
    16), or after a change to how a discount is judged. Anything on the shelf
    that no longer qualifies, is out of stock, or belongs to a product marked
    as withdrawn is taken off. Returns (written, withdrawn).
    """
    variants = all_scorable_variants(conn, domains)
    shops = ""
    if domains:
        shops = (
            " WHERE product_id IN (SELECT p.id FROM products p JOIN stores s"
            f" ON s.id = p.store_id WHERE s.domain IN ({','.join('?' * len(domains))}))"
        )
    on_shelf = {
        row[0] for row in conn.execute(f"SELECT variant_id FROM offers{shops}", list(domains))
    }
    market = reference.build_market_index(conn)
    trust = reference.store_trust(conn)
    watching = watched_products(conn, read_watchlist(config.watchlist_file))
    followed = dbm.following(conn)
    anyones = watching.union(*followed.values()) if followed else watching
    shelf = shelf_config(config)

    written = withdrawn = 0
    for start in range(0, len(variants), RESHELVE_CHUNK):
        chunk = variants[start : start + RESHELVE_CHUNK]
        scored = score_variants(
            conn, chunk, shelf, market=market, trust=trust, watched=anyones
        )
        on_offer = arrange_for(
            conn, scored, shelf, market, cap_per_store=False, fold_duplicates=False,
            skip_alerted=False, kids=True, per_product=False,
        )
        with dbm.transaction(conn):
            kept, dropped = dbm.record_offers(
                conn, chunk, [deal for deal, _ in on_offer], dbm.utcnow()
            )
        written += kept
        withdrawn += dropped
        log.info("rescored %d of %d variants", min(start + RESHELVE_CHUNK, len(variants)),
                 len(variants))
    # Whatever is on the shelf and was not even a candidate: sold out, or its
    # product marked as withdrawn, or its shop never read. None of it can stand.
    leftover = sorted(on_shelf.difference(variants))
    if leftover:
        with dbm.transaction(conn):
            conn.executemany(
                "DELETE FROM offers WHERE variant_id = ?", ((v,) for v in leftover)
            )
        withdrawn += len(leftover)
    return written, withdrawn


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
    verify_budget: int = VERIFY_BUDGET,
    collect: bool = True,
) -> RunStats:
    """One full sweep: collect, score, notify.

    Only variants whose price moved this run are scored, which is both cheap and
    correct — a deal appears when a price changes. `rescan` scores everything
    instead, for when the filters changed rather than the prices.

    `collect=False` sweeps no store at all and goes straight to opening cards
    one by one. That is `pi verify`, and it goes through this function rather
    than beside it for one reason: a price found by opening a product's own page
    has to be judged, recorded on the shelf and sent by exactly the same code
    that judges a price found by reading a catalogue, or the two would drift.
    """
    stats = RunStats()
    if collect and not rescan and _last_run_was_capped(conn):
        log.info("the previous run hit its alert cap — scoring everything this time")
        rescan = True
    stores = (
        dbm.get_stores(conn, platforms=("shopify", "jsonld", "asos"), domains=domains)
        if collect else []
    )
    # Naming stores explicitly is a deliberate act, so it skips both the queue
    # and the budget: `--stores` means these, now. The budget is still recorded,
    # because the next run adapts from the last recorded one and a hand-run
    # sweep should not look to it like a collapse in capacity.
    budget = config.max_shopify_stores
    if collect and not domains:
        stores, skipped = _drop_hopeless(stores)
        if skipped:
            log.info(
                "skipping %d store(s) that publish no machine-readable prices "
                "(re-check them with --stores)", skipped
            )
        productive = dbm.productive_store_ids(conn, PRODUCTIVE_DAYS)
        stores, waiting = due_stores(stores, productive)
        if waiting:
            log.info(
                "%d store(s) collected recently enough to wait their turn "
                "(%d shop(s) found something in the last %d days and are due hourly)",
                waiting, len(productive), PRODUCTIVE_DAYS,
            )
        budget = _adaptive_budget(conn, budget)
        stores, deferred = _take_shopify_slice(stores, budget)
        if deferred:
            log.info(
                "taking %d Shopify store(s) this run, %d deferred to the next — "
                "the per-IP quota does not stretch to all of them at once",
                budget, deferred,
            )
    if collect and not stores:
        log.warning("no readable stores — run `detect` first")
        return stats

    scope = "verify" if not collect else "stores" if domains else "sweep"
    run_id = conn.execute(
        "INSERT INTO runs (started_at, shopify_budget, scope) VALUES (?, ?, ?)",
        (dbm.utcnow(), budget, scope),
    ).lastrowid

    rates = load_rates(config.db_path.parent / "fx_cache.json")
    log.info("exchange rates: %s (%s)", rates.source, rates.fetched_at.date())
    by_platform = Counter(s["platform"] for s in stores)
    if stores:
        log.info(
            "sweeping %d stores (%s), Shopify budget %.1f req/s overall, %.2f per shop",
            len(stores),
            ", ".join(f"{n} {p}" for p, n in by_platform.most_common()),
            config.shopify_rate, config.shopify_host_rate,
        )
    else:
        log.info("checking %d card(s) one by one, no catalogue read", verify_budget)

    # Separate pools per platform. A jsonld store crawls hundreds of product
    # pages and holds its slot for a minute or more; sharing one pool let those
    # crawls occupy every slot and starve the Shopify stores, which are fast and
    # governed by the rate limiter anyway.
    pools = {
        "shopify": asyncio.Semaphore(config.concurrency),
        "jsonld": asyncio.Semaphore(max(2, config.concurrency // 2)),
        # One shop, read sequentially by its own adapter: a pool of one keeps
        # its forty requests from arriving as forty at once.
        "asos": asyncio.Semaphore(1),
    }
    limiter = RateLimiter(rate=config.shopify_rate, per_host_rate=config.shopify_host_rate)
    changed: list[int] = []
    classified: list[int] = []
    # Sizes that were sold out and are not any more, for whoever follows them.
    restocked: list[int] = []
    # A store being read for the first time has every standing sale look brand
    # new. That first pass is a baseline, not news: record the prices, announce
    # nothing, and let the next run report what actually moved.
    first_sight = {s["id"] for s in stores if not s["last_ok"]}
    if first_sight:
        log.info("%d store(s) seen for the first time — collecting a baseline, not alerting",
                 len(first_sight))

    async with make_client(ca_cache=tls.cache_dir(config.db_path)) as client:

        async def one(store: sqlite3.Row):
            pool = pools.get(store["platform"], pools["jsonld"])
            async with pool:
                try:
                    # A handful of shops answer only a browser's TLS fingerprint.
                    # They get their own client; everyone else shares the pooled one.
                    if store["impersonate"] and impersonate.available():
                        async with impersonate.ImpersonatingClient(timeout=30.0) as browser:
                            return store, await collect_store(
                                browser, store, jsonld_budget, limiter
                            )
                    return store, await collect_store(client, store, jsonld_budget, limiter)
                except Exception as exc:
                    # One shop's surprise is that shop's failure, not the hour's.
                    # Raised out of here it ended the whole run and threw away
                    # every other shop's catalogue with it.
                    log.exception("%s: reading it failed unexpectedly", store["domain"])
                    return store, FetchResult(
                        domain=store["domain"],
                        error=f"crashed: {type(exc).__name__}: {exc}"[:200],
                    )

        # Tasks rather than bare coroutines, so they can be cancelled: when the
        # database refused a write on 20.09, the run died with every other
        # shop's read still in flight, and each of them then failed on a closed
        # client and filled the journal with "Task exception was never retrieved".
        tasks = [asyncio.ensure_future(one(s)) for s in stores]
        try:
            for coro in asyncio.as_completed(tasks):
                store, result = await coro
                if not result.ok:
                    stats.stores_failed += 1
                    stats.failures.append((store["domain"], result.error or "unknown"))
                    if (result.error or "").startswith("skipped:"):
                        # Not asked at all — the platform had shut this IP out.
                        # Recorded as the shop's failure it would be backed off
                        # like a broken shop and its cards left out of the
                        # checking queue, for something the shop never did.
                        dbm.upsert_store(conn, store["domain"], last_error=result.error)
                        log.info("%-38s skipped: %s", store["domain"], result.error)
                        continue
                    dbm.upsert_store(
                        conn, store["domain"], status="error",
                        last_error=result.error, last_checked=dbm.utcnow(),
                    )
                    log.warning("%-38s FAILED: %s", store["domain"], result.error)
                    continue

                with dbm.transaction(conn):
                    written, ids, products = store_result(
                        conn, store["id"], result, rates, restocked
                    )
                # Free, because the crawl already happened: the per-IP quota holds a
                # dozen catalogue reads an hour, not 33,000 product checks. But only
                # after a sanity check when the read claims much of the shop has
                # gone — see withdraw_missing.
                withdrawn = 0
                if result.enumerated:
                    withdrawn, held = await withdraw_missing(
                        conn, client, store, products, limiter
                    )
                    if held:
                        stats.held.append((store["domain"], held))
                if withdrawn:
                    log.info(
                        "%-38s %d product(s) no longer listed", store["domain"], withdrawn
                    )
                    stats.withdrawn += withdrawn
                classified.extend(products)
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
                if result.currency and result.currency in rates:
                    fields["currency"] = result.currency
                elif result.currency:
                    # Every price was already dropped for want of a rate; keeping
                    # the shop's last known currency lets the next pass recover.
                    log.warning("%s: priced in %s, which has no exchange rate",
                                store["domain"], result.currency)
                fields["sitemap_cursor"] = result.next_cursor
                dbm.upsert_store(conn, store["domain"], **fields)
                log.info(
                    "%-38s %4d products, %3d price changes%s",
                    store["domain"], len(result.products), written,
                    "" if result.complete else f" (partial, resuming at {result.next_cursor})",
                )
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # Before scoring, so a deal is judged with the product already known to
        # be a women's shoe rather than an unclassified row.
        if classified:
            taxonomy.classify(conn, classified)

        if collect_only:
            _record_block(conn, run_id, limiter)
            _finish_run(conn, run_id, stats)
            return stats

        # Before scoring rather than after it, so that a price this pass found
        # by opening a product's own page is judged in the same run. At the end
        # it would sit unscored until the price moved again.
        if verify_budget:
            checks = await verify_offers(conn, client, rates, limiter, verify_budget)
            changed.extend(checks["changed"])
            stats.verified += checks["checked"]
            stats.withdrawn += checks["gone"]
            log.info(
                "opened %d card(s) one by one: %d still for sale, %d gone, "
                "%d did not answer",
                checks["checked"] + checks["gone"] + checks["unreachable"],
                checks["checked"], checks["gone"], checks["unreachable"],
            )

        scorable = all_scorable_variants(conn) if rescan else changed
        codes = read_watchlist(config.watchlist_file)
        watching = watched_products(conn, codes)
        if codes:
            log.info("watching %d article(s), matching %d product(s)", len(codes), len(watching))
        # What each reader starred on the shelf, and the union of all of it.
        # The union is what scoring has to be told: a followed product must be
        # judged before anybody can be told about it, and judging happens once
        # for everyone. Who actually hears is decided per reader below.
        followed = dbm.following(conn)
        anyones = watching.union(*followed.values()) if followed else watching
        if followed:
            log.info(
                "%d reader(s) following %d product(s)", len(followed), len(anyones) - len(watching)
            )
        # Two passes over the same variants, because the two questions differ.
        # The notification list is trimmed on purpose — capped per shop, one
        # alert per article, nothing announced twice — while the browsable list
        # is everything still on offer, including what was announced last week.
        # Both share the market and trust indexes, which are what cost anything.
        market = reference.build_market_index(conn)
        trust = reference.store_trust(conn)
        # Scored once. Whether a price is a good price is a fact about the
        # product, identical for every reader, and it is the expensive half —
        # two million price points. What follows per reader is arrangement.
        scored = score_variants(
            conn, scorable, shelf_config(config), market=market, trust=trust,
            watched=anyones,
        )
        # The shelf is written whole, children's clothing included: hiding it
        # is the page's decision (pi.db.offers_for), and a row that was never
        # written cannot be revealed by asking.
        on_offer = arrange_for(
            conn, scored, shelf_config(config), market,
            cap_per_store=False, fold_duplicates=False, skip_alerted=False,
            kids=True, per_product=False,
        )
        written, withdrawn = dbm.record_offers(
            conn, scorable, [deal for deal, _ in on_offer], dbm.utcnow()
        )
        log.info("%d offer(s) on the shelf, %d withdrawn", written, withdrawn)

        # The notification list is the same shelf, ordered and trimmed for the
        # person it is addressed to. Sizes and brands rank it and move its bar
        # rather than cutting it: a shoe in somebody else's size still arrives
        # when it is properly cheap, and a brand nobody named still arrives at
        # all — a hard list fails exactly on what is not in it.
        readers = personal.subscribers(
            conn, config.chat_id, config.filters, config.subscription
        )
        shipping = landed.load_rules()
        eur = rates.to_usd(1.0, "EUR")
        eur_usd = eur[0] if eur else None
        cap = limit if limit is not None else config.filters.max_alerts_per_run
        if len(readers) > 1:
            log.info("%d readers", len(readers))

        queues: list[tuple[personal.Subscriber, list[tuple[dealm.Deal, sqlite3.Row]]]] = []
        # What each reader's starred things cost the last time they were told,
        # so the notification can say what moved rather than only what it is
        # worth against the market.
        since = {r.user_id: dbm.favorite_prices(conn, r.user_id) for r in readers}
        capped_anyone = False
        for reader in readers:
            mine = watching | followed.get(reader.user_id, set())
            candidates = arrange_for(
                conn, scored, shelf_config(config), market, user_id=reader.user_id,
                rank=personal.ranker(
                    reader.reader, config.filters.min_score, shipping, eur_usd,
                    following=followed.get(reader.user_id, set()),
                    # The real filters, not shelf_config's: what reaches a
                    # notification is judged harder than what sits on a page
                    # somebody chose to open.
                    filters=config.filters,
                ),
                watched=mine,
            )
            selected = candidates[:cap]
            overflow = len(candidates) - len(selected)
            log.info(
                "%s: %d deals found, sending %d",
                reader.label, len(candidates), len(selected),
            )
            if overflow:
                # Deals past the cap are not recorded, and the next run only
                # scores variants whose price moved — so without this they would
                # be lost for good rather than merely delayed. Mark the run so
                # the next one reconsiders everything.
                log.warning(
                    "%s: %d deal(s) over the cap of %d were not sent; the next run "
                    "will reconsider them (raise max_alerts_per_run to see them "
                    "sooner)",
                    reader.label, overflow, cap,
                )
                capped_anyone = True
            queues.append((reader, selected))
        if capped_anyone:
            conn.execute("UPDATE runs SET capped = 1 WHERE id = ?", (run_id,))

        # What a price never shows: a followed size back in stock, a followed
        # product the shop took down, and one it put back. See pi.watch.
        returned = watch.returned_notices(conn, readers)
        back_again = {(notice.user_id, notice.product_id) for notice in returned}
        notices = [
            # A product back on sale whose size also came back is one piece
            # of news, not two messages.
            notice for notice in watch.restock_notices(conn, restocked, readers)
            if (notice.user_id, notice.product_id) not in back_again
        ] + returned + watch.gone_notices(conn, readers, config.filters.delisted_grace_days)

        if dry_run:
            for notice in notices:
                print("-" * 60)
                print(f"[{notice.kind} → {notice.chat_id}]")
                print(notice.text)
            for reader, selected in queues:
                if len(queues) > 1:
                    print("=" * 60)
                    print(f"=== {reader.label} ({len(selected)})")
                for deal, row in selected:
                    print("-" * 60)
                    print(caption_for(
                        deal, row, conn, shipping, eur_usd,
                        since_usd=since.get(reader.user_id, {}).get(deal.product_id),
                    ))
                    print(
                        f"[score {deal.score} · image "
                        f"{'yes' if row['image_url'] else 'no'}]"
                    )
            _finish_run(conn, run_id, stats)
            return stats

        if not config.telegram_ready:
            log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — nothing sent")
            _finish_run(conn, run_id, stats)
            return stats

        # Last gate before a message leaves: is this still true?
        queues, confirmed = await confirm_before_announcing(
            conn, client, rates, limiter, queues
        )
        if confirmed["checked"]:
            log.info(
                "confirmed %d find(s) with the shop · %d dropped (%d gone, %d unreachable)",
                confirmed["checked"], confirmed["dropped"],
                confirmed["gone"], confirmed["unreachable"],
            )

        for reader, selected in queues:
            stats.alerts_sent += await _send_to(
                conn, config, client, reader, selected, shipping, eur_usd,
                since.get(reader.user_id, {}),
            )
        stats.notices_sent += await _send_notices(conn, config, client, notices)

    if rates.dropped:
        # Once, at the end, so it covers the cards opened one by one as well.
        log.warning(
            "prices dropped for want of an exchange rate: %s",
            ", ".join(f"{code} ×{n}" for code, n in rates.dropped.most_common()),
        )
    _record_block(conn, run_id, limiter)
    _finish_run(conn, run_id, stats)
    # Last, because the notice is about the run just recorded — including
    # whether it was blocked, which is only known a few lines above this.
    await _warn_if_degraded(conn, config, dry_run=dry_run)
    return stats


# How much higher than announced a price may come back and still be sent. A
# shop rounding its own conversion moves a figure by a fraction of a percent;
# anything past this is the sale being over.
PRICE_TOLERANCE = 0.01


async def confirm_before_announcing(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    rates: dict,
    limiter: RateLimiter | None,
    queues: list[tuple[personal.Subscriber, list]],
) -> tuple[list[tuple[personal.Subscriber, list]], dict]:
    """Ask each shop about the finds about to be announced, and drop what moved.

    The price in a find was read from a catalogue page, and between that read
    and the message arriving the shop may have put it back up. Everything else
    here is careful about whether a discount is real; this is about whether it
    is still real, which is the part a reader checks first by clicking.

    Affordable only because the curation upstream is strict: the bot has 1,373
    offers it may ever interrupt somebody with and sends a handful a day, so
    this is single-figure requests per run rather than thousands.

    Asymmetric on purpose. A price that came back higher is the sale being over
    and the find is dropped. A price that came back lower is the find being
    truer than when it was scored, and it goes out unchanged — re-scoring it
    here would mean re-deciding, in the send path, what the run already decided.

    A shop that cannot be reached says nothing either way, and nothing either
    way is not a confirmation. The find is held rather than sent; no alert is
    recorded for it, so the next run considers it again.
    """
    wanted = sorted({deal.product_id for _, picked in queues for deal, _ in picked})
    report = {"checked": 0, "dropped": 0, "gone": 0, "unreachable": 0}
    if not wanted:
        return queues, report

    rows = {row["product_id"]: row for row in dbm.offers_to_confirm(conn, wanted, VERIFIABLE)}
    verdict: dict[int, bool] = {}
    for product_id in wanted:
        row = rows.get(product_id)
        if row is None:
            # Not a platform that can be asked about one product — ASOS reads
            # sale sections and has no per-product page. Nothing to confirm
            # with, so nothing is claimed: the find goes as it stands.
            verdict[product_id] = True
            continue
        status, product = await _fetch_one(client, row, limiter)
        report["checked"] += 1
        if status == "gone":
            with dbm.transaction(conn):
                dbm.mark_product_missing(conn, product_id, dbm.utcnow())
            report["gone"] += 1
            verdict[product_id] = False
            continue
        if status != "ok" or product is None:
            report["unreachable"] += 1
            verdict[product_id] = False
            continue
        with dbm.transaction(conn):
            store_result(
                conn,
                row["store_id"],
                FetchResult(
                    domain=row["domain"], products=[product], currency=row["currency"]
                ),
                rates,
            )
            dbm.confirm_offer(conn, product_id, dbm.utcnow())
        verdict[product_id] = True

    kept: list[tuple[personal.Subscriber, list]] = []
    for reader, picked in queues:
        survivors = []
        for deal, deal_row in picked:
            if not verdict.get(deal.product_id, True):
                report["dropped"] += 1
                continue
            now = dbm.latest_price_usd(conn, deal.variant_id)
            if now is not None and now > deal.price_usd * (1 + PRICE_TOLERANCE):
                log.info(
                    "%s went from %.2f to %.2f before it could be sent",
                    deal_row["title"][:48], deal.price_usd, now,
                )
                report["dropped"] += 1
                continue
            survivors.append((deal, deal_row))
        if survivors:
            kept.append((reader, survivors))
    return kept, report


async def _send_to(
    conn: sqlite3.Connection,
    config: Config,
    client: httpx.AsyncClient,
    reader: personal.Subscriber,
    selected: list[tuple[dealm.Deal, sqlite3.Row]],
    shipping: landed.Rules,
    eur_usd: float | None,
    since: dict[int, float | None] | None = None,
) -> int:
    """Write one reader's queue to their chat. Returns how many arrived."""
    since = since or {}
    sent = 0
    async with Telegram(config.bot_token, reader.chat_id, client) as telegram:
        for deal, row in selected:
            # Claim the alert first: a crash mid-send must not cause a repeat.
            if not dealm.record_alert(
                conn, deal, dbm.utcnow(), user_id=reader.user_id
            ):
                continue
            if await telegram.send_deal(
                caption_for(
                    deal, row, conn, shipping, eur_usd,
                    since_usd=since.get(deal.product_id),
                ),
                row["image_url"],
            ):
                sent += 1
                if deal.product_id in since:
                    # Next time is measured from this price, not from the one it
                    # was starred at — otherwise a thing that keeps drifting down
                    # reports the same total fall over and over.
                    dbm.record_favorite_price(
                        conn, reader.user_id, deal.product_id, deal.price_usd
                    )
            else:
                conn.execute(
                    "DELETE FROM alerts "
                    " WHERE user_id = ? AND product_id = ? AND price_bucket = ?",
                    (reader.user_id, deal.product_id, deal.bucket),
                )
                if telegram.chat_is_gone:
                    # Not a bad hour, a reader who left. Everything still queued
                    # for them would fail the same way, and so would every run
                    # after this one.
                    log.warning(
                        "%s is no longer reachable (%s) — not writing to them again "
                        "until they talk to the bot",
                        reader.label, telegram.last_error,
                    )
                    personal.deactivate(conn, reader.chat_id)
                    break
            await asyncio.sleep(1.0)  # Telegram allows ~30 messages/second; be calm
    return sent


async def _send_notices(
    conn: sqlite3.Connection,
    config: Config,
    client: httpx.AsyncClient,
    notices: list[watch.Notice],
) -> int:
    """Deliver what pi.watch found, marking each once it has arrived."""
    sent = 0
    for notice in notices:
        async with Telegram(config.bot_token, notice.chat_id, client) as telegram:
            if notice.kind in ("restock", "back"):
                ok = await telegram.send_deal(notice.text, notice.image_url)
            else:
                ok = await telegram.send_text(notice.text)
            if ok:
                sent += 1
                with dbm.transaction(conn):
                    watch.mark_told(conn, notice, dbm.utcnow())
            elif telegram.chat_is_gone:
                personal.deactivate(conn, notice.chat_id)
        await asyncio.sleep(1.0)
    if sent:
        log.info("told %d reader(s) about a followed product coming back or going", sent)
    return sent


async def _warn_if_degraded(conn: sqlite3.Connection, config: Config, dry_run: bool) -> None:
    notice = degradation_notice(conn)
    if not notice:
        return
    log.warning("collection has been reading well under normal for several runs")
    if dry_run or not config.telegram_ready:
        print(notice)
        return
    async with httpx.AsyncClient(timeout=30) as client:  # noqa: SIM117
        async with Telegram(config.bot_token, config.chat_id, client) as telegram:
            await telegram.send_text(notice)


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



# How many consecutive weak runs before saying so, and how far below normal a
# run has to be to count as weak.
DEGRADED_RUNS = 5
DEGRADED_SHARE = 0.5
# The stretch of history "normal" is measured over. Long enough that a bad
# afternoon does not become the new normal and silence the warning.
BASELINE_RUNS = 20


def _weak_runs(conn: sqlite3.Connection, offset: int = 0) -> bool:
    """Have the last DEGRADED_RUNS sweeps all read well under the usual amount?

    Hourly collection makes a single short run meaningless — the quota varies
    through the day and shops go down on their own. A run of them does mean
    something, and it is the case worth interrupting for: a collector that has
    quietly stopped working looks exactly like a market with no discounts, and
    without a signal the difference is noticed a week later.

    `offset` steps the window back one run, which is how the caller tells a
    condition that has just started from one that was already true and has
    already been reported.
    """
    rows = conn.execute(
        """
        SELECT products_seen FROM runs
         WHERE finished_at IS NOT NULL AND scope = 'sweep'
         ORDER BY id DESC LIMIT ? OFFSET ?
        """,
        (DEGRADED_RUNS + BASELINE_RUNS, offset),
    ).fetchall()
    if len(rows) < DEGRADED_RUNS + BASELINE_RUNS:
        return False  # not enough history to know what normal looks like
    recent = [row["products_seen"] for row in rows[:DEGRADED_RUNS]]
    baseline = statistics.median(row["products_seen"] for row in rows[DEGRADED_RUNS:])
    if baseline <= 0:
        return False
    return all(seen < baseline * DEGRADED_SHARE for seen in recent)


def degradation_notice(conn: sqlite3.Connection) -> str | None:
    """A message worth sending, or None. Sent once, when the run of weak runs starts."""
    if not _weak_runs(conn) or _weak_runs(conn, offset=1):
        return None
    rows = conn.execute(
        """
        SELECT products_seen, stores_ok, blocked FROM runs
         WHERE finished_at IS NOT NULL AND scope = 'sweep'
         ORDER BY id DESC LIMIT ?
        """,
        (DEGRADED_RUNS + BASELINE_RUNS,),
    ).fetchall()
    recent = rows[:DEGRADED_RUNS]
    baseline = statistics.median(row["products_seen"] for row in rows[DEGRADED_RUNS:])
    blocked = sum(1 for row in recent if row["blocked"])
    average = statistics.mean(row["products_seen"] for row in recent)
    lines = [
        "⚠️ <b>Сборщик читает заметно меньше обычного</b>",
        "",
        f"Последние {DEGRADED_RUNS} обходов подряд: в среднем "
        f"{average:,.0f} товаров за обход при обычных {baseline:,.0f}.",
    ]
    if blocked:
        lines.append(f"Из них с блокировкой: {blocked}.")
        lines.append("Скорее всего исчерпана квота на IP — обходы сами станут короче.")
    else:
        lines.append(
            "Блокировок нет, значит дело не в квоте: стоит посмотреть "
            "<code>pi health</code> и последние ошибки магазинов."
        )
    return "\n".join(lines)


def _readers_line(conn: sqlite3.Connection) -> str:
    """Readers and money, on one line of the daily summary.

    Counted with the same three-state arithmetic the rest of the project uses
    rather than off `plan`, because a plan with a date in the past is not a
    subscription — and `pi subscriptions` only corrects the column once a day,
    so reading it directly would over-report every morning.
    """
    rows = conn.execute("SELECT * FROM bot_users WHERE active = 1").fetchall()
    states = [dbm.subscription_state(conn, row["id"]) for row in rows]
    # Comps are separated out, or the one number this summary exists to give —
    # how many customers there are — stops meaning that the first time a friend
    # is given access.
    comped = sum(1 for row in rows if dbm.is_comped(row))
    paying = states.count("paid") - comped
    grace = states.count("grace")
    soon = len(dbm.expiring_soon(conn, within_days=7))
    stars = conn.execute(
        "SELECT COALESCE(SUM(stars_paid), 0) FROM bot_users"
    ).fetchone()[0]
    line = (
        f"👥 Читателей: {len(rows)} · платят {paying}"
        + (f" · подарено {comped}" if comped else "")
        + f" · истекает за неделю {soon}"
    )
    if grace:
        line += f" · в отсрочке {grace}"
    broken = dbm.unreadable_dates(conn)
    if broken:
        line += (
            f"\n⚠️ У {broken} читател(я/ей) нечитаемая дата подписки — они "
            "считаются неоплатившими. Поправить: pi grant <id>"
        )
    return line + f"\n⭐ Получено звёзд всего: {stars:,}".replace(",", " ")


def health_report(conn: sqlite3.Connection) -> str:
    """A short HTML summary of how collection is going."""
    counts = dict(
        conn.execute("SELECT platform, COUNT(*) FROM stores GROUP BY platform").fetchall()
    )
    # The last run that actually collected something. A `pi verify` run reads no
    # catalogue at all, so reporting it here would announce "0 stores ok, 0
    # products" — which is exactly what a total collapse looks like.
    last = conn.execute(
        """
        SELECT * FROM runs
         WHERE finished_at IS NOT NULL AND scope IN ('sweep', 'stores')
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    # Compare against a cutoff built the way we store timestamps. SQLite's
    # datetime() yields "2026-08-27 08:25:00" while our rows read
    # "2026-08-27T00:24:19+00:00", and 'T' sorts after ' ', so every row from
    # today counted as being inside the last day whatever its hour.
    a_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    day = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE sent = 1 AND user_id != ? AND ts > ?",
        (dbm.DIGEST_READER, a_day_ago),
    ).fetchone()[0]
    stale = conn.execute(
        """
        SELECT COUNT(*) FROM stores
        WHERE platform = 'shopify'
          AND (last_ok IS NULL OR last_ok < ?)
        """,
        (a_day_ago,),
    ).fetchone()[0]
    shopify_total = conn.execute(
        "SELECT COUNT(*) FROM stores WHERE platform = 'shopify'"
    ).fetchone()[0]
    products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    points = conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0]
    broken = conn.execute(
        """
        SELECT domain, last_error FROM stores
        WHERE status = 'error' AND platform IN ('shopify', 'jsonld', 'asos')
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
        if last["blocked"]:
            lines.append("⚠️ Обход прерван: Shopify заблокировал IP. Остальные магазины — в следующий раз.")
        if last["capped"]:
            lines.append("ℹ️ Уведомлений было больше лимита; следующий обход пришлёт остальные.")
    # Runs that started and never finished: a traceback or a timeout. The
    # summary is where the owner looks, and until this nothing in it said so.
    # Only runs older than an hour, so the one going right now is not counted.
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    died, why = conn.execute(
        """
        SELECT COUNT(*), MAX(note) FROM runs
         WHERE finished_at IS NULL AND started_at > ? AND started_at < ?
        """,
        (a_day_ago, an_hour_ago),
    ).fetchone()
    if died:
        lines.append(
            f"⚠️ Прогонов, не дошедших до конца, за сутки: {died}"
            + (f" — {escape(why[:120])}" if why else "")
        )
    held = conn.execute(
        "SELECT domain, withdrawal_held FROM stores WHERE withdrawal_held IS NOT NULL"
        " ORDER BY domain LIMIT 5"
    ).fetchall()
    if held:
        lines.append("⚠️ Не поверил полному чтению (магазин будто потерял большую часть "
                     "каталога, но проверка нашла товары в продаже):")
        lines += [f"  • {d}: {escape(note)}" for d, note in held]
    fresh, shown = dbm.shelf_freshness(conn)
    freshness = (
        f"Полка: {shown:,} предложений, подтверждены за 48 ч — {fresh / shown:.0%}"
        + (" ⚠️ цель ≥95%" if fresh < 0.95 * shown else "")
        if shown else "Полка пуста"
    )
    lines += [
        f"Уведомлений за сутки: {day}",
        freshness,
        "",
        _readers_line(conn),
        "",
        f"Shopify не обновлялись сутки: {stale} из {shopify_total}",
        f"В базе: {products:,} товаров, {points:,} точек истории",
        "",
        "<b>Магазины по платформам:</b>",
    ]
    labels = {
        "shopify": "Shopify (полный API)",
        "jsonld": "schema.org (по карточкам)",
        "asos": "собственный API магазина",
        "blocked": "закрыты анти-ботом",
        "tls": "битый TLS-сертификат",
        "dead": "не отвечают",
        "unknown": "без структурных данных",
        "new": "ещё не проверены",
    }
    for platform, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  • {labels.get(platform, platform)}: {count}")

    by_rule = conn.execute(
        """
        SELECT domain, tag_share, round_share FROM stores
        WHERE tag_share > 0.15 AND round_share >= 0.9
        ORDER BY tag_share DESC LIMIT 8
        """
    ).fetchall()
    if by_rule:
        lines += ["", "<b>Считают скидки по правилу (ярлыкам не верим):</b>"]
        lines += [
            f"  • {d}: скидка на {tag:.0%} каталога, {rnd:.0%} из них — круглые"
            for d, tag, rnd in by_rule
        ]

    if broken:
        lines += ["", "<b>Сломались на последнем обходе:</b>"]
        lines += [f"  • {d}: {(e or '')[:60]}" for d, e in broken]
    return "\n".join(lines)
