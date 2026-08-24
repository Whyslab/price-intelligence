"""Shopify adapter, against a catalogue captured from a real store."""
from __future__ import annotations

import httpx
import pytest
import respx

from pi.sources import shopify


def test_parses_a_real_catalogue(shopify_payload):
    products = shopify.parse_products(shopify_payload, "https://shop.example")

    assert products, "fixture should yield products"
    for product in products:
        assert product.title
        assert product.url.startswith("https://shop.example/products/")
        assert product.variants, "a product with no priced variant must be dropped"
        for variant in product.variants:
            assert variant.price > 0


def test_zero_priced_variants_are_dropped(shopify_payload):
    """The fixture contains a variant priced 0.00; it must not reach the database."""
    raw_ids = {
        str(v["id"])
        for p in shopify_payload["products"]
        for v in p["variants"]
        if v["price"] in ("0.00", "0", None)
    }
    assert raw_ids, "fixture should contain an unpriced variant"

    parsed_ids = {
        v.external_id for p in shopify.parse_products(shopify_payload, "https://x") for v in p.variants
    }
    assert not (raw_ids & parsed_ids)


def test_compare_at_not_above_price_is_discarded(shopify_payload):
    """A struck-through price equal to the asking price is not a discount."""
    for product in shopify.parse_products(shopify_payload, "https://x"):
        for variant in product.variants:
            if variant.compare_at is not None:
                assert variant.compare_at > variant.price


def test_a_product_without_images_still_parses(shopify_payload):
    """No picture is a reason to send text, not a reason to lose the deal."""
    products = shopify.parse_products(shopify_payload, "https://x")
    assert any(p.image_url is None for p in products)
    assert any(p.image_url for p in products)


def test_sizes_and_skus_survive(shopify_payload):
    variants = [v for p in shopify.parse_products(shopify_payload, "https://x") for v in p.variants]
    assert any(v.size for v in variants)
    assert any(v.size_norm for v in variants)


@respx.mock
async def test_fetch_walks_page_numbers_when_there_is_no_link_header(shopify_payload):
    """The storefront /products.json paginates with ?page=N and sends no Link header.

    Getting this wrong silently caps every store at one page, which is most of
    the catalogue lost with no error anywhere.
    """
    base = "https://shop.example"
    repeats = -(-shopify.PAGE_SIZE // len(shopify_payload["products"]))
    full = {"products": shopify_payload["products"] * repeats}   # a full page
    page1 = respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=full)
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json={"products": shopify_payload["products"][:1]})
    )
    page3 = respx.get(f"{base}/products.json?limit=250&page=3")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page1.called and page2.called
    assert not page3.called, "a short page means the catalogue ended"
    assert result.ok
    # repeated ids across pages are collapsed rather than counted twice
    assert len(result.products) == len(shopify_payload["products"])


@respx.mock
async def test_a_short_first_page_ends_the_walk(shopify_payload):
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert result.ok and not page2.called


@respx.mock
async def test_a_link_header_is_still_honoured_if_present(shopify_payload):
    base = "https://shop.example"
    page1 = respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(
            200, json=shopify_payload,
            headers={"Link": f'<{base}/products.json?limit=250&page_info=xyz>; rel="next"'},
        )
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page_info=xyz").mock(
        return_value=httpx.Response(200, json={"products": []})
    )

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page1.called and page2.called
    assert result.ok
    assert len(result.products) == len(shopify_payload["products"])


@respx.mock
async def test_rate_limit_is_retried_then_succeeds(shopify_payload):
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=shopify_payload),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")
    assert result.ok
    assert result.products


@respx.mock
async def test_a_non_shopify_answer_is_an_error_not_an_empty_catalogue():
    """Silently reporting 'zero products' is how the old version hid failures."""
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json={"error": "nope"})
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")
    assert not result.ok
    assert "not a Shopify catalogue" in result.error


@respx.mock
async def test_currency_is_read_from_the_storefront_not_guessed():
    """A .com domain in Berlin still charges in euros."""
    respx.get("https://shop.example/").mock(
        return_value=httpx.Response(
            200, text='<script>var Shopify = {}; Shopify.currency = {"active":"EUR","rate":"1.0"};</script>'
        )
    )
    async with httpx.AsyncClient() as client:
        assert await shopify.detect_currency(client, "https://shop.example") == "EUR"


@respx.mock
async def test_currency_falls_back_to_the_shops_country():
    respx.get("https://shop.example/").mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get("https://shop.example/meta.json").mock(
        return_value=httpx.Response(200, json={"name": "Shop", "country": "GB"})
    )
    async with httpx.AsyncClient() as client:
        assert await shopify.detect_currency(client, "https://shop.example") == "GBP"


@respx.mock
async def test_unknown_currency_stops_the_fetch():
    """Better no prices than prices in an assumed currency."""
    respx.get("https://shop.example/").mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get("https://shop.example/meta.json").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example")
    assert not result.ok
    assert "currency" in result.error


@pytest.mark.parametrize(
    "raw,expected",
    [("US 10.5", "US10.5"), ("EUR 44", "EU44"), ("44", "EU44"), ("10", "US10"),
     ("Large", "L"), ("One Size", "OS"), ("Default Title", None), ("", None), (None, None)],
)
def test_size_normalisation(raw, expected):
    from pi.sources.base import normalize_size

    assert normalize_size(raw) == expected


@respx.mock
async def test_a_429_backs_off_the_whole_shared_budget(shopify_payload):
    """One store's 429 must slow every store down — the limit is per IP.

    Shopify sends no Retry-After and stays throttled for minutes, so retrying
    the one request that failed while fifteen other stores keep hammering is
    exactly how a sweep loses most of its catalogue.
    """
    from pi.throttle import RateLimiter

    limiter = RateLimiter(rate=1000.0, cooldown=0.05)
    respx.get("https://shop.example/products.json?limit=250").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=shopify_payload),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", limiter=limiter)

    assert result.ok and result.products
    assert limiter.penalties == 1
    assert limiter.rate == 500.0


@respx.mock
async def test_every_request_passes_through_the_limiter(shopify_payload):
    calls: list[str] = []

    class Counting:
        penalties = 0

        async def acquire(self):
            calls.append("acquire")

        async def penalise(self, pause=None):
            calls.append("penalise")

    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient() as client:
        await shopify.fetch(client, "shop.example", currency="USD", limiter=Counting())

    assert calls == ["acquire"]
