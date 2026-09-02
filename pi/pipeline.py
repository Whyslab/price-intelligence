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
from itertools import groupby
from pathlib import Path

import httpx

from . import db as dbm
from . import deals as dealm
from . import landed, personal, reference, taxonomy, tls
from .config import Config
from .domains import same_shop
from .fx import Rates, load_rates
from .notify import Telegram, format_caption
from .sources import asos, impersonate, jsonld, shopify
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


def _overdue(store: sqlite3.Row, productive: set[int], now: datetime) -> float:
    """How many of this shop's own intervals have passed since it was collected.

    Overdue-ness rather than plain age, because the two groups are on different
    clocks. A productive shop two hours old is at 2.0 and a quiet shop thirty
    hours old is at 1.25, so the productive one goes first — which is the point.
    Sorting by age alone would let a day's worth of quiet shops crowd out every
    shop that has ever found anything.
    """
    interval = (
        PRODUCTIVE_INTERVAL_HOURS if store["id"] in productive else QUIET_INTERVAL_HOURS
    )
    if not store["last_ok"]:
        return float("inf")  # never collected: always first in line
    age = (now - datetime.fromisoformat(store["last_ok"])).total_seconds() / 3600
    return age / interval


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


def store_result(
    conn: sqlite3.Connection, store_id: int, result: FetchResult, rates: Rates
) -> tuple[int, list[int], list[int]]:
    """Persist one store's catalogue.

    Returns (points_written, changed_variant_ids, product_ids). The product ids
    are what the run classifies afterwards: brand, gender and kind are derived
    from the title, the category and the sizes, so they can only be worked out
    once all three are in the database.
    """
    written = 0
    changed: list[int] = []
    touched: list[int] = []
    fallback = (result.currency or "USD").upper()
    ts = dbm.utcnow()

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
                compare_at_native=variant.compare_at,
            ):
                written += 1
                changed.append(variant_id)
    return written, changed, touched


