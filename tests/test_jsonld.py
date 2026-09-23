"""The generic schema.org adapter that covers everything not on Shopify."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from pi.sources import jsonld

FIXTURES = Path(__file__).parent / "fixtures"


def no_other_sitemaps(host: str = "shop.example") -> None:
    """Answer 404 for the sitemap paths a test has not mocked itself.

    Discovery reads every candidate now rather than stopping at the first one
    that answers, because zalando.pl's /sitemap.xml lists its static pages and
    its catalogue lives in another file. Register this last: respx takes the
    first route that matches, so anything a test mocks explicitly still wins.
    """
    respx.route(host=host, path__regex=r"^/sitemap").mock(
        return_value=httpx.Response(404)
    )


def test_parses_a_plain_product_page():
    page = (FIXTURES / "product_page.html").read_text(encoding="utf-8")
    parsed = jsonld.parse_product(page, "https://shop.example/adilette.html")

    assert parsed is not None
    product, currency = parsed
    assert currency == "PLN"
    assert product.title == 'Adidas Adilette "Blue"'   # HTML entities decoded
    assert product.brand == "adidas"
    # a relative image src is resolved against the page it came from
    assert product.image_url == "https://shop.example/media/adidas-adilette-blue.jpg"

    variant = product.variants[0]
    assert variant.price == 129.99
    assert variant.in_stock is True
    assert variant.sku == "288022"
    assert variant.compare_at is None  # schema.org has no struck-through price


def test_parses_a_product_nested_in_a_graph_with_european_decimals():
    page = (FIXTURES / "product_page_graph.html").read_text(encoding="utf-8")
    parsed = jsonld.parse_product(page, "https://shop.example/991")

    assert parsed is not None
    product, currency = parsed
    assert currency == "EUR"
    assert product.brand == "New Balance"
    assert product.image_url == "https://cdn.example/991-1.jpg"
    assert product.variants[0].price == 1299.00       # "1.299,00" read correctly
    assert product.variants[0].in_stock is False      # OutOfStock respected


def test_a_page_without_product_markup_yields_nothing():
    assert jsonld.parse_product("<html><body>nothing here</body></html>", "https://x") is None
    assert jsonld.parse_product('<script type="application/ld+json">{oops</script>', "https://x") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("129.99", 129.99), ("1.299,00", 1299.0), ("1,299.00", 1299.0),
     ("€ 89,90", 89.9), (49, 49.0), ("0", None), ("", None), (None, None), ("free", None)],
)
def test_price_parsing_across_locales(raw, expected):
    assert jsonld._price(raw) == expected


@respx.mock
async def test_sitemap_walk_finds_product_pages_and_skips_the_rest():
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="""<urlset>
              <url><loc>https://shop.example/sitemap/products.xml</loc></url>
              <url><loc>https://shop.example/collections/sale</loc></url>
              <url><loc>https://shop.example/blog/post</loc></url>
            </urlset>""",
        )
    )
    respx.get("https://shop.example/sitemap/products.xml").mock(
        return_value=httpx.Response(
            200,
            text="""<urlset>
              <url><loc>https://shop.example/product/one.html</loc></url>
              <url><loc>https://shop.example/product/two.html</loc></url>
              <url><loc>https://other.example/product/three.html</loc></url>
            </urlset>""",
        )
    )
    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")

    assert urls == [
        "https://shop.example/product/one.html",
        "https://shop.example/product/two.html",
    ]  # off-site, category and blog URLs all excluded


@respx.mock
async def test_clean_product_paths_are_found_without_any_url_marker():
    """Shops that serve products from /mens/footwear/nike-air-max-90/ still work.

    Requiring a /product/ marker in the URL threw away whole catalogues; the
    slug is the signal, and the JSON-LD parser is the final judge.
    """
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="""<urlset>
              <url><loc>https://shop.example/</loc></url>
              <url><loc>https://shop.example/mens/footwear/</loc></url>
              <url><loc>https://shop.example/mens/footwear/nike-air-max-90/</loc></url>
              <url><loc>https://shop.example/mens/footwear/adidas-samba-og/</loc></url>
              <url><loc>https://shop.example/about-us/</loc></url>
            </urlset>""",
        )
    )
    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")

    assert urls == [
        "https://shop.example/mens/footwear/nike-air-max-90/",
        "https://shop.example/mens/footwear/adidas-samba-og/",
    ]  # homepage, section root and the about page all dropped


@respx.mock
async def test_the_cursor_advances_so_successive_runs_cover_the_catalogue():
    page = (FIXTURES / "product_page.html").read_text(encoding="utf-8")
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset>"
            + "".join(f"<url><loc>https://shop.example/product/{i}.html</loc></url>" for i in range(5))
            + "</urlset>",
        )
    )
    respx.route(host="shop.example", path__regex=r"/product/\d+\.html").mock(
        return_value=httpx.Response(200, text=page)
    )

    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        first = await jsonld.fetch(client, "shop.example", budget=2, cursor=0)
        assert first.ok and first.next_cursor == 2
        second = await jsonld.fetch(client, "shop.example", budget=2, cursor=first.next_cursor)
        assert second.ok and second.next_cursor == 4
        # wraps around rather than running off the end
        third = await jsonld.fetch(client, "shop.example", budget=2, cursor=second.next_cursor)
        assert third.ok and third.next_cursor == 1


@respx.mock
async def test_a_store_with_no_markup_reports_an_error():
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text="<urlset><url><loc>https://shop.example/product/x.html</loc></url></urlset>"
        )
    )
    respx.get("https://shop.example/product/x.html").mock(
        return_value=httpx.Response(200, text="<html>no markup</html>")
    )
    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        result = await jsonld.fetch(client, "shop.example", budget=1)
    assert not result.ok
    assert "schema.org" in result.error


class TestDetect:
    """Classifying how a store can be read, without destroying what we know."""

    @respx.mock
    async def test_a_shopify_store_is_recognised_by_its_catalogue(self):
        from pi.sources import detect

        respx.get("https://shop.example/products.json?limit=1").mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(200, text='Shopify.currency = {"active":"EUR"};')
        )
        respx.get("https://shop.example/meta.json").mock(
            return_value=httpx.Response(200, json={"name": "Shop", "country": "DE"})
        )
        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example")

        assert verdict["platform"] == "shopify"
        assert verdict["currency"] == "EUR"
        assert verdict["country"] == "DE"

    @respx.mock
    async def test_a_throttled_probe_does_not_overwrite_a_working_platform(self, tmp_path):
        """Shopify throttles our whole IP for minutes.

        A probe that runs during one must not rewrite 138 working shops as
        'blocked' and stop the collector reading them for good.
        """
        from pi import db as dbm
        from pi.sources import detect

        conn = dbm.connect(tmp_path / "pi.db")
        dbm.upsert_store(conn, "shop.example", platform="shopify", currency="EUR")

        respx.get("https://shop.example/products.json?limit=1").mock(
            return_value=httpx.Response(429)
        )
        async with httpx.AsyncClient() as client:
            tally = await detect.detect_all(conn, ["shop.example"], client)

        row = conn.execute("SELECT platform, currency, last_error FROM stores").fetchone()
        assert row["platform"] == "shopify", "the working platform survives"
        assert row["currency"] == "EUR"
        assert "throttled" in row["last_error"]
        assert tally == {"throttled": 1}

    @respx.mock
    async def test_a_real_block_is_recorded(self, tmp_path):
        from pi import db as dbm
        from pi.sources import detect

        conn = dbm.connect(tmp_path / "pi.db")
        respx.get("https://walled.example/products.json?limit=1").mock(
            return_value=httpx.Response(403)
        )
        respx.get("https://walled.example/").mock(return_value=httpx.Response(403))

        async with httpx.AsyncClient() as client:
            await detect.detect_all(conn, ["walled.example"], client)

        row = conn.execute("SELECT platform, status FROM stores").fetchone()
        assert row["platform"] == "blocked"
        assert row["status"] == "skipped"


@respx.mock
async def test_the_sitemap_is_found_where_robots_txt_says_it_is():
    """Many shops put the sitemap somewhere none of the standard guesses reach.

    Measured on the live site list, five of six stores that reported "no product
    URLs in sitemap" declared a perfectly good one in robots.txt.
    """
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text="User-agent: *\nDisallow: /cart\n"
                 "Sitemap: https://shop.example/shop/sitemapindex.xml\n"
                 "Sitemap: https://evil.example/other.xml\n",
        )
    )
    respx.get("https://shop.example/shop/sitemapindex.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset><url><loc>https://shop.example/product/one.html</loc></url></urlset>",
        )
    )
    off_site = respx.get("https://evil.example/other.xml")

    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")

    assert urls == ["https://shop.example/product/one.html"]
    assert not off_site.called, "a sitemap on another host is not ours to crawl"


@respx.mock
async def test_the_standard_paths_are_still_tried_without_robots_txt():
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset><url><loc>https://shop.example/product/one.html</loc></url></urlset>",
        )
    )
    no_other_sitemaps()
    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")
    assert urls == ["https://shop.example/product/one.html"]


class TestTellingAProductFromAnArticleAboutOne:
    """The positive hints are loose on purpose, which lets prose in."""

    def test_a_help_article_about_products_is_not_a_product(self):
        """www.nike.com's entire sitemap is 44 pages shaped like this."""
        for path in (
            "https://www.nike.com/help/a/product-discount",
            "https://www.nike.com/help/a/product-availability",
            "https://shop.example/support/product-care",
            "https://shop.example/customer-service/product-returns",
        ):
            assert not jsonld._is_product_url(path), path

    def test_a_real_product_url_still_passes(self):
        for path in (
            "https://shop.example/products/air-max-90",
            "https://shop.example/p/12345",
            "https://shop.example/mens/footwear/nike-air-max-90/",
        ):
            assert jsonld._is_product_url(path), path


