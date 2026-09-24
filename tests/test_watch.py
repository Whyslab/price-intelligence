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


def _in_stock(conn, product, size, price):
    """A fresh reading of one size, in stock at `price`."""
    (variant,) = conn.execute(
        "SELECT id FROM variants WHERE product_id = ? AND size = ?", (product, size)
    ).fetchone()
    dbm.record_price(conn, variant, price, None, True, "USD", price, 1.0, ts=ts(0))


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
        dbm.mark_product_missing(conn, product, ts(3))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert notice.kind == "gone" and "Снято с продажи" in notice.text
        assert "ближайшие 11 дн" in notice.text, "three of the fourteen days are gone"

        watch.mark_told(conn, notice, dbm.utcnow())
        assert watch.gone_notices(conn, [_reader(7)], grace_days=14) == []

    def test_gone_again_after_coming_back_is_news_again(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(5))
        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        watch.mark_told(conn, notice, ts(4))

        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        dbm.mark_product_missing(conn, product, ts(2.5))

        assert len(watch.gone_notices(conn, [_reader(7)], grace_days=14)) == 1

    def test_nobody_who_is_not_a_reader_is_written_to(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(3))

        assert watch.gone_notices(conn, [_reader(9)], grace_days=14) == []

    def test_a_product_missed_by_one_read_is_not_news(self, conn):
        """Review 24.09: a product a read skipped and the next read lists again
        cost a follower "снято" and "снова в продаже" for every flicker."""
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        sent = []
        for _ in range(3):
            dbm.mark_product_missing(conn, product, dbm.utcnow())
            for notice in watch.gone_notices(conn, [_reader(7)], grace_days=14):
                sent.append(notice.kind)
                watch.mark_told(conn, notice, dbm.utcnow())
            conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
            for notice in watch.returned_notices(conn, [_reader(7)]):
                sent.append(notice.kind)
                watch.mark_told(conn, notice, dbm.utcnow())

        assert sent == []

    def test_once_it_has_stayed_gone_it_is(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, dbm.utcnow())
        assert watch.gone_notices(conn, [_reader(7)], grace_days=14) == []

        later = datetime.now(UTC) + timedelta(hours=watch.GONE_NOTICE_AFTER_HOURS, minutes=1)
        assert len(watch.gone_notices(conn, [_reader(7)], grace_days=14, now=later)) == 1


class TestItComesBack:
    def _told_it_went(self, conn, missing_since=None):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, missing_since or ts(3))
        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        watch.mark_told(conn, notice, dbm.utcnow())
        return product

    def test_somebody_told_it_went_hears_that_it_is_back(self, conn):
        product = self._told_it_went(conn)
        assert watch.returned_notices(conn, [_reader(7)]) == [], "still gone"

        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        _in_stock(conn, product, "US10", 120.0)
        [notice] = watch.returned_notices(conn, [_reader(7)])

        assert (notice.user_id, notice.product_id, notice.kind) == (7, product, "back")
        assert "Снова в продаже" in notice.text
        assert "https://shop.example/products/air-thing" in notice.text
        assert notice.image_url == "https://img.example/a.jpg"

    def test_back_but_sold_out_is_not_called_on_sale(self, conn):
        product = self._told_it_went(conn)
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        [notice] = watch.returned_notices(conn, [_reader(7)])

        assert notice.kind == "back", "still news: it is no longer about to be deleted"
        assert "Снова в продаже" not in notice.text and "Снова в каталоге" in notice.text
        assert "$" not in notice.text, "no price for something nobody can buy"

    def test_back_in_another_size_says_theirs_is_still_sold_out(self, conn):
        """Review 24.09: a reader following US10 was told "снова в продаже · $90"
        — the price of a US9 — with US10 still sold out."""
        product, variants = _shoe(conn)
        dbm.add_favorite(conn, 7, product, variant_id=variants["US10"])
        dbm.mark_product_missing(conn, product, ts(3))
        [gone] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        watch.mark_told(conn, gone, dbm.utcnow())
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        _in_stock(conn, product, "US9", 90.0)

        [notice] = watch.returned_notices(conn, [_reader(7)])

        assert "Снова в продаже" not in notice.text
        assert "US10" in notice.text and "$90" not in notice.text

    def test_back_in_their_size_names_it_and_its_price(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(3))
        [gone] = watch.gone_notices(conn, [_reader(7, sizes=("US10",))], grace_days=14)
        watch.mark_told(conn, gone, dbm.utcnow())
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        _in_stock(conn, product, "US9", 90.0)
        _in_stock(conn, product, "US10", 125.0)

        [notice] = watch.returned_notices(conn, [_reader(7, sizes=("US10",))])

        assert "Снова в продаже" in notice.text and "Размер: US10" in notice.text
        assert "$125" in notice.text and "$90" not in notice.text

    def test_the_price_is_the_cheapest_size_in_stock(self, conn):
        product = self._told_it_went(conn)
        _in_stock(conn, product, "US10", 95.0)
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        [notice] = watch.returned_notices(conn, [_reader(7)])
        assert "95" in notice.text and "120" not in notice.text, "the sold-out 120 is not the price"

    def test_heard_once_and_the_next_disappearance_is_news_again(self, conn):
        product = self._told_it_went(conn)
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))
        [notice] = watch.returned_notices(conn, [_reader(7)])
        watch.mark_told(conn, notice, dbm.utcnow())

        assert watch.returned_notices(conn, [_reader(7)]) == []
        dbm.mark_product_missing(conn, product, ts(2.5))
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
    def test_a_mark_two_days_old_has_the_rest_of_it(self, conn):
        product, _ = _shoe(conn)
        dbm.add_favorite(conn, 7, product)
        dbm.mark_product_missing(conn, product, ts(2.2))

        [notice] = watch.gone_notices(conn, [_reader(7)], grace_days=14)
        assert "ближайшие 12 дн" in notice.text

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


