"""Deciding whether a price is actually a good deal.

The struck-through price a shop shows is marketing copy, not evidence — plenty
of stores keep an inflated "was" price up permanently. So a discount has to be
corroborated. Three independent signals feed the score:

1. the shop's own struck-through price,
2. the price against the median of what *we* recorded for that variant,
3. whether the price is the lowest we have ever seen.

Signal 2 needs no cooperation from the shop and cannot be gamed by it, so it
outranks signal 1 whenever there is enough history to use it. On a fresh
database only signal 1 exists, which is why it still counts on its own.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Filters

# Enough recorded history to trust the median over the shop's own claim.
MIN_HISTORY_POINTS = 3
MIN_HISTORY_DAYS = 7
BUCKET_RATIO = 1.05   # bucket width, used only for the UNIQUE backstop
RE_ALERT_DROP = 0.95  # a repeat needs the price at least 5% below the last alert


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
    reference_source: str        # "history" | "tag"
    discount_pct: float
    saving_usd: float
    score: int
    all_time_low: bool
    fake_sale: bool
    dropped_hours_ago: float | None
    history_points: int

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
    """
    tagged = [r for r in history if r["compare_at_usd"]]
    if len(tagged) < 2:
        return False
    newest = tagged[-1]
    unchanged_since = newest
    for row in reversed(tagged):
        if (
            abs(row["compare_at_usd"] - newest["compare_at_usd"]) > 0.005
            or row["compare_at_usd"] <= row["price_usd"]
        ):
            break
        unchanged_since = row
    age = _parse(newest["ts"]) - _parse(unchanged_since["ts"])
    return age >= timedelta(days=fake_sale_days)


def _dropped_hours_ago(history: list[sqlite3.Row], price_usd: float) -> float | None:
    """How long the current price has been in effect, in hours.

    None when there is only one observation: first sight of a product tells us
    when we looked, not when the shop changed anything, and reporting that as
    "the price just dropped" would be a claim we cannot support.
    """
    if len(history) < 2:
        return None
    started = None
    for row in reversed(history):
        if abs(row["price_usd"] - price_usd) > 0.005:
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
) -> Deal | None:
    """Score one variant. Returns None when it is not worth a notification.

    `history` is every recorded point for the variant, oldest first, including
    the current one.
    """
    if not in_stock or price_usd <= 0:
        return None
    if not (filters.min_price_usd <= price_usd <= filters.max_price_usd):
        return None

    past = [r["price_usd"] for r in history[:-1]] if len(history) > 1 else []
    span_days = (
        (_parse(history[-1]["ts"]) - _parse(history[0]["ts"])).total_seconds() / 86400
        if len(history) > 1
        else 0.0
    )
    has_history = len(past) >= MIN_HISTORY_POINTS and span_days >= MIN_HISTORY_DAYS
    fake_sale = _is_fake_sale(history, filters.fake_sale_days)

    # Our own median beats the shop's claim; the tag is the fallback.
    if has_history:
        reference, source = statistics.median(past), "history"
    elif compare_at_usd and compare_at_usd > price_usd:
        reference, source = compare_at_usd, "tag"
    else:
        return None

    if reference <= price_usd:
        return None

    discount_pct = (reference - price_usd) / reference * 100
    saving_usd = reference - price_usd
    if discount_pct < filters.min_discount_pct or saving_usd < filters.min_saving_usd:
        return None

    all_time_low = bool(past) and price_usd < min(past) - 0.005

    score = discount_pct * 1.6
    if source == "history":
        score += 8          # corroborated by data the shop cannot edit
    if all_time_low:
        score += 12
    if fake_sale:
        score -= 25         # the "was" price is decoration
    score = int(max(0, min(100, round(score))))
    if score < filters.min_score:
        return None

    return Deal(
        variant_id=variant_id,
        product_id=product_id,
        price_usd=round(price_usd, 2),
        reference_usd=round(reference, 2),
        reference_source=source,
        discount_pct=round(discount_pct, 1),
        saving_usd=round(saving_usd, 2),
        score=score,
        all_time_low=all_time_low,
        fake_sale=fake_sale,
        dropped_hours_ago=_dropped_hours_ago(history, price_usd),
        history_points=len(history),
    )


def already_alerted(conn: sqlite3.Connection, deal: Deal) -> bool:
    """True unless the price has fallen a further RE_ALERT_DROP below the best
    price we have already announced for this product.

    Keyed on the product rather than the variant, so a shoe discounted in eight
    sizes is announced once instead of eight times.

    Compared on price rather than on bucket: two prices 1.7% apart can still fall
    either side of a bucket edge, and that boundary artefact would let a
    near-identical price through. The bucket column remains as the UNIQUE
    backstop in the table, which is about races, not about judgement.
    """
    best = conn.execute(
        "SELECT MIN(price_usd) FROM alerts WHERE product_id = ?", (deal.product_id,)
    ).fetchone()[0]
    if best is None:
        return False
    return deal.price_usd >= best * RE_ALERT_DROP


def record_alert(conn: sqlite3.Connection, deal: Deal, ts: str) -> bool:
    """Persist the alert. Returns False if it was already there (race-safe)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alerts
            (product_id, variant_id, ts, price_usd, price_bucket, discount_pct, score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            deal.product_id, deal.variant_id, ts, deal.price_usd,
            deal.bucket, deal.discount_pct, deal.score,
        ),
    )
    return cur.rowcount > 0
