"""The shelf as a page.

Telegram is right for news and wrong for browsing, and the numbers say so: of
the 20,934 discounts standing on this database, 665 were ever announced. The
rest are not stale — they are simply not new, which is a different thing and
not a reason to hide them.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
import respx

from pi import db as dbm
from pi import web

from .conftest import ts
from .test_webauth import TOKEN, signed


def a_shelf(conn, n=3):
    """n offers, each in its own shop, cheapest last."""
    for i in range(n):
        store = dbm.upsert_store(conn, f"shop{i}.example", platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, f"p{i}", f"Nike Air Max {i}", f"https://shop{i}.example/p",
            brand="Nike", image_url="https://img.example/x.jpg",
        )
        conn.execute(
            "UPDATE products SET kind = 'shoes', gender = 'men', brand_family = 'Nike'"
            " WHERE id = ?", (product,),
        )
        variant = dbm.upsert_variant(
            conn, product, f"v{i}", size="US 10", size_norm="US10"
        )
        conn.execute(
            """
            INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                price_usd, reference_usd, reference_source,
                                discount_pct, saving_usd, score, all_time_low)
            VALUES (?, ?, ?, ?, ?, ?, 'market', ?, ?, ?, 0)
            """,
            (variant, product, ts(1), ts(0), 100.0 - i, 200.0, 50.0 + i, 100.0, 90 - i),
        )


def a_childs_offer(conn):
    """One more offer on the shelf, this one for a child."""
    store = dbm.upsert_store(conn, "kidshop.example", platform="shopify", currency="USD")
    product = dbm.upsert_product(
        conn, store, "k1", "Nike Dunk Low (GS)", "https://kidshop.example/p",
        brand="Nike", image_url="https://img.example/k.jpg",
    )
    conn.execute(
        "UPDATE products SET kind = 'shoes', brand_family = 'Nike', audience = 'kids'"
        " WHERE id = ?", (product,),
    )
    variant = dbm.upsert_variant(conn, product, "kv1", size="5Y", size_norm="US5")
    conn.execute(
        """
        INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                            price_usd, reference_usd, reference_source,
                            discount_pct, saving_usd, score, all_time_low)
        VALUES (?, ?, ?, ?, 40.0, 100.0, 'market', 60.0, 60.0, 99, 0)
        """,
        (variant, product, ts(1), ts(0)),
    )
    return product


class TestChildrensClothing:
    """Hidden by default, and reachable — a hidden misreading is unreportable."""

    def test_the_shelf_leaves_it_out_unless_asked(self, conn):
        a_shelf(conn, 3)
        a_childs_offer(conn)
        rows, total = dbm.offers_for(conn)
        assert total == 3
        assert all("(GS)" not in row["title"] for row in rows)

    def test_asking_puts_it_back(self, conn):
        a_shelf(conn, 3)
        a_childs_offer(conn)
        rows, total = dbm.offers_for(conn, kids=True)
        assert total == 4
        assert any("(GS)" in row["title"] for row in rows)

    def test_the_row_is_written_either_way(self, conn):
        """It is filtered on read, so a rerun of the classifier can undo it."""
        a_shelf(conn, 1)
        product = a_childs_offer(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM offers WHERE product_id = ?", (product,)
        ).fetchone()[0] == 1

    def test_the_counts_follow_what_the_page_will_show(self, conn):
        """A facet count is a promise about what a click returns."""
        a_shelf(conn, 3)
        a_childs_offer(conn)
        assert dbm.shelf_facets(conn)["total"] == 3
        assert dbm.shelf_facets(conn, kids=True)["total"] == 4
        shoes = {f["value"]: f["count"] for f in dbm.shelf_facets(conn)["kinds"]}
        assert shoes["shoes"] == 3

    def test_the_query_reads_the_switch(self):
        assert web.read_query("")["kids"] is False
        assert web.read_query("kids=1")["kids"] is True
        assert web.read_query("kids=0")["kids"] is False
        assert web.read_query("kids=nonsense")["kids"] is False


class TestReadingTheQuery:
    """Everything that can go wrong with a URL goes wrong here."""

    def test_nothing_asked_for_means_the_whole_shelf(self):
        args = web.read_query("")
        assert args["kinds"] == [] and args["sizes"] == []
        assert args["sort"] == "score"
        assert args["page"] == 0

    def test_a_filter_may_repeat_or_be_comma_separated(self):
        assert web.read_query("size=EU44&size=EU45")["sizes"] == ["EU44", "EU45"]
        assert web.read_query("size=EU44,EU45")["sizes"] == ["EU44", "EU45"]

    def test_sizes_are_matched_the_way_the_catalogue_stores_them(self):
        assert web.read_query("size=eu44")["sizes"] == ["EU44"]

    def test_an_unknown_sort_falls_back_rather_than_reaching_the_sql(self):
        """The sort lands in an ORDER BY, so only the map may choose it."""
        assert web.read_query("sort=; DROP TABLE offers")["sort"] == "score"
        assert web.read_query("sort=discount")["sort"] == "discount"

    def test_a_link_says_which_size_it_was_opened_from(self):
        assert web.read_variant("variant=4210") == 4210
        assert web.read_variant("") is None
        assert web.read_variant("variant=") is None
        assert web.read_variant("variant=; DROP TABLE offers") is None

    def test_nonsense_paging_does_not_get_through(self):
        assert web.read_query("page=-5")["page"] == 0
        assert web.read_query("page=abc")["page"] == 0
        assert web.read_query("limit=99999")["limit"] == web.MAX_PAGE_SIZE
        assert web.read_query("limit=0")["limit"] == 1


class TestTheShelfPage:
    def test_it_returns_offers_and_says_how_many_there_are(self, conn):
        a_shelf(conn, 3)
        page = web.shelf_page(conn, web.read_query(""))
        assert page["total"] == 3
        assert len(page["offers"]) == 3

    def test_every_card_says_when_the_shop_last_confirmed_it(self, conn):
        """The difference between a shelf and a graveyard."""
        a_shelf(conn, 1)
        card = web.shelf_page(conn, web.read_query(""))["offers"][0]
        assert card["checked_at"]
        assert card["price"] and card["was"] and card["discount"]

    def test_a_filter_narrows_it(self, conn):
        a_shelf(conn, 3)
        assert web.shelf_page(conn, web.read_query("size=US10"))["total"] == 3
        assert web.shelf_page(conn, web.read_query("size=EU99"))["total"] == 0
        assert web.shelf_page(conn, web.read_query("kind=shoes"))["total"] == 3
        assert web.shelf_page(conn, web.read_query("kind=accessories"))["total"] == 0

    def test_search_looks_at_the_title_and_the_brand(self, conn):
        a_shelf(conn, 2)
        assert web.shelf_page(conn, web.read_query("q=air max"))["total"] == 2
        assert web.shelf_page(conn, web.read_query("q=nike"))["total"] == 2
        assert web.shelf_page(conn, web.read_query("q=birkenstock"))["total"] == 0

    def test_sorting_is_by_what_was_asked_for(self, conn):
        a_shelf(conn, 3)
        by_price = web.shelf_page(conn, web.read_query("sort=cheapest"))["offers"]
        assert [o["price"] for o in by_price] == sorted(o["price"] for o in by_price)

    def test_paging_walks_the_whole_shelf_without_repeating(self, conn):
        a_shelf(conn, 3)
        seen = []
        for page in range(3):
            got = web.shelf_page(conn, web.read_query(f"limit=1&page={page}"))
            assert got["pages"] == 3
            seen += [o["url"] + str(o["price"]) for o in got["offers"]]
        assert len(set(seen)) == 3


class TestTheSeededPage:
    def test_the_first_screenful_arrives_inside_the_html(self, conn):
        """Two round trips before anything appears is the whole impression."""
        a_shelf(conn, 2)
        html = web.render_page(conn, web.read_query("")).decode("utf-8")
        start = html.index('id="seed"')
        blob = html[html.index(">", start) + 1 : html.index("</script>", start)]
        seed = json.loads(blob.replace("<\\/", "</"))["seed"]
        assert seed["offers"]["total"] == 2
        assert seed["facets"]["total"] == 2

    def test_a_title_that_could_close_the_script_tag_does_not(self, conn):
        """A product called `</script>` would otherwise end the page there."""
        a_shelf(conn, 1)
        conn.execute("UPDATE products SET title = '</script><b>x'")
        html = web.render_page(conn, web.read_query("")).decode("utf-8")
        assert "</script><b>x" not in html
        assert '<\\/script>' in html


class TestOneProductAndEveryoneElse:
    """The comparison is the point: 82% of the shelf has nobody to compare with."""

    _seq = 0

    @classmethod
    def _sell(cls, conn, domain, sku, price, on_shelf=False, title="Yeezy Slide Azure"):
        """One shop's listing of an article. Its own row even in a shop that
        already has one, because a shop listing the same shoe twice is exactly
        the case that must not count as corroboration."""
        cls._seq += 1
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, f"ext{cls._seq}", title, f"https://{domain}/p{cls._seq}",
            brand="adidas",
        )
        dbm.set_product_keys(conn, product, {("sku", sku)})
        variant = dbm.upsert_variant(conn, product, f"v{cls._seq}", sku=sku, size_norm="US10")
        dbm.record_price(
            conn, variant, price, None, True, "USD", price, 1.0, ts=dbm.utcnow()
        )
        if on_shelf:
            conn.execute(
                """
                INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                    price_usd, reference_usd, reference_source,
                                    discount_pct, saving_usd, score, all_time_low)
                VALUES (?, ?, ?, ?, ?, ?, 'market', 47.0, 70.0, 80, 0)
                """,
                (variant, product, ts(1), ts(0), price, 150.0),
            )
        return product

    def test_it_lists_every_other_shop_holding_the_article(self, conn):
        mine = self._sell(conn, "ours.example", "GX6138", 80.0, on_shelf=True)
        self._sell(conn, "pricey.example", "GX6138", 170.0)
        self._sell(conn, "middle.example", "GX6138", 125.0)

        page = web.product_page(conn, mine)
        assert [o["price"] for o in page["elsewhere"]] == [125.0, 170.0]
        assert page["cheapest_elsewhere"] is None, "nobody beats us"

    def test_it_says_so_when_somebody_is_cheaper(self, conn):
        """A page that only ever flatters what it is showing is an advertisement."""
        mine = self._sell(conn, "ours.example", "IF4396", 146.97, on_shelf=True)
        self._sell(conn, "cheaper.example", "IF4396", 105.0)

        page = web.product_page(conn, mine)
        assert page["cheapest_elsewhere"]["price"] == 105.0
        assert page["cheapest_elsewhere"]["domain"] == "cheaper.example"

    def test_the_same_merchant_in_two_countries_appears_once(self, conn):
        mine = self._sell(conn, "ours.example", "GX6138", 80.0, on_shelf=True)
        self._sell(conn, "chain.com", "GX6138", 150.0)
        self._sell(conn, "chain.de", "GX6138", 140.0)

        page = web.product_page(conn, mine)
        assert len(page["elsewhere"]) == 1
        assert page["elsewhere"][0]["price"] == 140.0, "the merchant's cheaper one"

    def test_our_own_shop_is_never_evidence_about_itself(self, conn):
        mine = self._sell(conn, "ours.example", "GX6138", 80.0, on_shelf=True)
        self._sell(conn, "ours.example", "GX6138", 200.0, title="Yeezy Slide Azure 2")

        assert web.product_page(conn, mine)["elsewhere"] == []

    def test_a_junk_key_does_not_drag_in_unrelated_things(self, conn):
        """`DIME` is a brand name in a SKU field, shared by 413 products."""
        mine = self._sell(conn, "ours.example", "DIME", 80.0, on_shelf=True)
        for n in range(dbm.MAX_KEY_FANOUT + 2):
            self._sell(conn, f"other{n}.example", "DIME", 20.0 + n, title=f"Thing {n}")

        assert web.product_page(conn, mine)["elsewhere"] == []

    def test_something_out_of_stock_elsewhere_is_not_a_price(self, conn):
        mine = self._sell(conn, "ours.example", "GX6138", 80.0, on_shelf=True)
        gone = self._sell(conn, "sold-out.example", "GX6138", 40.0)
        conn.execute(
            "UPDATE price_points SET in_stock = 0 WHERE variant_id IN"
            " (SELECT id FROM variants WHERE product_id = ?)", (gone,),
        )

        assert web.product_page(conn, mine)["elsewhere"] == []

    def test_a_product_that_left_the_shelf_is_not_found(self, conn):
        assert web.product_page(conn, 999999) == {}


class TestFacets:
    def test_filters_are_offered_only_for_what_is_actually_on_offer(self, conn):
        """A filter that returns an empty page is worse than no filter."""
        a_shelf(conn, 2)
        facets = dbm.shelf_facets(conn)
        assert facets["total"] == 2
        assert facets["kinds"] == [{"value": "shoes", "count": 2}]
        assert facets["sizes"] == [{"value": "US10", "count": 2}]
        assert {f["value"] for f in facets["brands"]} == {"Nike"}

    def test_an_empty_shelf_offers_nothing_rather_than_failing(self, conn):
        facets = dbm.shelf_facets(conn)
        assert facets["total"] == 0
        assert facets["kinds"] == []


class TestNarrowingByPriceAndDiscount:
    """The two things a person narrows by before anything else — "nothing over
    two hundred", "only real cuts" — and the two the shelf could not express."""

    def test_a_number_that_is_not_one_is_no_opinion_rather_than_zero(self):
        """Read as zero, an unreadable ceiling would empty the shelf silently."""
        assert web.read_query("")["min_price"] is None
        assert web.read_query("max_price=")["max_price"] is None
        assert web.read_query("max_price=abc")["max_price"] is None
        assert web.read_query("min_price=-5")["min_price"] is None
        assert web.read_query("max_price=199,90")["max_price"] == 199.90
        assert web.read_query("min_discount=50")["min_discount"] == 50.0

    def test_a_ceiling_leaves_out_what_costs_more(self, conn):
        a_shelf(conn, 3)  # $100, $99, $98
        assert web.shelf_page(conn, web.read_query("max_price=99"))["total"] == 2
        assert web.shelf_page(conn, web.read_query("min_price=99"))["total"] == 2
        assert web.shelf_page(conn, web.read_query("min_price=99&max_price=99"))["total"] == 1

    def test_a_floor_under_the_discount_leaves_out_the_shallow_ones(self, conn):
        a_shelf(conn, 3)  # 50%, 51%, 52%
        assert web.shelf_page(conn, web.read_query("min_discount=51"))["total"] == 2
        assert web.shelf_page(conn, web.read_query("min_discount=90"))["total"] == 0

    def test_the_narrowing_survives_being_combined(self, conn):
        a_shelf(conn, 3)
        page = web.shelf_page(conn, web.read_query("max_price=99&min_discount=52&kind=shoes"))
        assert page["total"] == 1