class TestCrawlingWhenTheSitemapIsUseless:
    """36 live stores reported "no product URLs in sitemap".

    Two different faults hide behind that one message. eobuwie.pl, modivo.pl,
    basket4ballers.com and dripla.com answer 404 for /sitemap.xml altogether;
    www.ageha.it serves a valid sitemap that lists five pages, none of them a
    product. Both shops still have a catalogue, reachable by following category
    links the way a customer would.
    """

    @respx.mock
    async def test_a_sitemap_of_static_pages_falls_back_to_crawling(self):
        respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://shop.example/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    "<url><loc>https://shop.example/about-us</loc></url>"
                    "<url><loc>https://shop.example/contact</loc></url>"
                    "</urlset>"
                ),
            )
        )
        respx.get("https://shop.example/sitemap_index.xml").mock(return_value=httpx.Response(404))
        respx.get("https://shop.example/sitemap/products.xml").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(
                200, text='<a href="/collections/sneakers">Sneakers</a>'
            )
        )
        respx.get("https://shop.example/collections/sneakers").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<a href="/products/air-max-90">one</a>'
                    '<a href="/products/gel-lyte-iii">two</a>'
                ),
            )
        )
        respx.get("https://shop.example/collections/sneakers?page=2").mock(
            return_value=httpx.Response(200, text='<a href="/products/air-max-90">one</a>')
        )

        async with httpx.AsyncClient() as client:
            urls = await jsonld.discover_product_urls(client, "https://shop.example")

        assert urls == [
            "https://shop.example/products/air-max-90",
            "https://shop.example/products/gel-lyte-iii",
        ]

    @respx.mock
    async def test_pages_that_announce_themselves_as_products_are_read_first(self):
        """The run only reads the first `budget` URLs, so their order decides everything.

        Measured on basket4ballers.com: the crawl found 214 URLs, of which the 38
        carrying a product marker all parsed and the categories sorting ahead of
        them alphabetically parsed as nothing. Ordering alone took that store
        from zero products to thirty-six.
        """
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<a href="/aaa-looks-like-a-slug">not a product</a>'
                    '<a href="/products/zzz-air-max-90">a product</a>'
                ),
            )
        )
        async with httpx.AsyncClient() as client:
            urls = await jsonld.crawl_product_urls(client, "https://shop.example")

        assert urls[0] == "https://shop.example/products/zzz-air-max-90"
        assert "https://shop.example/aaa-looks-like-a-slug" in urls

    @respx.mock
    async def test_campaign_parameters_do_not_multiply_one_page_into_many(self):
        """eobuwie.pl links the same brand listing five ways with itm_* parameters.

        Each one would have cost a page from a budget of thirty.
        """
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<a href="/products/one?itm_source=home&itm_medium=brands">a</a>'
                    '<a href="/products/one?utm_campaign=x">b</a>'
                    '<a href="/products/one">c</a>'
                    '<a href="/products/two?colour=black">d</a>'
                ),
            )
        )
        async with httpx.AsyncClient() as client:
            urls = await jsonld.crawl_product_urls(client, "https://shop.example")

        assert urls == [
            "https://shop.example/products/one",
            "https://shop.example/products/two?colour=black",
        ], "tracking is stripped, a real variant parameter is kept"

    @respx.mock
    async def test_the_crawl_stops_at_its_budget(self):
        """These are somebody else's servers and a crawl has no natural end."""
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(
                200,
                text="".join(
                    f'<a href="/collections/cat-{n}">c{n}</a>' for n in range(50)
                ),
            )
        )
        listing = respx.route(
            host="shop.example", path__regex=r"^/collections/cat-\d+$"
        ).mock(return_value=httpx.Response(200, text='<a href="/products/p">p</a>'))

        async with httpx.AsyncClient() as client:
            await jsonld.crawl_product_urls(client, "https://shop.example", budget=6)

        assert listing.call_count <= 5, "the front page counts against the budget too"


