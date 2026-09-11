"""Two finds a day for the readers who have not paid.

The free tier is not a smaller copy of the product. It is the product
advertising itself: two things off a shelf of thirty-two thousand, chosen to be
the best of them, sent once a day to somebody who cannot search that shelf.

Three decisions worth writing down.

**It reads the shelf, not the run.** The feed answers "what fell just now" and
is worth money because it is fast. The digest answers "what is this bot like",
and a find from three weeks ago that is still standing and still deeply cut
answers that perfectly. Tying the digest to fresh drops would make it
intermittent — some days nothing qualifies — and an advertisement that
sometimes does not arrive is worse than a modest one that always does.

**It is deduplicated against itself only.** Alerts written by `pi seed` and by
runs carry user 0, which means "everybody", and excluding those would hide
almost the whole shelf: seeding declared 29,853 running discounts already seen.
The digest keeps its own row under `FREE_READER`, so what it has shown once it
does not show again, and what a paying reader was told has no bearing on it.

**Two, from two shops, of two brands.** One sale can supply the deepest twenty
cuts on the shelf, and a day showing the same shop twice reads as that shop's
advertisement rather than as this one's.
"""
from __future__ import annotations

import logging
import sqlite3

from . import db as dbm

log = logging.getLogger(__name__)

# The reader the digest's own history is filed under. Not 0: that value means
# "everybody" on this column, and rows carrying it suppress a find for every
# reader there is. The digest must be able to show a paying reader's find to a
# free one, and must not close anything for anybody but itself.
FREE_READER = dbm.DIGEST_READER

# How deep to look before choosing. Enough that the shop-and-brand rule has room
# to skip past a single sale filling the top of the shelf, small enough that the
# query stays a page rather than the shelf.
CANDIDATE_DEPTH = 200


# How stale a price may be and still be worth advertising with. See the query.
MAX_AGE_HOURS = 48


def pick(conn: sqlite3.Connection, count: int = 2) -> list[sqlite3.Row]:
    """The best offers not shown before, at most one per shop and per brand.

    Ordered by whether the reference price came from other shops before it is
    ordered by score. A cut measured against what six other shops are asking is
    the one thing a free channel cannot say, so it is the one that should be in
    the advertisement — and `reference_source` is exactly that distinction,
    already computed and stored.
    """
    rows = conn.execute(
        f"""
        SELECT o.*, v.size_norm, v.size, p.title, p.url, p.image_url, p.brand,
               p.brand_norm, p.brand_family, p.kind,
               s.domain, s.name AS store_name, s.country
          FROM offers o
          JOIN variants v ON v.id = o.variant_id
          JOIN products p ON p.id = o.product_id
          JOIN stores s   ON s.id = p.store_id
         WHERE NOT EXISTS (
                   SELECT 1 FROM alerts a
                    WHERE a.product_id = o.product_id AND a.user_id = ?
               )
           -- The offered size has to still be there. The shelf keeps a card
           -- until the shop stops listing it, which is right for browsing and
           -- wrong for an advertisement: the two deepest cuts on the shelf were
           -- a -93% Wotherspoon and a -84% Yeezy with not one size in stock,
           -- and leading with those says "this bot shows you things you cannot
           -- buy" to somebody who has just arrived.
           AND EXISTS (
                   SELECT 1 FROM price_points pp
                    WHERE pp.variant_id = o.variant_id AND pp.in_stock = 1
                      AND pp.ts = (SELECT MAX(ts) FROM price_points
                                    WHERE variant_id = o.variant_id)
               )
           AND (p.audience IS NULL OR p.audience <> 'kids')
           -- And the price has to be one somebody still stands behind. The
           -- digest is what a reader who has paid nothing sees of this product,
           -- and a run of it led with a find the shop had last confirmed seven
           -- days earlier. Showing a stale price to somebody deciding whether
           -- this is worth paying for undoes the one claim being made.
           --
           -- Measured on the shelf: 33,141 offers stand, 2,883 were confirmed
           -- within a day and 4,049 within two. Two days leaves room to pick
           -- two finds from different shops and different brands, and matches
           -- the ceiling the shelf itself is held to.
           AND o.checked_at >= datetime('now', ?)
         ORDER BY (o.reference_source = 'market') DESC, o.score DESC,
                  o.discount_pct DESC
         LIMIT {CANDIDATE_DEPTH}
        """,
        (FREE_READER, f"-{MAX_AGE_HOURS} hours"),
    ).fetchall()

    chosen: list[sqlite3.Row] = []
    shops: set[str] = set()
    brands: set[str] = set()
    for row in rows:
        brand = (row["brand_family"] or row["brand_norm"] or "").lower()
        if row["domain"] in shops or (brand and brand in brands):
            continue
        chosen.append(row)
        shops.add(row["domain"])
        if brand:
            brands.add(brand)
        if len(chosen) == count:
            break
    return chosen


def record(conn: sqlite3.Connection, row: sqlite3.Row, ts: str) -> bool:
    """File a published find under the digest's reader. False if it was there.

    Claimed before the message goes out, exactly as the run does it: a crash
    between the insert and Telegram costs one find, while the other order costs
    the reader the same find every day until somebody notices.

    `sent` is 1 and the bucket is the offer's own price bucket, so the row is
    the same shape as every other alert and the UNIQUE constraint still does its
    job against a second process running the digest at the same moment.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alerts
            (product_id, variant_id, ts, price_usd, price_bucket, discount_pct,
             score, sent, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            row["product_id"], row["variant_id"], ts, row["price_usd"],
            int(row["price_usd"]), row["discount_pct"], row["score"], FREE_READER,
        ),
    )
    return cur.rowcount > 0


def unrecord(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Give a claimed find back, for when Telegram refused to carry it."""
    conn.execute(
        "DELETE FROM alerts WHERE user_id = ? AND product_id = ? AND price_bucket = ?",
        (FREE_READER, row["product_id"], int(row["price_usd"])),
    )


def readers(conn: sqlite3.Connection, subscription: bool = False) -> list[sqlite3.Row]:
    """Everyone the digest is for: reachable, and not paying for the real feed.

    A reader inside the grace period is deliberately excluded. Their feed is
    still running — that is what grace is — and adding the advertisement for
    the thing they already have would read as a demotion rather than an offer.

    With `subscription` off the list is empty on purpose. The digest is the
    product advertising itself to people who do not have it; when nothing is
    being sold, everybody already has it and there is nobody to advertise to.
    See Config.subscription and docs/subscription.md.
    """
    rows = conn.execute(
        "SELECT * FROM bot_users WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [
        row for row in rows
        if subscription and dbm.subscription_state(conn, row["id"]) == "free"
    ]