class TestAProductOnSaleInSeveralSizes:
    """`offers` is keyed by variant, so one product can hold several rows: 322
    of the 23,934 on the shelf do, and 188 of those at prices that differ
    between sizes — one of them $90 in one size against $180 in another."""

    @staticmethod
    def _two_sizes(conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, "p", "Salomon XT-6", "https://shop.example/p", brand="Salomon",
        )
        variants = {}
        for size, price, discount, score in (("US10", 90.0, 69.0, 70), ("US11", 180.0, 37.0, 40)):
            variant = dbm.upsert_variant(conn, product, f"v{size}", size=size, size_norm=size)
            conn.execute(
                """
                INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                    price_usd, reference_usd, reference_source,
                                    discount_pct, saving_usd, score, all_time_low)
                VALUES (?, ?, ?, ?, ?, 290.0, 'market', ?, ?, ?, 0)
                """,
                (variant, product, ts(1), ts(0), price, discount, 290.0 - price, score),
            )
            variants[size] = variant
        return product, variants

    def test_the_card_opens_on_the_size_it_was_built_from(self, conn):
        """A card showing $180 opening onto $90 is the fastest way to be disbelieved."""
        product, variants = self._two_sizes(conn)
        assert web.product_page(conn, product, variants["US11"])["ours"]["price"] == 180.0
        assert web.product_page(conn, product, variants["US10"])["ours"]["price"] == 90.0

    def test_every_card_on_the_shelf_can_say_which_row_it_is(self, conn):
        self._two_sizes(conn)
        cards = web.shelf_page(conn, web.read_query(""))["offers"]
        assert len(cards) == 2
        for card in cards:
            opened = web.product_page(conn, card["id"], card["variant"])
            assert opened["ours"]["price"] == card["price"]
            assert opened["ours"]["size"] == card["size"]

    def test_a_link_without_a_size_is_at_least_the_same_row_every_time(self, conn):
        """Before, it was whichever row the database happened to return."""
        product, _ = self._two_sizes(conn)
        assert {web.product_page(conn, product)["ours"]["price"] for _ in range(5)} == {90.0}

    def test_a_size_that_left_the_shelf_falls_back_rather_than_failing(self, conn):
        """The shop sold out of it between the page loading and the click."""
        product, variants = self._two_sizes(conn)
        conn.execute("DELETE FROM offers WHERE variant_id = ?", (variants["US11"],))
        assert web.product_page(conn, product, variants["US11"])["ours"]["price"] == 90.0


