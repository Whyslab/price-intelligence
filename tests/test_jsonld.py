"""The generic schema.org adapter that covers everything not on Shopify."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from pi.sources import jsonld

FIXTURES = Path(__file__).parent / "fixtures"


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

    async with httpx.AsyncClient() as client:
        first, cursor = await jsonld.fetch(client, "shop.example", budget=2, cursor=0)
        assert first.ok and cursor == 2
        second, cursor = await jsonld.fetch(client, "shop.example", budget=2, cursor=cursor)
        assert second.ok and cursor == 4
        # wraps around rather than running off the end
        third, cursor = await jsonld.fetch(client, "shop.example", budget=2, cursor=cursor)
        assert third.ok and cursor == 1


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
    async with httpx.AsyncClient() as client:
        result, _ = await jsonld.fetch(client, "shop.example", budget=1)
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
    async with httpx.AsyncClient() as client:
        urls = await jsonld.discover_product_urls(client, "https://shop.example")
    assert urls == ["https://shop.example/product/one.html"]
