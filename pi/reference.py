"""What a price should be compared against.

The shop's own struck-through price is the only reference that needs no data, and
it is the only one the shop can write itself. Everything here exists to stop that
being the answer.

Four references, in the order they are trusted:

1. **The lowest price this shop actually charged in the 30 days before the drop.**
   This is the formula the EU settled on (directive 98/6/EC as amended) for the
   same reason: it is the one number a "was 300, now 200" cannot survive if the
   300 was invented last week. Raising 200 to 300 for seven days and dropping back
   leaves the 30-day floor at 200, so the discount is zero.
2. **What other shops charge for the same product right now.** Needs no history
   at all, which matters on a database where 2.7 million variants out of 2.76
   million have been seen exactly once.
3. **The recommended price, read as the mode of the struck-through prices many
   shops show.** A real RRP is quoted by everyone; an invented one by nobody else.
4. **The shop's own tag** — last, and only from a shop that has not disqualified
   itself by putting the same discount on its whole catalogue.

Products are matched between shops on the manufacturer's article number, which
survives every shop's own naming: `CW2288-111` appears in fourteen of the shops
on the list, `IF4396-103` in twelve.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .domains import same_shop

# Article numbers as the big brands write them. Matched in SKUs and in titles
# alike, because plenty of shops put the code in the product name and nowhere
# else.
#
# Which shapes earn their place was measured, not guessed. The number that
# matters is not how many products gain a code but how many codes end up shared
# by three shops or more, since that is what a market price needs. Across the
# whole catalogue, added to the original set:
#
#     New Balance  M2002RDB, U204LMMA   +597 shared keys   +7,060 products
#     Asics        1201A019-021         +295               +2,401
#     Puma         635235-01             +69               +9,283
#     adidas 2+5   IF43961               +30               +7,447
#     Reebok       100008493             +17               +3,717
#     Vans         VN0A7Q2J               +0                 +350
#
# The first three are in. The last three are not: they cost the most in false
# matches — a bare nine-digit run is anything at all — and return almost nothing.
# Puma's shape earns its place on 69 keys rather than on the 9,283 products,
# which are mostly one shop talking to itself.
#
# Converse was listed here as an example and never matched: 162050C is six
# digits and a letter, and the pattern claiming it wanted five. Written properly
# it adds 26 shared keys, among them the Chuck 70 in nineteen shops.
_STYLE_CODE = re.compile(
    r"\b("
    r"[A-Z]{2}\d{4}-\d{3}"          # Nike, Jordan   CW2288-111
    r"|\d{6}-\d{3}"                 # older Nike     414571-102
    r"|\d{4}[A-Z]\d{3}-\d{3}"       # Asics          1201A019-021
    r"|\d{6}-\d{2}"                 # Puma           635235-01
    r"|[A-Z]{2}\d{4}"                # adidas         IF4396
    r"|[A-Z]{1,2}\d{3,4}[A-Z]{2,4}\d?"  # New Balance M990GL6, U204LMMA, CT302OE
    r"|\d{6}[A-Z]"                   # Converse       162050C
    r"|[A-Z]?\d{5}[A-Z]"             # five-digit codes with a colour letter
    r")\b"
)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
# Colour and packaging notes that shops append to an otherwise identical title.
_TITLE_NOISE = re.compile(
    r"\b(brand new|pre owned|opened packaging|used|deadstock|ds|vnds|sz \d+(\.\d)?)\b"
)

SKU = "sku"
STYLE = "style"
TITLE = "title"


def style_codes(text: str | None) -> set[str]:
    """Every manufacturer article number visible in a SKU or a product title."""
    if not text:
        return set()
    return {match.group(1) for match in _STYLE_CODE.finditer(text.upper())}


def title_key(brand: str | None, title: str | None) -> str | None:
    """Brand and title reduced to the part two shops are likely to agree on."""
    if not title:
        return None
    combined = f"{brand or ''} {title}".lower()
    combined = _TITLE_NOISE.sub(" ", combined)
    key = _NOT_ALNUM.sub(" ", combined).strip()
    return key if len(key) >= 8 else None


def keys_for(brand: str | None, title: str | None, skus: list[str | None]) -> set[tuple[str, str]]:
    """Every handle under which this product might be recognised elsewhere.

    A key is a claim that two rows are the same product, so it has to be one a
    different shop would arrive at independently. The shop's internal SKU usually
    is not — but plenty of shops use the manufacturer's number as their SKU, and
    that is exactly the case worth catching.
    """
    keys: set[tuple[str, str]] = set()
    for sku in skus:
        cleaned = (sku or "").strip().upper()
        if len(cleaned) >= 4:
            keys.add((SKU, cleaned))
        keys.update((STYLE, code) for code in style_codes(cleaned))
    keys.update((STYLE, code) for code in style_codes(title))
    key = title_key(brand, title)
    if key:
        keys.add((TITLE, key))
    return keys


@dataclass(frozen=True, slots=True)
class Market:
    """What shops other than this one say about the same product, in USD."""

    median_usd: float | None = None
    low_usd: float | None = None
    shops: int = 0
    msrp_usd: float | None = None
    msrp_shops: int = 0

    def priced(self, min_shops: int) -> bool:
        return self.median_usd is not None and self.shops >= min_shops

    def has_msrp(self, min_shops: int) -> bool:
        return self.msrp_usd is not None and self.msrp_shops >= min_shops


# Below this many struck-through items, the shape of a shop's discounts is not a
# fact about the shop. Three things on sale together is a small shop; three
# thousand is a policy.
MIN_TAGGED_SAMPLE = 50
# A struck-through price this close to the asking price is not a claim about
# anything. One shop carries 20,185 of them, which would otherwise read as a
# catalogue almost entirely on sale.
MIN_MEANINGFUL_PCT = 1.0
# How near a round 5% step counts as landing on it.
ROUND_STEP_TOLERANCE = 0.35


@dataclass(frozen=True, slots=True)
class Trust:
    """Whether a shop's struck-through prices are prices or arithmetic.

    The discriminating measurement, taken across all 78 shops with enough tags to
    judge: **what share of a shop's discounts land on a round 5% step.**

        www.superga.com          100%   www.footpatrol.com        21%
        it.oneblockdown.it       100%   www.overkillshop.com      16%
        www.urbanjunglestore.com 100%   shoegallerymiami.com      10%

    A former price that an item was really sold at produces whatever percentage
    the arithmetic produces — 36.4%, 22.8%, 41.1% — and lands on a round step
    about as often as chance allows; the median shop sits at 55%. A shop at 100%
    is not recording what things used to cost. It is applying "-40% to this
    category" and letting the "was" price be whatever makes the sum come out.

    That is exactly the fake the whole reference chain exists to catch, and it is
    visible without any history at all.
    """

    tag_share: float = 0.0        # share of the catalogue struck through
    round_share: float = 0.0      # share of those discounts on a round 5% step
    blanket_pct: float | None = None
    blanket_share: float = 0.0    # share of the catalogue at that one percentage
    sample: int = 0               # in-stock items
    tagged: int = 0               # of which struck through meaningfully

    def rule_priced(self, min_round_share: float, min_blanket_share: float) -> bool:
        """True when this shop's "was" prices are computed rather than remembered.

        Two shapes count. A ladder of round steps covering the whole catalogue —
        oneblockdown's 40/45/30/50/60/35, which no single-bucket test catches
        because the largest step is only 27% of it. And one identical percentage
        across the catalogue, which need not be round.
        """
        if self.tagged < MIN_TAGGED_SAMPLE or self.tag_share <= 0.15:
            return False
        return (
            self.round_share >= min_round_share
            or (self.tag_share > 0.5 and self.blanket_share >= min_blanket_share)
        )


def mode_price(prices: list[float], tolerance: float = 0.03) -> float | None:
    """The price most shops agree on, within a tolerance.

    A recommended price is a round number many shops copy from the brand, so it
    shows up as a cluster. An inflated one sits alone. The median would split the
    difference between the two; the mode picks the crowd.
    """
    if not prices:
        return None
    ordered = sorted(prices)
    best: list[float] = []
    for start in range(len(ordered)):
        cluster = [p for p in ordered[start:] if p <= ordered[start] * (1 + tolerance)]
        if len(cluster) > len(best):
            best = cluster
    return round(statistics.median(best), 2)


class MarketIndex:
    """Current prices for every product two or more shops both stock.

    Built once per run. The whole catalogue is 2.7 million variants, so the
    grouping happens in SQL and only the keys that more than one shop actually
    shares — tens of thousands, not millions — are ever brought into Python.
    """

    def __init__(self, by_key: dict[tuple[str, str], dict[str, tuple[float, float | None]]],
                 keys_by_product: dict[int, set[tuple[str, str]]]):
        self._by_key = by_key
        self._keys_by_product = keys_by_product

    @property
    def keys(self) -> int:
        return len(self._by_key)

    def identity(self, product_id: int) -> tuple[str, str] | None:
        """The strongest key under which other shops also stock this product.

        Strongest first, because the keys are not equally trustworthy: an
        article number is the manufacturer's and two shops arrive at it
        independently, while a title match is two shops happening to describe
        something similarly. None means nobody else stocks it, and it stands
        alone.
        """
        keys = self._keys_by_product.get(product_id)
        if not keys:
            return None
        for kind in (STYLE, SKU, TITLE):
            matching = sorted(key for key in keys if key[0] == kind)
            if matching:
                return matching[0]
        return None

    def look_up(self, product_id: int, shop: str) -> Market:
        """What everyone *except* `shop` is charging for this product.

        Excluding the shop itself is the point: a price confirmed by
        `bdgastore.com` and `shop.bdgastore.com` is one shop agreeing with itself.

        `shop` is canonicalised here rather than trusted, because the failure of
        passing a raw hostname is silent and in the wrong direction — the shop
        stops matching its own key, so its own price is counted as somebody
        else's opinion of it, and a lone shop looks corroborated.
        """
        shop = same_shop(shop)
        prices: dict[str, float] = {}
        tags: dict[str, float] = {}
        for key in self._keys_by_product.get(product_id, ()):
            for other, (price, tag) in self._by_key.get(key, {}).items():
                if other == shop:
                    continue
                if other not in prices or price < prices[other]:
                    prices[other] = price
                if tag and (other not in tags or tag > tags[other]):
                    tags[other] = tag
        if not prices:
            return Market()
        agreed = agreeing_prices(list(prices.values()))
        if not agreed:
            return Market()
        # Only the shops that survived `agreeing_prices` may speak, and they
        # speak once — about the price and about the tag alike. A shop dropped
        # for asking $3,824 where others ask $60 was dropped because it is not
        # holding this article; its struck-through price is then a claim about
        # something else, and counting it towards the recommended price reads
        # the same rejected evidence a second time. `agreeing_prices` returns a
        # contiguous run of the sorted prices, so its ends bound exactly who is
        # still in.
        low, high = min(agreed), max(agreed)
        kept = {shop for shop, price in prices.items() if low <= price <= high}
        agreed_tags = [tag for shop, tag in tags.items() if shop in kept]
        return Market(
            median_usd=round(statistics.median(agreed), 2),
            low_usd=round(low, 2),
            shops=len(agreed),
            msrp_usd=mode_price(agreed_tags),
            msrp_shops=len(agreed_tags),
        )


# How far two shops may be apart and still be talking about the same thing.
# Four is loose on purpose — the same shoe really is $90 in a sale and $220 at
# full price, and a reference that rejected that would reject most of the
# market. What it does reject is $61 against $3,824.
MAX_SPREAD = 4.0
# The largest fall from a shop's own earlier price that can be believed. The
# live shelf's history-sourced references stop at 6× and then jump straight to
# 87,000×, so there is nothing real in between to lose.
MAX_DROP = 20.0


def agreeing_prices(prices: list[float]) -> list[float]:
    """The prices that are plausibly for the same article, or nothing.

    Article matching is a claim, and a wrong claim is invisible until it prices
    something. Measured on the live index, 3.9% of matched articles have one
    shop asking ten times what another does — because shops put things in the
    SKU field that are not article numbers. `8.625` is a skateboard's width;
    `SN8-020325` is somebody's internal batch code covering three different Vans
    models; `DIME` and `POLAR` are brand names, and 413 unrelated products share
    the first of them.

    The damage is not subtle. sneakers123.com lists a Vans Authentic at $3,824
    against another shop's $60.99; the median of two is their average, so the
    shelf showed a real $46 shoe as 98% off $1,942 — at the very top, sorted
    there by the size of the lie.

    Two shops that disagree by more than MAX_SPREAD are not corroborating each
    other, they are describing different things, and there is no way to tell
    which one is right: the honest answer is that this product has no market
    price. With three or more, the median is a stable enough middle to measure
    from, so the outliers are dropped and the rest still count.
    """
    if len(prices) < 2:
        return prices
    ordered = sorted(prices)
    if len(prices) == 2:
        low, high = ordered
        return [] if low <= 0 or high / low > MAX_SPREAD else ordered
    middle = statistics.median(ordered)
    if middle <= 0:
        return []
    return [p for p in ordered if middle / MAX_SPREAD <= p <= middle * MAX_SPREAD]


def build_market_index(conn: sqlite3.Connection) -> MarketIndex:
    """Read the current in-stock price of every shared product, one shop at a time."""
    conn.execute("DROP TABLE IF EXISTS temp.pi_market")
    conn.execute(
        """
        CREATE TEMP TABLE pi_market AS
        SELECT k.key_type   AS key_type,
               k.key        AS key,
               p.store_id   AS store_id,
               v.product_id AS product_id,
               MIN(latest.price_usd)      AS price_usd,
               MAX(latest.compare_at_usd) AS compare_at_usd
        FROM (
            SELECT variant_id, price_usd, compare_at_usd
            FROM (
                SELECT variant_id, price_usd, compare_at_usd, in_stock,
                       ROW_NUMBER() OVER (PARTITION BY variant_id ORDER BY ts DESC) AS rn
                FROM price_points
            )
            WHERE rn = 1 AND in_stock = 1
        ) AS latest
        JOIN variants     v ON v.id = latest.variant_id
        JOIN products     p ON p.id = v.product_id
        JOIN product_keys k ON k.product_id = p.id
        GROUP BY k.key_type, k.key, p.store_id
        """
    )
    conn.execute("CREATE INDEX temp.ix_pi_market ON pi_market(key_type, key)")

    by_key: dict[tuple[str, str], dict[str, tuple[float, float | None]]] = defaultdict(dict)
    keys_by_product: dict[int, set[tuple[str, str]]] = defaultdict(set)
    rows = conn.execute(
        """
        SELECT m.key_type, m.key, m.product_id, m.price_usd, m.compare_at_usd, s.domain
        FROM pi_market m
        JOIN stores s ON s.id = m.store_id
        WHERE (m.key_type, m.key) IN (
            SELECT key_type, key FROM pi_market GROUP BY key_type, key HAVING COUNT(*) > 1
        )
        """
    )
    for key_type, key, product_id, price_usd, compare_at_usd, domain in rows:
        key = (key_type, key)
        shop = same_shop(domain)
        known = by_key[key].get(shop)
        if known is None or price_usd < known[0]:
            by_key[key][shop] = (price_usd, compare_at_usd)
        keys_by_product[product_id].add(key)
    return MarketIndex(dict(by_key), dict(keys_by_product))


def store_trust(conn: sqlite3.Connection) -> dict[int, Trust]:
    """For every shop: how much of its catalogue is struck through, and how much
    of it at one and the same percentage."""
    tally: dict[int, tuple[int, int, int, dict[int, int]]] = {}
    rows = conn.execute(
        """
        SELECT p.store_id, latest.price_usd, latest.compare_at_usd
        FROM (
            SELECT variant_id, price_usd, compare_at_usd
            FROM (
                SELECT variant_id, price_usd, compare_at_usd, in_stock,
                       ROW_NUMBER() OVER (PARTITION BY variant_id ORDER BY ts DESC) AS rn
                FROM price_points
            )
            WHERE rn = 1 AND in_stock = 1
        ) AS latest
        JOIN variants v ON v.id = latest.variant_id
        JOIN products p ON p.id = v.product_id
        """
    )
    for store_id, price_usd, compare_at_usd in rows:
        total, tagged, on_step, buckets = tally.get(store_id) or (0, 0, 0, defaultdict(int))
        total += 1
        if compare_at_usd and price_usd and compare_at_usd > price_usd:
            pct = (compare_at_usd - price_usd) / compare_at_usd * 100
            if pct >= MIN_MEANINGFUL_PCT:
                tagged += 1
                buckets[round(pct)] += 1
                if abs(pct - 5 * round(pct / 5)) <= ROUND_STEP_TOLERANCE:
                    on_step += 1
        tally[store_id] = (total, tagged, on_step, buckets)

    trust: dict[int, Trust] = {}
    for store_id, (total, tagged, on_step, buckets) in tally.items():
        if not total:
            continue
        top_pct, top_count = max(buckets.items(), key=lambda kv: kv[1]) if buckets else (None, 0)
        trust[store_id] = Trust(
            tag_share=round(tagged / total, 3),
            round_share=round(on_step / tagged, 3) if tagged else 0.0,
            blanket_pct=float(top_pct) if top_pct is not None else None,
            blanket_share=round(top_count / total, 3),
            sample=total,
            tagged=tagged,
        )
    return trust


def record_trust(conn: sqlite3.Connection, trust: dict[int, Trust]) -> None:
    """Keep each shop's discount profile in the store row.

    Not needed to score anything — it is recomputed every run — but it is the
    only place the reason a shop's tags are being ignored is visible without
    reading the code.
    """
    conn.executemany(
        "UPDATE stores SET tag_share = ?, round_share = ?, blanket_pct = ?,"
        " blanket_share = ? WHERE id = ?",
        [
            (t.tag_share, t.round_share, t.blanket_pct, t.blanket_share, store_id)
            for store_id, t in trust.items()
        ],
    )


def _parse(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PriorFloor:
    """The shop's own lowest price before the current one took effect."""

    lowest_native: float
    covered_days: float
    began: datetime