class TestWhatTheControlsAreBuiltFrom:
    def test_the_range_control_reaches_both_ends_of_the_shelf(self, conn):
        """A slider that cannot reach the cheapest thing on the shelf is a bug
        people report as missing stock."""
        a_shelf(conn, 3)
        assert dbm.shelf_facets(conn)["price"] == {"min": 98.0, "max": 100.0}

    def test_an_empty_shelf_has_ends_rather_than_nothing(self, conn):
        assert dbm.shelf_facets(conn)["price"] == {"min": 0, "max": 0}

    def test_the_page_opens_on_its_reader_own_sizes(self, conn):
        """Otherwise it opens on whatever the catalogue has most of, which is
        women's EU36 and is nobody's idea of a first screen."""
        dbm.upsert_bot_user(
            conn, 1, "1", "reader", sizes="EU44,EU45", kinds="shoes", genders="men",
        )
        assert dbm.shelf_facets(conn)["mine"] == {
            "genders": ["men"], "kinds": ["shoes"], "sizes": ["EU44", "EU45"],
        }

    def test_a_reader_who_answered_nothing_highlights_nothing(self, conn):
        dbm.upsert_bot_user(conn, 1, "1", "reader")
        assert dbm.shelf_facets(conn)["mine"] == {"genders": [], "kinds": [], "sizes": []}

    def test_no_reader_yet_is_not_an_error(self, conn):
        assert dbm.shelf_facets(conn)["mine"] == {}


def _offer(conn, store_id, key, price, discount, score=None):
    """One offer in a named shop, scored by how deep its cut is."""
    product = dbm.upsert_product(
        conn, store_id, key, f"Thing {key}", f"https://x.example/{key}"
    )
    variant = dbm.upsert_variant(conn, product, f"v-{key}")
    conn.execute(
        """
        INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                            price_usd, reference_usd, reference_source,
                            discount_pct, saving_usd, score, all_time_low)
        VALUES (?, ?, ?, ?, ?, ?, 'market', ?, ?, ?, 0)
        """,
        (variant, product, ts(1), ts(0), price, price * 2, discount, price,
         score if score is not None else discount),
    )
    # 'market' and not 'tag': the shelf shows only discounts somebody other than
    # the seller vouches for, and these tests are about the order of what it
    # shows, not about which evidence gets in.


