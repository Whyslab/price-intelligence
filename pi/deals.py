"""Deciding whether a price is actually a good deal.

The struck-through price a shop shows is marketing copy, not evidence. It is
also the only reference that costs nothing to produce, which is why it used to
be the answer: on the live database 520 notifications went out and every single
one of them rested on it alone, because the checks meant to outrank it needed
four price points and seven days of history, and 2,749,975 variants out of
2,762,842 had been seen exactly once.

So the reference price is chosen from `pi.reference`, in descending order of how
hard it is to fake, and the shop's own tag comes last:

1. the lowest price this shop actually charged in the 30 days before the drop,
2. what other shops charge for the same article number right now,
3. the recommended price, read as the mode of many shops' struck-through prices,
4. the shop's own tag — and only from a shop that has not disqualified itself.

The first one is decisive when it exists. If a shop's price has not fallen below
its own recent floor, it has not dropped its price, whatever the tag says: put
200 up to 300 for a week and back to 200 and the floor is still 200. Falling
through to the market or the tag there would reinstate exactly the fiction the
floor exists to catch.

Two vetoes sit in front of all of it. A price above what other shops are asking
is not a discount, whatever it is marked down from. And a tag more than
`inflated_tag_pct` above the recommended price — or from a shop running the same
percentage across its whole catalogue — is not evidence of anything.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Filters
from .reference import MAX_DROP, Market, Trust, prior_floor

# How much of the reference window has to be covered by observations before the
# floor it produces is worth believing. A "30-day low" drawn from two days of
# data is a two-day low wearing the wrong name.
MIN_HISTORY_DAYS = 7
BUCKET_RATIO = 1.05   # bucket width, used only for the UNIQUE backstop
RE_ALERT_DROP = 0.95  # a repeat needs the price at least 5% below the last alert
# The same rule for something a reader asked to follow by name. 5% is the bar
# for an unsolicited interruption about a thing nobody named; being told again
# about the one product you starred is not an interruption of the same kind, and
# on a $200 jacket 2% is $4 — small, but exactly the size of movement somebody
# waiting for a particular thing wants to know about.
FAVORITE_RE_ALERT_DROP = 0.98


def price_bucket(price_usd: float) -> int:
    """Bucket prices so two within 5% of each other collide.

    This is what stops the same shoe being announced over and over: the alerts
    table is UNIQUE on (variant_id, price_bucket), so a second notification only
    happens once the price has fallen into a lower bucket.
    """
    return math.floor(math.log(max(price_usd, 0.01)) / math.log(BUCKET_RATIO))


@dataclass(slots=True)
class Deal:
    variant_id: int
    product_id: int
    price_usd: float
    reference_usd: float
    reference_source: str        # "history" | "market" | "msrp" | "tag"
    discount_pct: float
    saving_usd: float
    score: int
    all_time_low: bool
    fake_sale: bool
    dropped_hours_ago: float | None
    history_points: int
    # What the rest of the market said, so the notification can show its working
    # instead of asking to be taken on faith.
    market_shops: int = 0
    market_median_usd: float | None = None
    beats_market: bool = False
    msrp_usd: float | None = None
    # How many shops struck a price through, which is not how many quote one:
    # the notification names this figure as its evidence and they differ.
    msrp_shops: int = 0
    inflated_tag: bool = False
    rule_priced: bool = False
    blanket_pct: float | None = None
    # How many other shops had the same article on offer this run. Their alerts
    # were folded into this one, so the reader hears about the shoe once.
    also_in_shops: int = 0
    # An article on the watchlist. It reaches the reader whatever the thresholds
    # say, because the point of watching one is not to be told only about the
    # big drops.
    watched: bool = False

    @property
    def bucket(self) -> int:
        return price_bucket(self.price_usd)


def _parse(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_fake_sale(history: list[sqlite3.Row], fake_sale_days: int) -> bool:
    """True when the struck-through price has sat unchanged above the asking price.

    A genuine sale has a beginning. A "was £220, now £120" that has read exactly
    that way for a month is simply what the shoe costs.

    Only the tag's own stability is tested, not the asking price's: a shop that
    keeps "was £220" pinned for two months while nudging the price between £120
    and £130 is still running a permanent sale.

    Compared in the shop's own currency, so a moving exchange rate cannot make a
    pinned tag look like it just changed.
    """
    tagged = [r for r in history if _tag_native(r)]
    if len(tagged) < 2:
        return False
    newest = tagged[-1]
    newest_tag = _tag_native(newest)
    unchanged_since = newest
    for row in reversed(tagged):
        tag = _tag_native(row)
        if abs(tag - newest_tag) > 0.005 or tag <= row["price_native"]:
            break
        unchanged_since = row
    age = _parse(newest["ts"]) - _parse(unchanged_since["ts"])
    return age >= timedelta(days=fake_sale_days)


def _tag_native(row: sqlite3.Row) -> float | None:
    """The struck-through price in the shop's currency, or None if there is none."""
    return row["compare_at_native"] or None