CANDIDATES_SQL = """
    SELECT v.id AS variant_id, v.product_id, v.sku, v.size, v.size_norm, v.color,
           p.title, p.brand, p.url, p.image_url, p.store_id,
           p.brand_norm, p.brand_family, p.gender, p.kind,
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


def read_watchlist(path: Path) -> set[str]:
    """Article numbers to be told about regardless of the thresholds.

    One per line, blank lines and # comments ignored. Written as the
    manufacturer writes them — CW2288-111, M2002RDB — and matched against both
    the article numbers we extract and the shops' own SKUs, because a shop that
    uses the manufacturer's number as its SKU is the common case.
    """
    if not path.exists():
        return set()
    codes = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if code:
            codes.add(code)
    return codes


def watched_products(conn: sqlite3.Connection, codes: set[str]) -> set[int]:
    """Which products those article numbers refer to, in any shop."""
    if not codes:
        return set()
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"""
        SELECT product_id FROM product_keys
        WHERE key_type IN ('style', 'sku') AND key IN ({placeholders})
        """,
        sorted(codes),
    )
    return {row[0] for row in rows}


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
        filters=replace(config.filters, sizes=(), brands_allow=(), brands_deny=()),
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
) -> list[tuple[dealm.Deal, sqlite3.Row]]:
    """One reader's list, out of deals already scored. The cheap half.

    Everything here is a question about the reader rather than about the price:
    what they have already been told, how much they should care, and how much of
    it they can stand in one hour. Run once per subscriber.
    """
    best_per_product: dict[int, tuple[dealm.Deal, sqlite3.Row]] = {}
    for deal, row in scored:
        if skip_alerted and dealm.already_alerted(conn, deal, user_id):
            continue
        # One notification per product: the same hoodie discounted in six sizes
        # is one thing worth knowing, so keep only its best-scoring variant.
        previous = best_per_product.get(deal.product_id)
        if previous is None or deal.score > previous[0].score:
            best_per_product[deal.product_id] = (deal, row)

    found = list(best_per_product.values())
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
        skip_alerted=skip_alerted,
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
) -> str:
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
        landed=landed.landed_all(
            shipping, deal.price_usd, row["kind"], row["domain"],
            row["country"], eur_usd,
        ),
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
    stores = dbm.get_stores(conn, platforms=("shopify", "jsonld", "asos"), domains=domains)
    # Naming stores explicitly is a deliberate act, so it skips both the queue
    # and the budget: `--stores` means these, now. The budget is still recorded,
    # because the next run adapts from the last recorded one and a hand-run
    # sweep should not look to it like a collapse in capacity.
    budget = config.max_shopify_stores
    if not domains:
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
    if not stores:
        log.warning("no readable stores — run `detect` first")
        return stats

    run_id = conn.execute(
        "INSERT INTO runs (started_at, shopify_budget, scope) VALUES (?, ?, ?)",
        (dbm.utcnow(), budget, "stores" if domains else "sweep"),
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
        # One shop, read sequentially by its own adapter: a pool of one keeps
        # its forty requests from arriving as forty at once.
        "asos": asyncio.Semaphore(1),
    }
    limiter = RateLimiter(rate=config.shopify_rate, per_host_rate=config.shopify_host_rate)
    changed: list[int] = []
    classified: list[int] = []
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
                # A handful of shops answer only a browser's TLS fingerprint.
                # They get their own client; everyone else shares the pooled one.
                if store["impersonate"] and impersonate.available():
                    async with impersonate.ImpersonatingClient(
                        timeout=30.0, headers=HEADERS
                    ) as browser:
                        return store, await collect_store(
                            browser, store, jsonld_budget, limiter
                        )
                return store, await collect_store(client, store, jsonld_budget, limiter)

        for coro in asyncio.as_completed([one(s) for s in stores]):
            store, result = await coro
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
                written, ids, products = store_result(conn, store["id"], result, rates)
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
            if result.currency:
                fields["currency"] = result.currency
            fields["sitemap_cursor"] = result.next_cursor
            dbm.upsert_store(conn, store["domain"], **fields)
            log.info(
                "%-38s %4d products, %3d price changes%s",
                store["domain"], len(result.products), written,
                "" if result.complete else f" (partial, resuming at {result.next_cursor})",
            )

        # Before scoring, so a deal is judged with the product already known to
        # be a women's shoe rather than an unclassified row.
        if classified:
            taxonomy.classify(conn, classified)

        if collect_only:
            _record_block(conn, run_id, limiter)
            _finish_run(conn, run_id, stats)
            return stats

        scorable = all_scorable_variants(conn) if rescan else changed
        codes = read_watchlist(config.watchlist_file)
        watching = watched_products(conn, codes)
        if codes:
            log.info("watching %d article(s), matching %d product(s)", len(codes), len(watching))
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
            watched=watching,
        )
        on_offer = arrange_for(
            conn, scored, shelf_config(config), market,
            cap_per_store=False, fold_duplicates=False, skip_alerted=False,
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
        readers = personal.subscribers(conn, config.chat_id, config.filters)
        shipping = landed.load_rules()
        eur = rates.to_usd(1.0, "EUR")
        eur_usd = eur[0] if eur else None
        cap = limit if limit is not None else config.filters.max_alerts_per_run
        if len(readers) > 1:
            log.info("%d readers", len(readers))

        queues: list[tuple[personal.Subscriber, list[tuple[dealm.Deal, sqlite3.Row]]]] = []
        capped_anyone = False
        for reader in readers:
            candidates = arrange_for(
                conn, scored, shelf_config(config), market, user_id=reader.user_id,
                rank=personal.ranker(
                    reader.reader, config.filters.min_score, shipping, eur_usd
                ),
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

        if dry_run:
            for reader, selected in queues:
                if len(queues) > 1:
                    print("=" * 60)
                    print(f"=== {reader.label} ({len(selected)})")
                for deal, row in selected:
                    print("-" * 60)
                    print(caption_for(deal, row, conn, shipping, eur_usd))
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

        for reader, selected in queues:
            stats.alerts_sent += await _send_to(
                conn, config, client, reader, selected, shipping, eur_usd
            )

    _record_block(conn, run_id, limiter)
    _finish_run(conn, run_id, stats)
    # Last, because the notice is about the run just recorded — including
    # whether it was blocked, which is only known a few lines above this.
    await _warn_if_degraded(conn, config, dry_run=dry_run)
    return stats


async def _send_to(
    conn: sqlite3.Connection,
    config: Config,
    client: httpx.AsyncClient,
    reader: personal.Subscriber,
    selected: list[tuple[dealm.Deal, sqlite3.Row]],
    shipping: landed.Rules,
    eur_usd: float | None,
) -> int:
    """Write one reader's queue to their chat. Returns how many arrived."""
    sent = 0
    async with Telegram(config.bot_token, reader.chat_id, client) as telegram:
        for deal, row in selected:
            # Claim the alert first: a crash mid-send must not cause a repeat.
            if not dealm.record_alert(
                conn, deal, dbm.utcnow(), user_id=reader.user_id
            ):
                continue
            if await telegram.send_deal(
                caption_for(deal, row, conn, shipping, eur_usd), row["image_url"]
            ):
                sent += 1
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


def health_report(conn: sqlite3.Connection) -> str:
    """A short HTML summary of how collection is going."""
    counts = dict(
        conn.execute("SELECT platform, COUNT(*) FROM stores GROUP BY platform").fetchall()
    )
    last = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # Compare against a cutoff built the way we store timestamps. SQLite's
    # datetime() yields "2026-08-27 08:25:00" while our rows read
    # "2026-08-27T00:24:19+00:00", and 'T' sorts after ' ', so every row from
    # today counted as being inside the last day whatever its hour.
    a_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    day = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE sent = 1 AND ts > ?", (a_day_ago,)
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
    lines += [
        f"Уведомлений за сутки: {day}",
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