class TestWhoseFindGoesFirst:
    """One shop must not own the whole first screen of the shelf.

    Live: www.freshmansarchive.com lists the same vintage blazer in eight sizes
    at −90%, all scored alike, and they filled the top of the page.
    """

    def test_the_default_sort_takes_each_shop_in_turn(self, conn):
        loud = dbm.upsert_store(conn, "loud.example", platform="shopify", currency="USD")
        quiet = dbm.upsert_store(conn, "quiet.example", platform="shopify", currency="USD")
        for n in range(4):
            _offer(conn, loud, f"loud{n}", price=100.0, discount=90.0 - n)
        _offer(conn, quiet, "quiet0", price=100.0, discount=40.0)

        rows, _ = dbm.offers_for(conn, limit=2, order_by=web.SORTS["score"])

        shops = [row["domain"] for row in rows]
        assert shops == ["loud.example", "quiet.example"], shops

    def test_asking_for_the_deepest_cut_still_answers_literally(self, conn):
        loud = dbm.upsert_store(conn, "loud.example", platform="shopify", currency="USD")
        quiet = dbm.upsert_store(conn, "quiet.example", platform="shopify", currency="USD")
        _offer(conn, loud, "a", price=100.0, discount=95.0)
        _offer(conn, loud, "b", price=100.0, discount=94.0)
        _offer(conn, quiet, "c", price=100.0, discount=10.0)

        rows, _ = dbm.offers_for(conn, limit=2, order_by=web.SORTS["discount"])

        assert [row["domain"] for row in rows] == ["loud.example", "loud.example"]


class TestLookingSomethingUpFromThePage:
    """The shelf holds what is discounted; this asks the whole catalogue."""

    @staticmethod
    def _stock(conn, domain, title, price, style=None):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, f"p-{domain}", title, f"https://{domain}/p", brand="Nike"
        )
        if style:
            dbm.set_product_keys(conn, product, [("style", style)])
        variant = dbm.upsert_variant(conn, product, f"v-{domain}")
        dbm.record_price(conn, variant, price, None, True, "USD", price, 1.0)

    def test_an_article_comes_back_cheapest_first(self, conn):
        self._stock(conn, "dear.example", "AF1", 150.0, "CW2288-111")
        self._stock(conn, "cheap.example", "AF1", 90.0, "CW2288-111")

        page = web.lookup_page(conn, "CW2288-111")

        assert page["same_thing"] is True
        assert [s["price"] for s in page["shops"]] == [90.0, 150.0]
        assert page["shops"][0]["shop"] == "cheap.example"

    def test_a_name_says_it_is_not_a_comparison(self, conn):
        self._stock(conn, "one.example", "Salomon XT-6 Ember", 140.0)
        self._stock(conn, "two.example", "Salomon XT-6 Skyline", 160.0)

        page = web.lookup_page(conn, "Salomon XT-6")

        assert page["same_thing"] is False, "different shoes, not one price compared"
        assert page["found"] == 2

    def test_an_empty_query_is_not_a_search(self, conn):
        self._stock(conn, "shop.example", "AF1", 110.0, "CW2288-111")
        assert web.lookup_page(conn, "")["shops"] == []

    def test_every_row_carries_what_the_page_draws(self, conn):
        self._stock(conn, "shop.example", "AF1", 110.0, "CW2288-111")
        row = web.lookup_page(conn, "CW2288-111")["shops"][0]
        assert set(row) >= {"shop", "url", "title", "price", "country", "checked_at"}
        assert json.dumps(row), "must survive the trip to the browser"