def prior_floor(
    history: list[sqlite3.Row], window_days: int, now: datetime | None = None
) -> PriorFloor | None:
    """Lowest price this shop actually charged in the window before the drop.

    Not a median over recorded points: those are change events, so a price that
    stood for six months is one row and a week of jitter is five, and the median
    reads the week as the norm. This walks the step function the rows describe.

    Returns None when the current price has always been the price, or when there
    is nothing recorded before it — in both cases the shop has not dropped
    anything as far as we can tell, which is a different statement from "no
    discount" and the caller should treat it as such.
    """
    if len(history) < 2:
        return None
    now = now or datetime.now(UTC)
    current = history[-1]
    currency, price = current["currency"], current["price_native"]

    began_at = len(history) - 1
    for index in range(len(history) - 1, -1, -1):
        row = history[index]
        if row["currency"] != currency or abs(row["price_native"] - price) > 0.005:
            break
        began_at = index
    if began_at == 0:
        return None  # this has always been the price

    began = _parse(history[began_at]["ts"])
    window_start = began - timedelta(days=window_days)
    lowest: float | None = None
    for index in range(began_at):
        row = history[index]
        if row["currency"] != currency:
            continue
        if _parse(history[index + 1]["ts"]) <= window_start:
            continue  # this price had already been replaced before the window
        lowest = row["price_native"] if lowest is None else min(lowest, row["price_native"])
    if lowest is None:
        return None
    if lowest > price * MAX_DROP:
        # No shop cuts a price by 95%. A ratio like that says the earlier
        # figure was never a price: topshelfslc.com records 99,999 against a
        # median of 190, a placeholder that survives the ingestion ceiling
        # because it is not absurd enough relative to the shop. Believing it
        # would put the item on the shelf at −100%, which is the one number on
        # a shelf that is always wrong.
        return None

    covered_from = max(window_start, _parse(history[0]["ts"]))
    return PriorFloor(
        lowest_native=lowest,
        covered_days=max((began - covered_from).total_seconds() / 86400, 0.0),
        began=began,
    )