class TestDetectingAShopByItsProductPage:
    """The homepage is the wrong place to look for schema.org/Product.

    Deciding from the front page alone classified 45 live stores as having no
    structured data. Six of them were opened by hand — BSTN, Oi Polloi,
    Consortium, Wood Wood, Blue Tomato, Laced — and every one answers 200 with a
    readable catalogue behind it.
    """

    @respx.mock
    async def test_a_shop_with_a_bare_homepage_is_judged_by_a_product_page(self):
        from pi.sources import detect

        respx.get("https://shop.example/products.json?limit=1").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(200, text="<html><body>welcome</body></html>")
        )
        respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://shop.example/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text="<urlset><url><loc>https://shop.example/product/one.html</loc></url></urlset>",
            )
        )
        respx.get("https://shop.example/product/one.html").mock(
            return_value=httpx.Response(
                200, text=(FIXTURES / "product_page.html").read_text(encoding="utf-8")
            )
        )

        no_other_sitemaps()
        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example")

        assert verdict["platform"] == "jsonld"

    @respx.mock
    async def test_a_shop_with_no_product_pages_at_all_says_so(self):
        """The old message blamed the storefront for what was never checked."""
        from pi.sources import detect

        respx.get("https://shop.example/products.json?limit=1").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(200, text="<html><body>welcome</body></html>")
        )
        respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
        respx.route(host="shop.example", path__regex=r"^/sitemap").mock(
            return_value=httpx.Response(404)
        )

        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example")

        assert verdict["platform"] == "unknown"
        assert verdict["error"] == "no product pages found in the sitemap or by crawling"

    @respx.mock
    async def test_a_javascript_priced_shop_is_told_apart_from_an_empty_one(self):
        """These two need different answers, and used to get the same one.

        A shop we simply could not find a product page on might be reachable a
        different way. A shop whose product page loads and carries a name, an
        article number and no price is rendering it in JavaScript — a wall, not
        a gap in the parser. 43 of the 45 stores behind the old verdict are the
        second kind, and nothing in this adapter will ever read them.
        """
        from pi.sources import detect

        respx.get("https://shop.example/products.json?limit=1").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://shop.example/").mock(
            return_value=httpx.Response(200, text="<html><body>welcome</body></html>")
        )
        respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://shop.example/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text="<urlset><url><loc>https://shop.example/product/one.html</loc></url></urlset>",
            )
        )
        respx.get("https://shop.example/product/one.html").mock(
            return_value=httpx.Response(
                200, text='<h1 itemprop="name">Air Max 90</h1><div id="root"></div>'
            )
        )

        no_other_sitemaps()
        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example")

        assert verdict["platform"] == "unknown"
        assert "JavaScript" in verdict["error"]


