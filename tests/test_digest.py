"""The free tier's two finds a day: what is chosen, and what is never repeated."""
from __future__ import annotations

import sqlite3

from pi import db as dbm
from pi import digest

from .conftest import ts


def _offer(
    conn: sqlite3.Connection,
    domain: str,
    brand: str,
    score: int,
    *,
    source: str = "history",
    in_stock: bool = True,
    audience: str | None = None,
    price: float = 100.0,
    checked_at: str | None = None,
) -> int:
    """One product on the shelf, with its latest price point saying availability."""
    store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
    product = dbm.upsert_product(
        conn, store, f"{domain}-{brand}-{score}", f"{brand} shoe",
        f"https://{domain}/p", brand=brand,
    )
    # brand_family and audience are the classifier's columns, not the collector's
    # — `pi reclassify` fills them, so a fixture sets them the same way.
    conn.execute(
        "UPDATE products SET brand_family = ?, audience = ? WHERE id = ?",
        (brand, audience, product),
    )
    variant = dbm.upsert_variant(
        conn, product, f"v-{score}", sku=f"SKU{score}", size="US 10", size_norm="US10"
    )
    dbm.record_price(
        conn, variant, price_usd=price, compare_at_usd=price * 2, in_stock=in_stock,
        currency="USD", price_native=price, fx_rate=1.0, ts=ts(0),
    )
    conn.execute(
        """
        INSERT INTO offers (variant_id, product_id, found_at, checked_at, price_usd,
                            reference_usd, reference_source, discount_pct, saving_usd,
                            score, all_time_low)
        VALUES (?, ?, ?, ?, ?, ?, ?, 50.0, 100.0, ?, 0)
        """,
        (variant, product, ts(1), checked_at or ts(0), price, price * 2, source, score),
    )
    return product


def test_the_best_two_are_chosen(conn):
    _offer(conn, "a.example", "Nike", score=90)
    best = _offer(conn, "b.example", "Adidas", score=99)
    _offer(conn, "c.example", "Puma", score=50)
    picked = digest.pick(conn, count=2)
    assert len(picked) == 2
    assert picked[0]["product_id"] == best


def test_a_market_reference_outranks_a_higher_score(conn):
    _offer(conn, "tagged.example", "Nike", score=100, source="tag")
    market = _offer(conn, "market.example", "Adidas", score=60, source="market")
    picked = digest.pick(conn, count=1)
    # "Cheaper than six other shops" is the one claim a free channel cannot
    # make, so it leads even when a shop's own label scores better.
    assert picked[0]["product_id"] == market


def test_never_two_from_the_same_shop(conn):
    _offer(conn, "one.example", "Nike", score=99)
    _offer(conn, "one.example", "Adidas", score=98)
    _offer(conn, "two.example", "Puma", score=10)
    picked = digest.pick(conn, count=2)
    assert {row["domain"] for row in picked} == {"one.example", "two.example"}


def test_never_two_of_the_same_brand(conn):
    _offer(conn, "one.example", "Nike", score=99)
    _offer(conn, "two.example", "Nike", score=98)
    _offer(conn, "three.example", "Puma", score=10)
    picked = digest.pick(conn, count=2)
    assert {(row["brand_family"] or "") for row in picked} == {"Nike", "Puma"}


def test_out_of_stock_is_not_advertised(conn):
    _offer(conn, "gone.example", "Nike", score=100, in_stock=False)
    keep = _offer(conn, "here.example", "Adidas", score=10)
    picked = digest.pick(conn, count=2)
    assert [row["product_id"] for row in picked] == [keep]


def test_children_stay_off_the_free_shelf_too(conn):
    _offer(conn, "kids.example", "Nike", score=100, audience="kids")
    keep = _offer(conn, "grown.example", "Adidas", score=10)
    picked = digest.pick(conn, count=2)
    assert [row["product_id"] for row in picked] == [keep]


def test_what_was_published_is_not_published_again(conn):
    first = _offer(conn, "one.example", "Nike", score=99)
    _offer(conn, "two.example", "Adidas", score=98)
    picked = digest.pick(conn, count=1)
    assert picked[0]["product_id"] == first
    digest.record(conn, picked[0], dbm.utcnow())

    again = digest.pick(conn, count=1)
    assert again[0]["product_id"] != first