def _dropped_hours_ago(history: list[sqlite3.Row], price_native: float) -> float | None:
    """How long the current price has been in effect, in hours.

    None when there is only one observation: first sight of a product tells us
    when we looked, not when the shop changed anything, and reporting that as
    "the price just dropped" would be a claim we cannot support.

    Matched on the shop's own price. In dollars, a variant whose price had not
    moved in months reported "price dropped 4 hours ago" every time the euro did.
    """
    if len(history) < 2:
        return None
    started = None
    for row in reversed(history):
        if abs(row["price_native"] - price_native) > 0.005:
            break
        started = row
    if started is None:
        return None
    delta = datetime.now(UTC) - _parse(started["ts"])
    return round(max(delta.total_seconds(), 0) / 3600, 1)


def evaluate(
    variant_id: int,
    product_id: int,
    price_usd: float,
    compare_at_usd: float | None,
    in_stock: bool,
    history: list[sqlite3.Row],
    filters: Filters,
    market: Market | None = None,
    trust: Trust | None = None,
    watched: bool = False,
) -> Deal | None:
    """Score one variant. Returns None when it is not worth a notification.

    `history` is every recorded point for the variant, oldest first, including
    the current one. `market` is what other shops charge for the same article
    number; `trust` is what this shop's catalogue says about how much its own
    struck-through prices are worth. Both default to knowing nothing, in which
    case only the shop's history and its tag are available.
    """
    if not in_stock or price_usd <= 0 or not history:
        return None
    if not watched and not (filters.min_price_usd <= price_usd <= filters.max_price_usd):
        return None
    market = market or Market()
    trust = trust or Trust()

    # Everything below is arithmetic in the shop's own currency, converted to
    # dollars only at the end. Comparing dollar figures recorded on different
    # days compares two things at once — the shop's price and the exchange rate —
    # and the rate is not news.
    current = history[-1]
    fx_rate = current["fx_rate"] or 1.0
    price_native = current["price_native"]
    compare_native = _tag_native(current)

    def to_native(usd: float | None) -> float | None:
        return None if usd is None else usd * fx_rate

    market_native = to_native(market.median_usd) if market.priced(filters.market_min_shops) else None
    msrp_native = to_native(market.msrp_usd) if market.has_msrp(filters.msrp_min_shops) else None
    low_native = to_native(market.low_usd) if market.priced(filters.market_min_shops) else None

    # Veto: whatever it is marked down from, a price above what other shops are
    # asking for the same article is not a discount.
    if market_native is not None and price_native > market_native + 0.005:
        return None

    rule_priced = trust.rule_priced(
        filters.rule_priced_share, filters.blanket_sale_share
    )
    inflated_tag = bool(
        msrp_native
        and compare_native
        and compare_native > msrp_native * (1 + filters.inflated_tag_pct / 100)
    )
    tag_native = (
        compare_native if compare_native and not rule_priced and not inflated_tag else None
    )

    floor = prior_floor(history, filters.reference_window_days)
    if floor is not None and floor.covered_days < MIN_HISTORY_DAYS:
        floor = None

    if floor is not None:
        # The shop's own recent floor is decisive: if the price is not below it,
        # the price did not drop, and no tag or market figure may say otherwise.
        if floor.lowest_native <= price_native + 0.005:
            return None
        reference_native, source = floor.lowest_native, "history"
    elif market_native is not None and market_native > price_native:
        reference_native, source = market_native, "market"
    elif msrp_native is not None and msrp_native > price_native:
        reference_native, source = msrp_native, "msrp"
    elif tag_native and tag_native > price_native:
        reference_native, source = tag_native, "tag"
    else:
        return None

    if reference_native > price_native * MAX_DROP:
        # Nobody cuts a price by 95%. www.freshmansarchive.com marks a $40
        # vintage fleece down from $1,420 and a $81 blazer from $1,691, and
        # because it does not do that across its whole catalogue, none of the
        # rule-pricing or inflated-tag tests catch it. The claim survives every
        # check and lands at the top of the shelf, which is where the least
        # believable number in the database should never be.
        return None

    discount_pct = (reference_native - price_native) / reference_native * 100
    saving_usd = round((reference_native - price_native) / fx_rate, 2)
    if not watched and (
        discount_pct < filters.min_discount_pct or saving_usd < filters.min_saving_usd
    ):
        return None

    past = [r["price_native"] for r in history[:-1] if r["currency"] == current["currency"]]
    all_time_low = bool(past) and price_native < min(past) - 0.005
    beats_market = bool(low_native and price_native <= low_native + 0.005 and market.shops >= 1)
    fake_sale = _is_fake_sale(history, filters.fake_sale_days)

    score = discount_pct * 1.6
    if source == "history":
        score += 10         # the shop's own floor, which it cannot rewrite
    elif source == "market":
        score += 12         # priced against shops with no stake in this one
    elif source == "msrp":
        score += 6
    if all_time_low:
        score += 12
    if beats_market:
        score += 15         # cheaper than every other shop we can see
    if fake_sale:
        score -= 25         # the "was" price is decoration
    if rule_priced:
        score -= 20         # so is every other "was" price in this shop
    score = int(max(0, min(100, round(score))))
    if not watched and score < filters.min_score:
        return None

    return Deal(
        variant_id=variant_id,
        product_id=product_id,
        price_usd=round(price_usd, 2),
        reference_usd=round(reference_native / fx_rate, 2),
        reference_source=source,
        discount_pct=round(discount_pct, 1),
        saving_usd=round(saving_usd, 2),
        score=score,
        all_time_low=all_time_low,
        fake_sale=fake_sale,
        dropped_hours_ago=_dropped_hours_ago(history, price_native),
        history_points=len(history),
        market_shops=market.shops,
        market_median_usd=market.median_usd,
        beats_market=beats_market,
        msrp_usd=market.msrp_usd if market.has_msrp(filters.msrp_min_shops) else None,
        msrp_shops=market.msrp_shops if market.has_msrp(filters.msrp_min_shops) else 0,
        inflated_tag=inflated_tag,
        blanket_pct=trust.blanket_pct if rule_priced else None,
        rule_priced=rule_priced,
        watched=watched,
    )


