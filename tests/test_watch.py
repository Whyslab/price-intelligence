"""What somebody following a product hears besides a price drop."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pi import db as dbm
from pi import personal, watch

from .conftest import ts


def _reader(user_id: int, sizes: tuple[str, ...] = ()) -> personal.Subscriber:
    return personal.Subscriber(
        user_id=user_id, chat_id=str(user_id),
        reader=personal.Reader(sizes=frozenset(sizes)), label=f"reader{user_id}",
    )


def _shoe(conn, sizes=("US9", "US10")):
    """A product with one variant per size, every size last seen sold out."""
    store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD",
                             name="Shop", last_ok=ts(0))
    product = dbm.upsert_product(conn, store, "p1", "Air Thing", "/products/air-thing",
                                 brand="Nike", image_url="https://img.example/a.jpg")
    variants = {}
    for size in sizes:
        variant = dbm.upsert_variant(conn, product, size, size=size, size_norm=size)
        dbm.record_price(conn, variant, 120.0, None, False, "USD", 120.0, 1.0, ts=ts(2))
        variants[size] = variant
    return product, variants


def _restock(conn, variant) -> list[int]:
    back: list[int] = []
    dbm.record_price(conn, variant, 120.0, None, True, "USD", 120.0, 1.0, ts=ts(0),
                     restocked=back)
    return back


class TestASizeComesBack:
    def test_the_moment_is_noticed_when_the_point_is_written(self, conn):
        _, variants = _shoe(conn)
        assert _restock(conn, variants["US10"]) == [variants["US10"]]
        assert _restock(conn, variants["US10"]) == [], "still in stock is not coming back"

    def test_somebody_following_it_hears(self, conn):
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        back = _restock(conn, variants["US10"])

        notices = watch.restock_notices(conn, back, [_reader(7)])

        assert [(n.user_id, n.product_id, n.kind) for n in notices] == [(7, product, "restock")]
        text = notices[0].text
        assert "Снова в наличии" in text and "US10" in text
        assert "https://shop.example/products/air-thing" in text, "the link opens"

    def test_a_size_they_do_not_take_is_not_what_they_waited_for(self, conn):
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        back = _restock(conn, variants["US9"])

        assert watch.restock_notices(conn, back, [_reader(7, sizes=("US10",))]) == []

    def test_a_star_on_one_size_follows_that_size(self, conn):
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product, variant_id=variants["US9"])
        back = _restock(conn, variants["US10"])

        assert watch.restock_notices(conn, back, [_reader(7)]) == []

    def test_somebody_who_does_not_follow_it_hears_nothing(self, conn):
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        back = _restock(conn, variants["US10"])

        assert watch.restock_notices(conn, back, [_reader(9)]) == []

    def test_a_size_that_flickers_is_announced_once_a_day(self, conn):
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        back = _restock(conn, variants["US10"])
        [notice] = watch.restock_notices(conn, back, [_reader(7)])
        watch.mark_told(conn, notice, ts(0))

        assert watch.restock_notices(conn, back, [_reader(7)]) == []
        later = datetime.now(UTC) + timedelta(hours=25)
        assert watch.restock_notices(conn, back, [_reader(7)], now=later), "a day on, it may"


class TestTheShopTakesItDown:
    def test_a_follower_is_told_once_per_disappearance(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(0))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert notice.kind == "gone" and "Снято с продажи" in notice.text
        assert "14 дн" in notice.text

        watch.mark_told(conn, notice, dbm.utcnow())
        assert watch.gone_notices(conn, [_reader(7)], grace_days=14) == []

    def test_gone_again_after_coming_back_is_news_again(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(3))
        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        watch.mark_told(conn, notice, ts(2))

        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        dbm.mark_product_missing(conn, product, ts(0))

        assert len(watch.gone_notices(conn, [_reader(7)], grace_days=14)) == 1

    def test_nobody_who_is_not_a_reader_is_written_to(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(0))

        assert watch.gone_notices(conn, [_reader(9)], grace_days=14) == []


class TestItComesBack:
    def _told_it_went(self, conn, missing_since=None):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, missing_since or ts(1))
        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        watch.mark_told(conn, notice, dbm.utcnow())
        return product

    def test_somebody_told_it_went_hears_that_it_is_back(self, conn):
        product = self._told_it_went(conn)
        assert watch.returned_notices(conn, [_reader(7)]) == [], "still gone"

        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        [notice] = watch.returned_notices(conn, [_reader(7)])

        assert (notice.user_id, notice.product_id, notice.kind) == (7, product, "back")
        assert "Снова в продаже" in notice.text
        assert "https://shop.example/products/air-thing" in notice.text
        assert notice.image_url == "https://img.example/a.jpg"

    def test_the_price_is_the_cheapest_size_in_stock(self, conn):
        product = self._told_it_went(conn)
        variants = dict(conn.execute(
            "SELECT size, id FROM variants WHERE product_id = ?", (product,)
        ).fetchall())
        dbm.record_price(conn, variants["US10"], 95.0, None, True, "USD", 95.0, 1.0, ts=ts(0))
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        [notice] = watch.returned_notices(conn, [_reader(7)])
        assert "95" in notice.text and "120" not in notice.text, "the sold-out 120 is not the price"

    def test_heard_once_and_the_next_disappearance_is_news_again(self, conn):
        product = self._told_it_went(conn)
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        [notice] = watch.returned_notices(conn, [_reader(7)])
        watch.mark_told(conn, notice, dbm.utcnow())

        assert watch.returned_notices(conn, [_reader(7)]) == []
        dbm.mark_product_missing(conn, product, ts(0))
        assert len(watch.gone_notices(conn, [_reader(7)], grace_days=14)) == 1

    def test_nobody_is_told_it_is_back_who_was_not_told_it_went(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(1))
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        assert watch.returned_notices(conn, [_reader(7)]) == []

    def test_nobody_who_is_not_a_reader_is_written_to(self, conn):
        product = self._told_it_went(conn)
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        assert watch.returned_notices(conn, [_reader(9)]) == []


class TestTheFortnightLeft:
    def test_a_fresh_mark_has_the_whole_of_it(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(0))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert "ближайшие 14 дн" in notice.text

    def test_an_old_mark_has_what_is_left_of_it(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(10.5))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert "ближайшие 4 дн" in notice.text

    def test_never_less_than_a_day(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(30))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert "ближайшие 1 дн" in notice.text