class TestSizesAboutSomethingElse:
    """Review 24.09: shoe sizes on a reader following a hoodie matched none of
    its sizes, so its S coming back was never told and it coming back on sale
    was called "нет в наличии" while S and M were there."""

    @staticmethod
    def _hoodie(conn, in_stock=True):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD",
                                 name="Shop", last_ok=ts(0))
        product = dbm.upsert_product(conn, store, "h1", "Hoodie", "/products/hoodie",
                                     brand="Stussy")
        variants = {}
        for size in ("S", "M"):
            variant = dbm.upsert_variant(conn, product, size, size=size, size_norm=size)
            dbm.record_price(conn, variant, 90.0, None, in_stock, "USD", 90.0, 1.0, ts=ts(3))
            variants[size] = variant
        return product, variants

    def test_back_on_sale_is_on_sale(self, conn):
        product, _ = self._hoodie(conn)
        dbm.add_favorite(conn, 7, product)
        shoe_sizes = _reader(7, sizes=("US10",))
        dbm.mark_product_missing(conn, product, ts(3))
        [gone] = watch.gone_notices(conn, [shoe_sizes], grace_days=14)
        watch.mark_told(conn, gone, dbm.utcnow())
        conn.execute("UPDATE products SET missing_since = NULL WHERE id = ?", (product,))

        [back] = watch.returned_notices(conn, [shoe_sizes])

        assert "Снова в продаже" in back.text and "$90" in back.text
        assert "нет в наличии" not in back.text.lower()

    def test_a_size_coming_back_is_told(self, conn):
        product, variants = self._hoodie(conn, in_stock=False)
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["S"])

        assert len(watch.restock_notices(conn, back, [_reader(7, sizes=("US10",))])) == 1

    def test_sizes_that_do_fit_still_filter(self, conn):
        product, variants = self._hoodie(conn, in_stock=False)
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["S"])

        assert watch.restock_notices(conn, back, [_reader(7, sizes=("M",))]) == []


class TestSizesOfTheSameFamily:
    """Review 24.09 (fifth pass): EU46 on a shoe made in EU40–43 was taken as
    sizes about another kind of thing, and its EU42 coming back was told."""

    @staticmethod
    def _shoe_in(conn, sizes, kind="shoes"):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD",
                                 name="Shop", last_ok=ts(0))
        product = dbm.upsert_product(conn, store, "s1", "Runner", "/products/runner")
        conn.execute("UPDATE products SET kind = ? WHERE id = ?", (kind, product))
        variants = {}
        for size in sizes:
            variant = dbm.upsert_variant(conn, product, size, size=size, size_norm=size)
            dbm.record_price(conn, variant, 100.0, None, False, "USD", 100.0, 1.0, ts=ts(3))
            variants[size] = variant
        return product, variants

    def test_a_shoe_not_made_in_their_size_stays_quiet(self, conn):
        product, variants = self._shoe_in(conn, ("EU40", "EU41", "EU42", "EU43"))
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["EU42"])

        assert watch.restock_notices(conn, back, [_reader(7, sizes=("EU46",))]) == []

    def test_their_own_size_in_another_system_is_still_the_shoe_family(self, conn):
        product, variants = self._shoe_in(conn, ("US9", "US10"))
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["US9"])

        assert watch.restock_notices(conn, back, [_reader(7, sizes=("EU44",))]) == [], (
            "shoe sizes speak about a shoe; no guessing EU44 is US9"
        )

    def test_a_waist_size_is_not_a_shoe_size(self, conn):
        """Review 24.09 (sixth pass): a bare 32 is normalised to US32, so shoe
        sizes on a reader silenced trousers the way they had silenced hoodies."""
        product, variants = self._shoe_in(conn, ("US30", "US32", "US34"), kind="clothing")
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["US32"])

        assert len(watch.restock_notices(conn, back, [_reader(7, sizes=("EU44",))])) == 1

    def test_letter_sizes_on_a_reader_speak_about_a_hoodie(self, conn):
        product, variants = self._shoe_in(conn, ("S", "M", "L"), kind="clothing")
        dbm.add_favorite(conn, 7, product)

        back = _restock(conn, variants["S"])

        assert watch.restock_notices(conn, back, [_reader(7, sizes=("EU44", "L"))]) == []
        assert len(watch.restock_notices(conn, back, [_reader(7, sizes=("EU44",))])) == 1
