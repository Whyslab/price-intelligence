"""Shopify adapter, against a catalogue captured from a real store."""
from __future__ import annotations

import asyncio

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


@pytest.mark.parametrize(
    "raw,expected",
    [("X-Large", "XL"), ("X Large", "XL"), ("X-L", "XL"), ("X L", "XL"),
     ("XX-Large", "XXL"), ("X-X-Large", "XXL"), ("X-Small", "XS"), ("XL", "XL")],
)
def test_an_x_size_spelled_out_is_still_that_size(raw, expected):
    """Nobody writes their size as "X-L", so nothing matched these.

    12,017 variants in the live catalogue were sized "X-Large" or "X Large" and
    normalised to forms no size filter would ever equal.
    """
    from pi.sources.base import normalize_size

    assert normalize_size(raw) == expected


def test_an_x_that_is_not_a_size_is_left_alone():
    from pi.sources.base import normalize_size

    assert normalize_size("Max Load") == "MAX LOAD"


@respx.mock
async def test_one_strict_shop_backs_itself_off_not_the_whole_sweep(shopify_payload):
    """Measured live: while a sweep was collecting 429s, kith.com and
    feature.com still answered 200 to a single request. Slowing every store
    because one shop is strict wastes the whole sweep's budget."""
    from pi.throttle import RateLimiter

    limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.01)
    respx.get("https://shop.example/products.json?limit=250").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=shopify_payload)]
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", limiter=limiter)

    assert result.ok and result.products
    assert limiter.penalties == 1
    assert limiter.rate == 1000.0, "the global budget is untouched by one shop"


@respx.mock
async def test_the_limiter_holds_its_slot_across_the_request(shopify_payload):
    """The slot must wrap the HTTP call, not merely precede it.

    Shopify objects to parallel requests from one IP — measured: eight at once
    all returned 429 while the same shops answered 200 sequentially. Releasing
    the slot before the response arrives would let requests overlap again.
    """
    from contextlib import asynccontextmanager

    events: list[str] = []

    class Watching:
        penalties = 0
        rate = float("inf")

        async def acquire(self, host=""):
            events.append(f"acquire:{host}")

        def note_attempt(self, host=""):
            events.append(f"try:{host}")

        def note_success(self, host=""):
            events.append(f"ok:{host}")

        @asynccontextmanager
        async def slot(self, host=""):
            events.append(f"enter:{host}")
            try:
                yield
            finally:
                events.append(f"exit:{host}")

        async def penalise(self, pause=None, host=""):
            events.append(f"penalise:{host}")

        async def confirm_blocked(self):
            return False

    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient() as client:
        await shopify.fetch(client, "shop.example", currency="USD", limiter=Watching())

    # The slot is released as soon as the response arrives; the success is
    # recorded after, once the status has been looked at.
    assert events == [
        "enter:shop.example", "try:shop.example", "exit:shop.example", "ok:shop.example"
    ]


@respx.mock
async def test_a_blocked_platform_stops_the_sweep_instead_of_probing_it(
    shopify_payload, monkeypatch
):
    """Measured: once blocked, even one request every two seconds returns 429
    from every shop, for over twenty minutes. Continuing only prolongs it."""
    from pi.throttle import MIN_ATTEMPTS_BEFORE_BLOCK, RateLimiter

    monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 0.05)
    limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
    for _ in range(MIN_ATTEMPTS_BEFORE_BLOCK):
        limiter.note_attempt()
    for n in range(4):
        await limiter.penalise(host=f"other{n}.example")
    assert limiter.blocked

    route = respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", limiter=limiter)

    assert not route.called, "no request is made at all"
    assert not result.ok
    assert "blocked" in result.error


@respx.mock
async def test_a_store_waiting_out_a_false_block_is_still_collected(
    shopify_payload, monkeypatch
):
    """The breaker fires on a back-off convoy as readily as on a real block, and
    a store failed in the same instant can never be saved by it being wrong."""
    from pi.throttle import MIN_ATTEMPTS_BEFORE_BLOCK, RateLimiter

    monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 5.0)
    limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
    for _ in range(MIN_ATTEMPTS_BEFORE_BLOCK):
        limiter.note_attempt()
    for n in range(4):
        await limiter.penalise(host=f"other{n}.example")

    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )

    async def another_shop_answers():
        await asyncio.sleep(0.05)
        limiter.note_success("elsewhere.example")

    async with httpx.AsyncClient() as client:
        result, _ = await asyncio.gather(
            shopify.fetch(client, "shop.example", currency="USD", limiter=limiter),
            another_shop_answers(),
        )

    assert result.ok, "the block was lifted before this store gave up"
    assert result.products


class TestFinishingACatalogueOverSeveralRuns:
    """A pass that stopped early used to be filed as a complete success.

    www.flatspot.com's last sweep returned 1,000 products; the database knows of
    17,348 for that shop. Nothing in the run said so, the shop was marked
    collected and went to the back of the queue, and the same first pages came
    back next time. Two different things cut a sweep short — the page cap on a
    large catalogue, and Shopify refusing part way through — and both are
    answered the same way: say where to carry on.
    """

    @staticmethod
    def _full_page(shopify_payload):
        repeats = -(-shopify.PAGE_SIZE // len(shopify_payload["products"]))
        return {"products": shopify_payload["products"] * repeats}

    @respx.mock
    async def test_stopping_at_the_page_cap_says_where_to_resume(self, shopify_payload):
        respx.route(host="shop.example", path="/products.json").mock(
            return_value=httpx.Response(200, json=self._full_page(shopify_payload))
        )
        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", currency="USD", max_pages=2)

        assert result.ok
        assert not result.complete
        assert result.next_cursor == 3, "two full pages read from page one, carry on at three"

    @respx.mock
    async def test_a_finished_catalogue_asks_for_no_second_pass(self, shopify_payload):
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", currency="USD")

        assert result.complete
        assert result.next_cursor == 0

    @respx.mock
    async def test_the_next_run_starts_where_the_last_one_stopped(self, shopify_payload):
        page_two = respx.get("https://shop.example/products.json?limit=250&page=2").mock(
            return_value=httpx.Response(200, json=shopify_payload)
        )
        first_page = respx.get("https://shop.example/products.json?limit=250")

        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", currency="USD", cursor=2)

        assert page_two.called
        assert not first_page.called, "the pages already read are not read again"
        assert result.products

    @respx.mock
    async def test_a_sweep_cut_short_keeps_its_place_rather_than_starting_over(
        self, shopify_payload
    ):
        """The refusal arrives mid-catalogue; the page already read is not lost."""
        respx.get("https://shop.example/products.json?limit=250").mock(
            return_value=httpx.Response(200, json=self._full_page(shopify_payload))
        )
        respx.get("https://shop.example/products.json?limit=250&page=2").mock(
            return_value=httpx.Response(429)
        )

        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", currency="USD")

        assert result.ok, "one good page is still a result"
        assert result.next_cursor == 2
        assert result.products