def test_recording_twice_is_refused(conn):
    _offer(conn, "one.example", "Nike", score=99)
    row = digest.pick(conn, count=1)[0]
    assert digest.record(conn, row, dbm.utcnow()) is True
    assert digest.record(conn, row, dbm.utcnow()) is False


def test_a_find_given_back_can_be_published_later(conn):
    product = _offer(conn, "one.example", "Nike", score=99)
    row = digest.pick(conn, count=1)[0]
    digest.record(conn, row, dbm.utcnow())
    digest.unrecord(conn, row)
    # Telegram refused to carry it; nobody saw it, so it is still unpublished.
    assert digest.pick(conn, count=1)[0]["product_id"] == product


def test_the_digest_does_not_close_a_find_for_a_paying_reader(conn):
    _offer(conn, "one.example", "Nike", score=99)
    row = digest.pick(conn, count=1)[0]
    digest.record(conn, row, dbm.utcnow())
    # Rows carrying user 0 mean "everybody"; the digest's own must not, or
    # publishing an advertisement would silence the product people pay for.
    filed = conn.execute(
        "SELECT user_id FROM alerts WHERE product_id = ?", (row["product_id"],)
    ).fetchall()
    assert [r["user_id"] for r in filed] == [digest.FREE_READER]
    assert digest.FREE_READER != 0


def test_readers_are_the_ones_not_paying(conn):
    for user_id in (1, 2, 3, 4):
        dbm.upsert_bot_user(conn, user_id, chat_id=str(user_id))
    dbm.grant(conn, 2, days=30)
    dbm.grant(conn, 3, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 3", (ts(1),))  # grace
    conn.execute("UPDATE bot_users SET active = 0 WHERE id = 4")

    # Selling is off by default; the digest only has an audience when there
    # is something the audience has not bought.
    audience = [row["id"] for row in digest.readers(conn, subscription=True)]
    # 2 pays and gets the real feed; 3 is in grace and still gets it; 4 left.
    assert audience == [1]


class TestTheDigestDoesNotAdvertiseAStalePrice:
    """This is what somebody who has paid nothing sees of the product. A run of
    it led with a find the shop had last confirmed seven days earlier — showing
    a stale price to the person deciding whether this is worth paying for undoes
    the one claim being made."""

    def test_a_price_confirmed_today_is_offered(self, conn):
        _offer(conn, "a.example", "Nike", score=90, checked_at=ts(0))
        assert len(digest.pick(conn, count=2)) == 1

    def test_a_price_nobody_has_confirmed_for_a_week_is_not(self, conn):
        _offer(conn, "a.example", "Nike", score=99, checked_at=ts(7))
        assert digest.pick(conn, count=2) == []

    def test_the_line_is_the_same_one_the_shelf_is_held_to(self, conn):
        """48 hours, and measured: 4,049 of the shelf's 33,141 offers clear it,
        which is room enough to pick two from different shops."""
        _offer(conn, "a.example", "Nike", score=90, checked_at=ts(1))
        _offer(conn, "b.example", "Adidas", score=99, checked_at=ts(3))
        picked = digest.pick(conn, count=2)
        assert [row["domain"] for row in picked] == ["a.example"]

    def test_the_line_is_forty_eight_hours_to_the_hour(self, conn):
        """Compared against datetime('now'), whose space sorts below the
        column's 'T', every price from the cutoff's own day passed: a price 60
        hours old was advertised whenever it fell on that calendar day."""
        _offer(conn, "a.example", "Nike", score=90, checked_at=ts(47 / 24))
        _offer(conn, "b.example", "Adidas", score=99, checked_at=ts(49 / 24))
        assert [row["domain"] for row in digest.pick(conn, count=2)] == ["a.example"]


class TestTheDigestAdvertisesOnlyWhatSomebodyElseVouchesFor:
    """The bot and the shelf both hold back a discount that rests on nothing but
    the shop's own struck-through price. The free digest is the product's
    advertisement, and it led with exactly that."""

    def test_a_find_resting_on_the_shops_own_tag_is_not_advertised(self, conn):
        _offer(conn, "a.example", "Nike", score=99, source="tag")
        assert digest.pick(conn, count=2) == []

    def test_an_all_time_low_counts_even_on_a_tag(self, conn):
        product = _offer(conn, "a.example", "Nike", score=99, source="tag")
        conn.execute("UPDATE offers SET all_time_low = 1 WHERE product_id = ?", (product,))
        assert [row["product_id"] for row in digest.pick(conn, count=2)] == [product]