@respx.mock
async def test_a_sitemap_of_static_pages_is_not_the_end_of_the_search():
    """zalando.pl's /sitemap.xml lists nine pages, none of them a product.

    Discovery used to return on the first sitemap that answered with anything at
    all, so those nine were the whole catalogue as far as we were concerned.
    """
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset><url><loc>https://shop.example/delivery-and-returns</loc></url></urlset>",
        )
    )
    respx.get("https://shop.example/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/sitemap/products.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset><url><loc>https://shop.example/products/air-max-90</loc></url></urlset>",
        )
    )

    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")

    assert urls == ["https://shop.example/products/air-max-90"], (
        "a marked product outranks a plausible-looking static page from an earlier sitemap"
    )


class TestProductGroups:
    """One product, many sizes — schema.org's newer shape.

    A ProductGroup carries no Offer of its own; the prices and the stock live in
    `hasVariant`, one Product per size. Reading only the old shape cost the whole
    shop: www.43einhalb.com had been failing with "no schema.org/Product markup
    found" since it migrated, and reads 395 products once this is understood.
    """

    def _parsed(self):
        page = (FIXTURES / "product_group_page.html").read_text(encoding="utf-8")
        return jsonld.parse_product(page, "https://shop.example/en/p/cap-40271")

    def test_every_size_becomes_its_own_variant(self):
        parsed = self._parsed()
        assert parsed is not None
        product, currency = parsed
        assert currency == "EUR"
        assert product.title == "x 43einhalb R3D Anniversary - Fitted Cap"
        assert product.brand == "New Era"
        assert [v.size for v in product.variants] == ["EU 42", "EU 43"]

    def test_a_size_without_a_price_is_left_out(self):
        """Not guessed at from its neighbours: an unpriced size is not on sale."""
        product, _ = self._parsed()
        assert all(v.price > 0 for v in product.variants)
        assert "EU 44" not in [v.size for v in product.variants]

    def test_stock_is_read_per_size_not_per_product(self):
        """The difference between a find and a sold-out listing."""
        product, _ = self._parsed()
        by_size = {v.size: v for v in product.variants}
        assert by_size["EU 42"].in_stock is False
        assert by_size["EU 43"].in_stock is True
        assert by_size["EU 43"].price == 39.00

    def test_a_bare_numeric_sku_is_still_an_sku(self):
        """43einhalb publishes "sku": 40272 unquoted, which used to read as none."""
        product, _ = self._parsed()
        assert [v.sku for v in product.variants] == ["40272", "40273"]

    def test_the_group_id_identifies_the_product(self):
        """`productGroupID` is stable across the sizes; a variant sku is not."""
        product, _ = self._parsed()
        assert product.external_id == "10000045122"

    def test_the_old_single_offer_shape_still_works(self):
        """The shops that never migrated must not be broken by this."""
        page = (FIXTURES / "product_page.html").read_text(encoding="utf-8")
        parsed = jsonld.parse_product(page, "https://shop.example/adilette.html")
        assert parsed is not None
        product, currency = parsed
        assert currency == "PLN"
        assert len(product.variants) == 1
        assert product.variants[0].price == 129.99


