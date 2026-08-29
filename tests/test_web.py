"""The shelf as a page.

Telegram is right for news and wrong for browsing, and the numbers say so: of
the 20,934 discounts standing on this database, 665 were ever announced. The
rest are not stale — they are simply not new, which is a different thing and
not a reason to hide them.
"""
from __future__ import annotations

import json

from pi import db as dbm
from pi import web

from .conftest import ts


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