class TestStarringSomethingOverHttp:
    """The one thing this server writes, and the check that stands in front of it.

    Over a real socket rather than by calling the handler's methods, because
    what is being tested is a rule about requests: no header, no write, with no
    exemption for the machine the server happens to be running on.
    """

    @staticmethod
    @contextmanager
    def _serving(conn, **attrs):
        """A server with the paywall **on**.

        It ships off (Config.subscription), because nothing is being sold yet.
        These tests are about what the paywall does when it is switched on, so
        they switch it on rather than assert the default — otherwise the rules
        that guard the thing being sold would quietly stop being tested the day
        selling was paused.
        """
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        attrs = {"subscription": True, **attrs}
        handler = type("Bound", (web.Handler,), {"db_path": path, **attrs})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # A short poll, or every one of these tests spends half a second in
        # shutdown() waiting for serve_forever's default interval to come round.
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @staticmethod
    def _call(url, method="GET", body=None, headers=None):
        request = Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as failure:
            return failure.code, json.loads(failure.read() or b"{}")

    @staticmethod
    def _a_product(conn):
        store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, "p1", "Salomon XT-6", "https://shop.example/p1", brand="Salomon"
        )
        variant = dbm.upsert_variant(conn, product, "v1", size_norm="EU44")
        dbm.record_price(conn, variant, 180.0, None, True, "USD", 180.0, 1.0)
        return product, variant

    def test_without_an_identity_nothing_can_be_starred(self, conn):
        """Not even from this machine: an exemption for localhost is invisible
        from outside, and `--host 0.0.0.0` is a flag that exists."""
        product, _ = self._a_product(conn)
        with self._serving(conn) as base:
            code, _ = self._call(f"{base}/api/favorites", "POST", {"product_id": product})

        assert code == 401
        assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0

    def test_a_forged_signature_is_not_an_identity(self, conn):
        product, _ = self._a_product(conn)
        with self._serving(conn, bot_token="123:AA") as base:
            code, _ = self._call(
                f"{base}/api/favorites", "POST", {"product_id": product},
                {"X-Telegram-Init-Data": "user=%7B%22id%22%3A5%7D&hash=deadbeef"},
            )

        assert code == 401

    def test_telegram_says_who_it_is_and_the_star_is_saved(self, conn):
        product, variant = self._a_product(conn)
        dbm.upsert_bot_user(conn, 7, "7")
        dbm.grant(conn, 7, days=30)
        with self._serving(conn, bot_token=TOKEN) as base:
            code, body = self._call(
                f"{base}/api/favorites", "POST",
                {"product_id": product, "variant_id": variant},
                {"X-Telegram-Init-Data": signed(user_id=7)},
            )

        assert (code, body["added"]) == (201, True)
        assert dbm.favorite_ids(conn, 7) == {product}

    def test_the_owner_flag_stands_in_for_telegram(self, conn):
        """What debugging outside Telegram uses, and it has to be typed."""
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(f"{base}/api/favorites", "POST", {"product_id": product})
            listed_code, listed = self._call(f"{base}/api/favorites")

        assert code == 201
        assert listed_code == 200
        assert [item["id"] for item in listed["items"]] == [product]
        assert listed["user"] == 7

    def test_starring_something_that_does_not_exist_is_refused(self, conn):
        self._a_product(conn)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(f"{base}/api/favorites", "POST", {"product_id": 9999})

        assert code == 404

    def test_a_body_without_a_product_is_a_bad_request_not_a_crash(self, conn):
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(f"{base}/api/favorites", "POST", {"nothing": True})

        assert code == 400

    def test_unstarring_removes_it(self, conn):
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            self._call(f"{base}/api/favorites", "POST", {"product_id": product})
            code, body = self._call(f"{base}/api/favorites/{product}", "DELETE")

        assert (code, body["removed"]) == (200, True)
        assert dbm.favorite_ids(conn, 7) == set()

    def test_starring_is_a_subscriber_feature(self, conn):
        """A free reader gets nothing from a star, and could read prices out of it."""
        product, variant = self._a_product(conn)
        with self._serving(conn, bot_token=TOKEN) as base:
            code, _ = self._call(
                f"{base}/api/favorites", "POST",
                {"product_id": product, "variant_id": variant},
                {"X-Telegram-Init-Data": signed(user_id=7)},
            )
            listed, _ = self._call(
                f"{base}/api/favorites", headers={"X-Telegram-Init-Data": signed(user_id=7)}
            )

        # Left on the identity check alone, this was the whole shelf: star ids
        # in a loop, then read title, shop, live price and discount back out.
        assert (code, listed) == (402, 402)
        assert dbm.favorite_ids(conn, 7) == set()

    def test_unstarring_stays_open_after_a_subscription_lapses(self, conn):
        product, _ = self._a_product(conn)
        dbm.upsert_bot_user(conn, 7, "7")
        dbm.grant(conn, 7, days=30)
        with self._serving(conn, bot_token=TOKEN) as base:
            self._call(
                f"{base}/api/favorites", "POST", {"product_id": product},
                {"X-Telegram-Init-Data": signed(user_id=7)},
            )
            conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 7", (ts(90),))
            code, _ = self._call(
                f"{base}/api/favorites/{product}", "DELETE",
                headers={"X-Telegram-Init-Data": signed(user_id=7)},
            )

        # Somebody must always be able to take their own things off their own
        # list, and un-starring reveals nothing.
        assert code == 200
        assert dbm.favorite_ids(conn, 7) == set()

    def test_one_readers_star_is_invisible_to_another(self, conn):
        product, _ = self._a_product(conn)
        for user_id in (7, 9):
            dbm.upsert_bot_user(conn, user_id, str(user_id))
            dbm.grant(conn, user_id, days=30)
        with self._serving(conn, bot_token=TOKEN) as base:
            self._call(
                f"{base}/api/favorites", "POST", {"product_id": product},
                {"X-Telegram-Init-Data": signed(user_id=7)},
            )
            _, theirs = self._call(
                f"{base}/api/favorites", headers={"X-Telegram-Init-Data": signed(user_id=9)}
            )

        assert theirs["items"] == []

    def test_reading_the_shelf_needs_a_subscription(self, conn):
        """The shelf is the thing being sold, so browsing it is what is gated."""
        a_shelf(conn, 2)
        with self._serving(conn) as base:
            code, body = self._call(f"{base}/api/offers")

        # 402 rather than 403: the request is understood, and the only thing
        # missing is payment.
        assert code == 402
        assert body["error"] == "subscription required"

    def test_the_owner_is_not_a_customer_of_their_own_collector(self, conn):
        a_shelf(conn, 2)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, body = self._call(f"{base}/api/offers")

        assert (code, body["total"]) == (200, 2)

    def test_a_subscriber_gets_the_shelf(self, conn):
        a_shelf(conn, 2)
        dbm.upsert_bot_user(conn, 9, "99")
        dbm.grant(conn, 9, days=30)
        with self._serving(conn, owner_id=9) as base:
            code, body = self._call(f"{base}/api/offers")

        assert (code, body["total"]) == (200, 2)

    def test_a_lapsed_reader_loses_the_shelf(self, conn):
        a_shelf(conn, 2)
        dbm.upsert_bot_user(conn, 9, "99")
        dbm.grant(conn, 9, days=30)
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 9", (ts(1),))
        with self._serving(conn, owner_id=None) as base:
            code, _ = self._call(f"{base}/api/offers")

        # Inside the grace period the feed still runs; the shelf does not, or
        # grace would be a free month of the thing being sold.
        assert code == 402

    @staticmethod
    def _page(url):
        with urlopen(url, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_the_page_arrives_with_the_owners_hearts_already_on_it(self, conn):
        """No round trip and no flash: the server already knows who this is."""
        product, _ = self._a_product(conn)
        a_shelf(conn, 1)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            self._call(f"{base}/api/favorites", "POST", {"product_id": product})
            page = self._page(base + "/")

        assert '"me": 7' in page
        assert f'"favorites": [{product}]' in page

    def test_a_link_to_the_list_arrives_with_the_list_in_it(self, conn):
        """?favorites=1 is a link somebody can be sent, so it opens on the answer."""
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            self._call(f"{base}/api/favorites", "POST", {"product_id": product})
            page = self._page(base + "/?favorites=1")
            ordinary = self._page(base + "/")

        assert "Salomon XT-6" in page
        assert '"favorites_items": null' in ordinary, "an unopened list costs nothing"

    def test_a_page_served_to_nobody_offers_the_subscription(self, conn):
        """Whoever lands here followed a button out of the bot.

        They need to know what this is and how to open it, and an error code
        tells them neither — it reads as a broken link.
        """
        a_shelf(conn, 1)
        with self._serving(conn) as base:
            page = self._page(base + "/")

        assert "Полка открывается по подписке" in page
        assert "150 звёзд в месяц" in page

    def test_a_page_served_to_the_owner_says_who_they_are(self, conn):
        a_shelf(conn, 1)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            page = self._page(base + "/")

        assert '"me": 7' in page

    def test_a_starred_row_carries_what_the_card_draws(self, conn):
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            self._call(f"{base}/api/favorites", "POST", {"product_id": product})
            _, listed = self._call(f"{base}/api/favorites")

        item = listed["items"][0]
        assert set(item) >= {"id", "title", "url", "image", "shop", "price", "since"}
        assert item["price"] == 180.0


class TestTheOwnerFlagBehindAProxy:
    """`--owner` says "whoever reaches me is that person". A tunnel makes that
    false while every other signal still says localhost.

    The guard lives in `_reader`, not in `_paying`: identity is what the flag
    hands out, and every verb — including the one that deletes — resolves it
    there. Put on `_paying` alone it was remembered twice out of three times.
    """

    # The server harness lives on the class above; borrowed rather than copied.
    _serving = staticmethod(TestStarringSomethingOverHttp._serving)
    _call = staticmethod(TestStarringSomethingOverHttp._call)
    _a_product = staticmethod(TestStarringSomethingOverHttp._a_product)

    def test_a_direct_request_is_believed(self, conn):
        a_shelf(conn, 2)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, body = self._call(f"{base}/api/offers")

        assert (code, body["total"]) == (200, 2)

    def test_a_proxied_request_is_not(self, conn):
        a_shelf(conn, 2)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(
                f"{base}/api/offers", headers={"X-Forwarded-For": "203.0.113.9"}
            )

        # cloudflared points at 127.0.0.1, so the peer address says localhost
        # for the whole internet. What a proxy cannot hide is being one.
        assert code == 402

    def test_cloudflares_own_header_counts_too(self, conn):
        a_shelf(conn, 1)
        dbm.comp(conn, 7)  # --owner names a reader; access is a separate thing
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(
                f"{base}/api/offers", headers={"CF-Connecting-IP": "203.0.113.9"}
            )

        assert code == 402

    def test_a_signed_exempt_reader_is_believed_through_a_proxy(self, conn):
        """Unlike --owner, this identity was proved, so a proxy changes nothing."""
        a_shelf(conn, 1)
        with self._serving(conn, bot_token=TOKEN, exempt_id=7) as base:
            code, body = self._call(
                f"{base}/api/offers",
                headers={
                    "X-Telegram-Init-Data": signed(user_id=7),
                    "X-Forwarded-For": "203.0.113.9",
                },
            )

        assert (code, body["total"]) == (200, 1)

    def test_the_delete_verb_is_guarded_too(self, conn):
        """The verb that was missed: it destroys, and its answer is an oracle."""
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)
        dbm.add_favorite(conn, 7, product, None)
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(
                f"{base}/api/favorites/{product}", "DELETE",
                headers={"CF-Connecting-IP": "203.0.113.9"},
            )

        # Through a tunnel this was `for i in $(seq 1 200000); do curl -X DELETE`
        # emptying the owner's list, and the removed flag flipping true→false
        # said which ids had been on it.
        assert code == 401
        assert dbm.favorite_ids(conn, 7) == {product}

    def test_the_owner_can_still_unstar_directly(self, conn):
        product, _ = self._a_product(conn)
        dbm.comp(conn, 7)
        dbm.add_favorite(conn, 7, product, None)
        with self._serving(conn, owner_id=7) as base:
            code, _ = self._call(f"{base}/api/favorites/{product}", "DELETE")

        assert code == 200
        assert dbm.favorite_ids(conn, 7) == set()

    def test_the_page_does_not_name_the_owner_through_a_proxy(self, conn):
        a_shelf(conn, 1)
        dbm.comp(conn, 7)
        with self._serving(conn, owner_id=7) as base:
            page = self._page_with(base + "/", {"X-Forwarded-For": "203.0.113.9"})

        assert "Полка открывается по подписке" in page
        assert '"me": 7' not in page

    @staticmethod
    def _page_with(url, headers):
        request = Request(url, headers=headers)
        with urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")