def a_page(offers, **product) -> str:
    """A plain Product page whose `offers` is whatever a test needs it to be."""
    node = {"@context": "https://schema.org", "@type": "Product",
            "name": "Nike Air Max 1/97 Sean Wotherspoon", "sku": "AJ4219-400", **product}
    node["offers"] = offers
    return f'<html><script type="application/ld+json">{json.dumps(node)}</script></html>'


def offer(price, *, sku=None, size=None, in_stock=True):
    node = {"@type": "Offer", "price": str(price), "priceCurrency": "EUR",
            "availability": "https://schema.org/" + ("InStock" if in_stock else "OutOfStock")}
    if sku:
        node["sku"] = sku
    if size:
        node["size"] = size
    return node


class TestAProductThatListsAnOfferPerSize:
    """schema.org's older shape for the same statement `hasVariant` makes.

    Nine sizes in stock are nine Offers under one Product. Reading only the
    first read whichever size the shop's template led with, and both ends of
    that list are wrong: allikestore.com priced a Sean Wotherspoon at EUR 47.99
    off a leftover child's size while other shops asked $500-$1,500.
    """

    @staticmethod
    def sizes() -> list[dict]:
        return [
            offer(47.99, sku="AJ4219-400-36", size="EU 36"),
            offer(699.0, sku="AJ4219-400-42", size="EU 42"),
            offer(719.0, sku="AJ4219-400-44", size="EU 44"),
        ]

    def test_every_size_becomes_its_own_variant(self):
        product, currency = jsonld.parse_product(a_page(self.sizes()), "https://shop.example/p")
        assert currency == "EUR"
        assert [v.size for v in product.variants] == ["EU 36", "EU 42", "EU 44"]
        assert [v.price for v in product.variants] == [47.99, 699.0, 719.0]

    def test_the_order_of_the_sizes_does_not_decide_the_price(self):
        """The bug this replaces: the first Offer won, whatever it was."""
        forwards, _ = jsonld.parse_product(a_page(self.sizes()), "https://shop.example/p")
        backwards, _ = jsonld.parse_product(
            a_page(list(reversed(self.sizes()))), "https://shop.example/p"
        )
        assert sorted(v.price for v in forwards.variants) == sorted(
            v.price for v in backwards.variants
        )

    def test_stock_is_read_per_size(self):
        offers = [
            offer(699.0, sku="a", size="EU 42", in_stock=False),
            offer(719.0, sku="b", size="EU 44", in_stock=True),
        ]
        product, _ = jsonld.parse_product(a_page(offers), "https://shop.example/p")
        assert [v.in_stock for v in product.variants] == [False, True]

    def test_sizes_that_cannot_be_told_apart_collapse_to_the_middle(self):
        """No sku and no size is nothing stable to call a variant by, so the
        product keeps one price — the median, which a reorder cannot move."""
        offers = [offer(47.99), offer(699.0), offer(719.0)]
        product, _ = jsonld.parse_product(a_page(offers), "https://shop.example/p")
        assert len(product.variants) == 1
        assert product.variants[0].price == 699.0, "not 47.99, which led the list"

    def test_the_collapsed_price_is_the_same_whatever_the_order(self):
        prices = [47.99, 699.0, 719.0]
        seen = set()
        for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):
            page = a_page([offer(prices[i]) for i in order])
            product, _ = jsonld.parse_product(page, "https://shop.example/p")
            seen.add(product.variants[0].price)
        assert seen == {699.0}

    def test_a_collapsed_product_is_in_stock_if_any_size_is(self):
        """A shop with one size left still has the shoe."""
        offers = [offer(699.0, in_stock=False), offer(719.0, in_stock=True)]
        product, _ = jsonld.parse_product(a_page(offers), "https://shop.example/p")
        assert product.variants[0].in_stock is True

    def test_a_single_offer_in_a_list_still_reads_as_one_price(self):
        product, _ = jsonld.parse_product(a_page([offer(129.99)]), "https://shop.example/p")
        assert [v.price for v in product.variants] == [129.99]

    def test_an_unpriced_offer_among_priced_ones_is_left_out(self):
        offers = [{"@type": "Offer", "sku": "x", "size": "EU 40"},
                  offer(699.0, sku="a", size="EU 42"),
                  offer(719.0, sku="b", size="EU 44")]
        product, _ = jsonld.parse_product(a_page(offers), "https://shop.example/p")
        assert [v.size for v in product.variants] == ["EU 42", "EU 44"]