def already_alerted(
    conn: sqlite3.Connection,
    deal: Deal,
    user_id: int = 0,
    re_alert_drop: float = RE_ALERT_DROP,
) -> bool:
    """True unless the price has fallen a further RE_ALERT_DROP below the best
    price we have already announced for this product *to this reader*.

    Keyed on the product rather than the variant, so a shoe discounted in eight
    sizes is announced once instead of eight times.

    Keyed on the reader as well, because news is news to each person separately.
    Asking only "was this product announced" was right while there was one
    reader and quietly wrong the moment there were two: whoever the run reached
    first would close the story for everybody behind them, so the more people
    subscribed the less each of them heard. Rows carrying user 0 still count for
    everyone — see the note on the column — which is what makes `pi seed` and
    everything announced before this existed apply to a reader who joins today.

    Compared on price rather than on bucket: two prices 1.7% apart can still fall
    either side of a bucket edge, and that boundary artefact would let a
    near-identical price through. The bucket column remains as the UNIQUE
    backstop in the table, which is about races, not about judgement.
    """
    best = conn.execute(
        "SELECT MIN(price_usd) FROM alerts WHERE product_id = ? AND user_id IN (0, ?)",
        (deal.product_id, user_id),
    ).fetchone()[0]
    if best is None:
        return False
    return deal.price_usd >= best * re_alert_drop


def record_alert(
    conn: sqlite3.Connection, deal: Deal, ts: str, sent: bool = True, user_id: int = 0
) -> bool:
    """Persist the alert. Returns False if it was already there (race-safe).

    `sent=False` is for `pi seed`, which records deals in order to suppress a
    notification rather than to report one. It leaves `user_id` at 0, because
    what it is recording is true of every reader.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alerts
            (product_id, variant_id, ts, price_usd, price_bucket, discount_pct,
             score, sent, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            deal.product_id, deal.variant_id, ts, deal.price_usd,
            deal.bucket, deal.discount_pct, deal.score, int(sent), user_id,
        ),
    )
    return cur.rowcount > 0