def a_tied_shelf(conn, n=1200, stores=2):
    """n offers that tie on every key the shelf can sort by.

    The duplicate this exists to catch is not a duplicate row — it is one row
    served twice. OFFSET paging over a non-unique ORDER BY is undefined: SQLite
    may hand back tied rows in a different order for each page, so the same card
    lands on page one and again on page two while another is never drawn at all.

    Two shops rather than one because the default sort ranks within a shop
    first, so a single-shop fixture would leave that window unexercised. On the
    live database 29,703 of 33,277 offers sit in a group tied on
    (shop, score, discount), so this fixture is the ordinary case.
    """
    for i in range(n):
        store = dbm.upsert_store(
            conn, f"tied{i % stores}.example", platform="shopify", currency="USD"
        )
        product = dbm.upsert_product(
            conn, store, f"t{i}", f"Tied {i}", f"https://tied.example/{i}",
            brand="Nike", image_url="https://img.example/x.jpg",
        )
        conn.execute(
            "UPDATE products SET kind = 'shoes', gender = 'men', brand_family = 'Nike'"
            " WHERE id = ?", (product,),
        )
        variant = dbm.upsert_variant(
            conn, product, f"tv{i}", size="US 10", size_norm="US10"
        )
        conn.execute(
            """
            INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                price_usd, reference_usd, reference_source,
                                discount_pct, saving_usd, score, all_time_low)
            VALUES (?, ?, ?, ?, 100.0, 200.0, 'market', 50.0, 100.0, 77, 0)
            """,
            (variant, product, ts(1), ts(0)),
        )


def _walk(conn, order_by, limit=24):
    """Every variant id the shelf hands out, page by page, in order."""
    seen, page = [], 0
    while True:
        rows, total = dbm.offers_for(
            conn, order_by=order_by, limit=limit, offset=page * limit
        )
        if not rows:
            return seen, total
        seen += [row["variant_id"] for row in rows]
        page += 1
        assert page < 500, "paging did not terminate"


class TestPagingNeverServesTheSameOfferTwice:
    """A shelf of ties, walked to the end, must yield each offer exactly once.

    Honest about what this does and does not show. Walked against the live
    database of 32,139 standing offers, the ordering *without* a unique
    tiebreaker also returned every card exactly once: SQLite's order for tied
    rows is undefined by contract but stable in practice while the query plan
    and the data both hold still. So this test did not fail before the
    tiebreaker was added and does not demonstrate that it fixed anything.

    It is kept as a guard on the contract rather than as proof of a bug. What
    the tiebreaker actually buys is that a plan change — a new index, a widened
    filter — cannot silently start dropping cards. The repetition a reader
    really sees on the shelf is a different thing entirely: 1,539 products hold
    more than one card because they are discounted in several sizes, and 2,428
    title-and-brand groups repeat across shops. That is folding, and no ordering
    fixes it.
    """

    def test_every_offered_sort(self, conn):
        a_tied_shelf(conn)
        for name, order_by in web.SORTS.items():
            seen, total = _walk(conn, order_by)
            assert len(seen) == len(set(seen)), f"{name}: an offer was served twice"
            assert len(seen) == total, f"{name}: walked {len(seen)} of {total}"

    def test_the_default_ordering_of_offers_for(self, conn):
        """`pi.bot` pages the same table without passing a sort of its own."""
        a_tied_shelf(conn, n=200)
        seen, total = _walk(conn, dbm.offers_for.__defaults__[4])
        assert len(seen) == len(set(seen))
        assert len(seen) == total