class TestAShopThatStoppedLettingUsIn:
    """www.kickz.com was reported for three weeks as having "no schema.org/Product
    markup", which sends whoever reads it to look at the parser. Its pages were
    not loading at all — a different problem with a different fix."""

    @staticmethod
    def _sitemap():
        respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://shop.example/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text="""<urlset>
                  <url><loc>https://shop.example/product/one.html</loc></url>
                  <url><loc>https://shop.example/product/two.html</loc></url>
                </urlset>""",
            )
        )
        no_other_sitemaps()

    @respx.mock
    async def test_pages_that_will_not_load_are_called_that(self):
        self._sitemap()
        respx.get(url__regex=r"https://shop\.example/product/.*").mock(
            return_value=httpx.Response(403)
        )
        async with httpx.AsyncClient() as client:
            result = await jsonld.fetch(client, "shop.example")

        assert result.error.startswith(jsonld.PAGES_WOULD_NOT_LOAD)
        assert "HTTP 403 ×2" in result.error, "each page asked once, not twice"

    @respx.mock
    async def test_pages_that_load_without_prices_are_still_no_markup(self):
        self._sitemap()
        respx.get(url__regex=r"https://shop\.example/product/.*").mock(
            return_value=httpx.Response(200, text="<html>no prices here</html>")
        )
        async with httpx.AsyncClient() as client:
            result = await jsonld.fetch(client, "shop.example")

        assert result.error == jsonld.NO_MARKUP
