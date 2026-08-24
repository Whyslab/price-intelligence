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
async def test_the_cursor_advances_so_successive_runs_cover_the_catalogue():
    page = (FIXTURES / "product_page.html").read_text(encoding="utf-8")
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