class TestSizesAreFoldedOnlyWhenTheyCostTheSame:
    """One shoe listed in three sizes at one price is the shelf repeating itself.

    The same shoe at three different prices is not: folding it would put a price
    on the card that is not the price of the size somebody came for. Measured on
    the live shelf, 1,539 products hold more than one card and 910 of them are
    priced differently by size, so both halves of this are the common case.
    """

    @staticmethod
    def _product(conn, prices, handle="p"):
        """One product offered in len(prices) sizes, at the prices given."""
        store = dbm.upsert_store(conn, f"{handle}.example", platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, handle, "Salomon XT-6", f"https://{handle}.example/p",
            brand="Salomon", image_url="https://img.example/x.jpg",
        )
        for i, price in enumerate(prices):
            variant = dbm.upsert_variant(
                conn, product, f"{handle}v{i}", size=f"US{10 + i}", size_norm=f"US{10 + i}"
            )
            conn.execute(
                """
                INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                    price_usd, reference_usd, reference_source,
                                    discount_pct, saving_usd, score, all_time_low)
                VALUES (?, ?, ?, ?, ?, 290.0, 'market', 50.0, ?, 70, 0)
                """,
                (variant, product, ts(1), ts(0), price, 290.0 - price),
            )
        return product

    def test_one_price_across_sizes_becomes_one_card(self, conn):
        self._product(conn, [90.0, 90.0, 90.0])
        page = web.shelf_page(conn, web.read_query(""))
        assert len(page["offers"]) == 1
        assert page["offers"][0]["sizes_on_offer"] == 3
        assert page["total"] == 1

    def test_prices_that_differ_by_size_stay_apart(self, conn):
        self._product(conn, [90.0, 180.0])
        page = web.shelf_page(conn, web.read_query(""))
        assert len(page["offers"]) == 2
        assert page["total"] == 2
        # Never "3 sizes at this price" on a card that is one price of several.
        assert {card["sizes_on_offer"] for card in page["offers"]} == {1}

    def test_the_count_matches_what_paging_hands_out(self, conn):
        """`total` drives the page counter, so a disagreement is visible."""
        self._product(conn, [90.0, 90.0], handle="same")
        self._product(conn, [70.0, 140.0], handle="split")
        seen, total = _walk(conn, web.SORTS[web.DEFAULT_SORT], limit=1)
        assert total == 3  # one folded card, two unfolded
        assert len(seen) == total

    def test_a_size_filter_still_picks_the_size_asked_for(self, conn):
        """Folding after the WHERE, not before: the card must be the US11 row."""
        self._product(conn, [90.0, 90.0])
        page = web.shelf_page(conn, web.read_query("size=US11"))
        assert len(page["offers"]) == 1
        assert page["offers"][0]["size"] == "US11"
        assert page["offers"][0]["sizes_on_offer"] == 1


class TestTheShelfIsNotGivenAwayOnTheFirstRequest:
    """Inside Telegram the signature rides in the URL fragment, which browsers
    never send. So the first GET cannot know who is asking — and must neither
    refuse a paying reader nor hand the shelf to a stranger."""

    def test_an_unsigned_request_gets_no_offers_in_the_page(self, conn):
        a_shelf(conn, 3)
        page = web.render_page(conn, web.read_query(""), None, seeded=False)
        assert b'{"seed": null}' in page
        assert b"Nike Air Max" not in page

    def test_a_known_reader_still_gets_the_first_screen(self, conn):
        a_shelf(conn, 3)
        page = web.render_page(conn, web.read_query(""), 42)
        assert b'{"seed": null}' not in page
        assert b"Nike Air Max" in page


class TestWithNothingBeingSold:
    """Selling is switched off, not removed (Config.subscription).

    The paywall, Stars, renewal, grace and refunds all stay in the code and
    stay tested; what changes is that the shelf is open. Проверяется именно
    выключенное состояние, потому что оно сейчас и работает у людей.
    """

    def test_the_shelf_opens_without_any_signature(self, conn):
        a_shelf(conn, 3)
        serving = TestStarringSomethingOverHttp._serving(conn, subscription=False)
        with serving as base, urlopen(base + "/api/offers?limit=3") as r:
            assert r.status == 200
            assert json.loads(r.read())["total"] == 3

    def test_the_page_still_carries_its_first_screen(self, conn):
        """With no paywall there is nobody to withhold it from."""
        a_shelf(conn, 3)
        page = web.render_page(conn, web.read_query(""), None)
        assert b"Nike Air Max" in page

    def test_the_paywall_is_still_there_when_switched_on(self, conn):
        """The point of a switch: the rules it guards do not rot while it is off."""
        a_shelf(conn, 3)
        with TestStarringSomethingOverHttp._serving(conn, subscription=True) as base:
            try:
                urlopen(base + "/api/offers?limit=3")
                raise AssertionError("the shelf was handed over without a subscription")
            except HTTPError as refused:
                assert refused.code == 402


class TestOnlyDiscountsSomebodyElseVouchesFor:
    """20,908 of the 26,013 cards the shelf could draw rest on nothing but the
    shop's own struck-through price — the one number a shop writes about itself,
    and the one every free discount channel repeats. A page of those reads as a
    jumble sale whatever it is dressed in."""

    @staticmethod
    def _offer(conn, domain, source, all_time_low=0):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, domain, f"Shoe {domain}", f"https://{domain}/p", brand="Nike"
        )
        conn.execute(
            "UPDATE products SET kind='shoes', brand_family='Nike' WHERE id = ?",
            (product,),
        )
        variant = dbm.upsert_variant(conn, product, f"v-{domain}", size_norm="US10")
        conn.execute(
            """
            INSERT INTO offers (variant_id, product_id, found_at, checked_at,
                                price_usd, reference_usd, reference_source,
                                discount_pct, saving_usd, score, all_time_low)
            VALUES (?, ?, ?, ?, 100.0, 200.0, ?, 50.0, 100.0, 80, ?)
            """,
            (variant, product, ts(1), ts(0), source, all_time_low),
        )

    def test_the_shops_own_tag_is_not_shown(self, conn):
        self._offer(conn, "tag.example", "tag")
        self._offer(conn, "market.example", "market")
        rows, total = dbm.offers_for(conn)
        assert total == 1
        assert rows[0]["reference_source"] == "market"

    def test_an_all_time_low_counts_even_on_a_tag(self, conn):
        """Our own record of the price is evidence the shop did not write."""
        self._offer(conn, "low.example", "tag", all_time_low=1)
        assert dbm.offers_for(conn)[1] == 1

    def test_the_full_view_is_one_click_away(self, conn):
        """Somebody hunting one particular thing wants the weak evidence too."""
        self._offer(conn, "tag.example", "tag")
        self._offer(conn, "market.example", "market")
        assert dbm.offers_for(conn, all_discounts=True)[1] == 2

    def test_the_counts_follow_what_the_page_will_show(self, conn):
        self._offer(conn, "tag.example", "tag")
        self._offer(conn, "market.example", "market")
        assert dbm.shelf_facets(conn)["total"] == 1
        assert dbm.shelf_facets(conn, all_discounts=True)["total"] == 2

    def test_the_query_reads_the_switch(self):
        assert web.read_query("")["all_discounts"] is False
        assert web.read_query("all_discounts=1")["all_discounts"] is True
        assert web.read_query("all_discounts=nonsense")["all_discounts"] is False


class TestTheButtonThatPromisesToTakeYouBackAndSubscribe:
    """«Вернуться в бота и оформить» должно оформлять, а не только закрывать.

    Кнопка на закрытой полке умела ровно одно — закрыть окно. Человек
    оказывался в чате, где ничего не происходило, и оформить было негде.
    Теперь страница сперва просит бота положить туда предложение.
    """

    @staticmethod
    @contextmanager
    def _serving(conn, **attrs):
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        handler = type("Bound", (web.Handler,), {"db_path": path, "bot_token": TOKEN, **attrs})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @staticmethod
    def _call(url, headers=None):
        request = Request(url, method="POST", data=b"{}",
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as failure:
            return failure.code, json.loads(failure.read() or b"{}")

    def test_a_stranger_cannot_make_the_bot_write_to_anybody(self, conn):
        """Без подписи неизвестно, чей это чат, — и писать некому."""
        with self._serving(conn) as base:
            code, body = self._call(f"{base}/api/pitch")
        assert code == 401
        assert body["error"] == "not signed in"

    @respx.mock
    def test_a_signed_reader_gets_the_offer_put_in_their_chat(self, conn):
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        with self._serving(conn) as base:
            code, body = self._call(f"{base}/api/pitch",
                                    {"X-Telegram-Init-Data": signed(user_id=77)})

        assert code == 200
        assert body == {"sent": True}
        assert route.called
        sent = json.loads(route.calls[0].request.content)
        assert sent["chat_id"] == 77, "предложение должно уйти тому, кто нажал"
        button = sent["reply_markup"]["inline_keyboard"][0][0]
        assert button["callback_data"] == "pitch", (
            "развилка «продажа включена или нет» живёт в боте — "
            "страница только нажимает ту же кнопку, что и меню"
        )

    @respx.mock
    def test_telegram_being_down_does_not_break_the_page(self, conn):
        """Не дозвонились — честно говорим «не отправил», а не падаем с 500."""
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            side_effect=httpx.ConnectError("нет сети")
        )
        with self._serving(conn) as base:
            code, body = self._call(f"{base}/api/pitch",
                                    {"X-Telegram-Init-Data": signed(user_id=77)})
        assert code == 200
        assert body == {"sent": False}


class TestTheLinkACardOpens:
    """A shop writes the link itself, so the shelf decides what it may be."""

    def test_a_link_relative_to_the_shop_opens_the_shop(self):
        # 2,769 products carried one; from the shelf it opened the shelf's 404.
        assert web.shop_link("/products/thing", "shop.example") == "https://shop.example/products/thing"

    def test_an_absolute_link_is_left_alone(self):
        url = "https://other.example/p/1?x=2"
        assert web.shop_link(url, "shop.example") == url

    def test_a_link_that_is_not_a_web_page_is_refused(self):
        # The page runs inside Telegram with the reader's signature to hand.
        for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "vbscript:x"):
            assert web.shop_link(bad, "shop.example") is None, bad

    def test_no_link_is_no_link(self):
        assert web.shop_link(None, "shop.example") is None
        assert web.shop_link("", "shop.example") is None

    def test_the_shelf_hands_out_the_resolved_link(self, conn):
        a_shelf(conn, n=1)
        conn.execute("UPDATE products SET url = '/products/relative'")
        conn.commit()
        page = web.shelf_page(conn, web.read_query(""))
        urls = [o["url"] for o in page["offers"]]
        assert urls and all(u.startswith("https://") and u.endswith("/products/relative") for u in urls)


class TestTheServerLetsGoOfTheDatabase:
    def test_many_requests_leave_no_open_handles_behind(self, conn):
        import gc
        import os

        a_shelf(conn, n=3)
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

        def db_handles() -> int:
            n = 0
            for fd in os.listdir("/proc/self/fd"):
                try:
                    if os.readlink(f"/proc/self/fd/{fd}").startswith(str(path)):
                        n += 1
                except OSError:
                    pass
            return n

        handler = type("Bound", (web.Handler,), {"db_path": path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        gc.disable()  # a leak that only garbage collection hides is still a leak
        try:
            before = db_handles()
            for _ in range(40):
                with urlopen(base + "/api/offers?limit=3") as r:
                    r.read()
            after = db_handles()
        finally:
            gc.enable()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        assert after - before < 5, (before, after)

    def test_a_slow_client_cannot_hold_a_thread_for_good(self):
        assert web.Handler.timeout and web.Handler.timeout <= 60


class TestAReaderWhoLeavesMidAnswer:
    """A page closed before its answer arrived printed a twenty-line traceback
    into the journal every time; nothing was wrong."""

    def test_a_broken_pipe_is_not_an_error(self, caplog):
        import io
        import logging

        class Gone(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError(32, "Broken pipe")

        handler = web.Handler.__new__(web.Handler)
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /api/offers HTTP/1.1"
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 1)
        handler.wfile = Gone()
        caplog.set_level(logging.DEBUG, logger="pi.web")

        handler._send(200, b"{}", "application/json")

        assert "went away" in caplog.text


class TestTheCardShowsItsPriceHistory:
    """The evidence behind «было»: a discount against the shop's own lowest
    price of the month is as convincing as being able to see the month."""

    def test_the_history_is_the_shops_own_price_change_by_change(self, conn):
        a_shelf(conn, n=1)
        variant, product = conn.execute("SELECT variant_id, product_id FROM offers").fetchone()
        dbm.record_price(conn, variant, 150.0, None, True, "USD", 150.0, 1.0, ts=ts(20))
        dbm.record_price(conn, variant, 80.0, None, True, "USD", 80.0, 1.0, ts=ts(0))

        history = web.product_page(conn, product)["history"]

        assert history["currency"] == "USD"
        assert [point["p"] for point in history["points"]][-2:] == [150.0, 80.0]

    def test_a_shop_that_changed_currency_draws_only_the_current_one(self, conn):
        a_shelf(conn, n=1)
        variant, product = conn.execute("SELECT variant_id, product_id FROM offers").fetchone()
        dbm.record_price(conn, variant, 100.0, None, True, "USD", 100.0, 1.0, ts=ts(9))
        dbm.record_price(conn, variant, 96.0, None, True, "CAD", 131.0, 1.36, ts=ts(1))
        dbm.record_price(conn, variant, 90.0, None, True, "CAD", 122.0, 1.36, ts=ts(0))

        history = web.product_page(conn, product)["history"]

        assert history["currency"] == "CAD"
        assert [point["p"] for point in history["points"]] == [131.0, 122.0]
