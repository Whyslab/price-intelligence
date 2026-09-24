"""Shopify adapter, against a catalogue captured from a real store."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from pi.sources import shopify

from .conftest import end_of_catalogue, numbered_products


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
    page1 = respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 1))
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 2, count=3))
    )
    page3 = end_of_catalogue(base, page=3)
    page4 = respx.get(f"{base}/products.json?limit=250&page=4")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page1.called and page2.called and page3.called
    assert not page4.called, "an empty page means the catalogue ended"
    assert result.ok and result.complete and result.enumerated
    assert len(result.products) == shopify.PAGE_SIZE + 3


@respx.mock
async def test_a_short_page_is_not_the_end_of_the_catalogue(shopify_payload):
    """Shopify cuts a page and only then removes what this visitor may not buy.

    Measured 23.09.2026: shop.simon.com answered 245 products on page one and
    250 on page two. Stopping at the short page told the run it had seen the
    whole shop, and 76,062 products still for sale were marked as withdrawn.
    """
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 1, count=245))
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 2))
    )
    end_of_catalogue(base, page=3)
    end_of_catalogue(base, page=4)  # empty after a full page is asked about once more

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page2.called, "the short first page did not end the walk"
    assert len(result.products) == 245 + 250
    assert result.enumerated, "reaching the empty page from page one is the whole shop"


@respx.mock
async def test_an_empty_page_after_a_full_one_is_asked_about_again(shopify_payload):
    """250 products in a row this visitor may not buy make an empty page in the
    middle of a catalogue. Taken as the end, every pass would stop at the same
    gap and the tail — here 30 products — would be marked withdrawn."""
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 1))
    )
    end_of_catalogue(base, page=2)
    respx.get(f"{base}/products.json?limit=250&page=3").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 3, count=30))
    )
    end_of_catalogue(base, page=4)

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert len(result.products) == 250 + 30, "the tail past the gap was read"
    assert result.enumerated


@respx.mock
async def test_an_empty_page_is_asked_past_while_products_it_sells_are_unlisted(
    shopify_payload
):
    """Review 24.09, then www.italist.com the same day: a short page, an empty
    one, and the catalogue going on after it. Asking past every empty page
    would cost a request per pass; asking when products the shop was selling
    have not come up yet costs one only where the pass would withdraw them."""
    base = "https://shop.example"
    first = numbered_products(shopify_payload, 1, count=240)
    tail = numbered_products(shopify_payload, 3, count=20)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=first)
    )
    end_of_catalogue(base, page=2)  # a page this visitor may buy nothing from
    page3 = respx.get(f"{base}/products.json?limit=250&page=3").mock(
        return_value=httpx.Response(200, json=tail)
    )
    end_of_catalogue(base, page=4)
    known = {str(product["id"]) for product in first["products"] + tail["products"]}

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", known=known)

    assert page3.called and len(result.products) == 260
    assert result.enumerated


@respx.mock
async def test_a_gap_of_two_empty_pages_is_read_past_too(shopify_payload):
    """Review 24.09: 500 products in a row this visitor may not buy make two
    empty pages, and one probe stopped at the second."""
    base = "https://shop.example"
    first = numbered_products(shopify_payload, 1, count=240)
    tail = numbered_products(shopify_payload, 4, count=20)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=first)
    )
    for empty in (2, 3):
        respx.get(f"{base}/products.json?limit=250&page={empty}").mock(
            return_value=httpx.Response(200, json={"products": []})
        )
    respx.get(f"{base}/products.json?limit=250&page=4").mock(
        return_value=httpx.Response(200, json=tail)
    )
    end_of_catalogue(base, page=5)
    known = {str(product["id"]) for product in first["products"] + tail["products"]}

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", known=known)

    assert len(result.products) == 260 and result.enumerated


@respx.mock
async def test_the_look_past_empty_pages_is_bounded(shopify_payload):
    """Products really gone are unlisted too; the pass asks GAP_PAGES past the
    empty page for them and stops, rather than walking to page 100."""
    base = "https://shop.example"
    first = numbered_products(shopify_payload, 1, count=240)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=first)
    )
    end_of_catalogue(base, page=2)
    beyond = respx.get(
        f"{base}/products.json?limit=250&page={2 + shopify.GAP_PAGES + 1}"
    )

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(
            client, "shop.example", currency="USD", known={"gone-for-real"}
        )

    assert not beyond.called and result.enumerated and len(result.products) == 240


@respx.mock
async def test_nothing_unlisted_means_no_extra_page(shopify_payload):
    base = "https://shop.example"
    first = numbered_products(shopify_payload, 1, count=240)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=first)
    )
    end_of_catalogue(base, page=2)
    page3 = respx.get(f"{base}/products.json?limit=250&page=3")
    known = {str(product["id"]) for product in first["products"]}

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", known=known)

    assert not page3.called, "the whole shop came up: one empty page is the end"
    assert result.enumerated and len(result.products) == 240


@respx.mock
async def test_a_pass_resumed_part_way_does_not_ask_past_the_end(shopify_payload):
    """Only a pass from the beginning can withdraw anything; a resumed one reads
    a tail and must not spend a request on the page past it."""
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250&page=3").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 3, count=40))
    )
    end_of_catalogue(base, page=4)
    page5 = respx.get(f"{base}/products.json?limit=250&page=5")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(
            client, "shop.example", currency="USD", cursor=3, known={"not-listed"}
        )

    assert not page5.called and not result.enumerated


@respx.mock
async def test_an_empty_page_after_a_short_one_is_the_end(shopify_payload):
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 1, count=40))
    )
    end_of_catalogue(base, page=2)
    page3 = respx.get(f"{base}/products.json?limit=250&page=3")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert not page3.called, "one empty page is enough after a short one"
    assert result.enumerated


@respx.mock
async def test_a_page_that_repeats_what_was_read_ends_the_walk(shopify_payload):
    """A storefront that ignored ?page= would serve page one for ever."""
    base = "https://shop.example"
    same = numbered_products(shopify_payload, 1, count=40)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=same)
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json=same)
    )
    page3 = respx.get(f"{base}/products.json?limit=250&page=3")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page2.called and not page3.called
    assert len(result.products) == 40
    assert not result.enumerated, "a repeat says nothing about where the catalogue ends"


@respx.mock
async def test_a_page_of_unpriced_products_is_still_a_page(shopify_payload):
    """Parsing drops what has no price; the page itself still listed products,
    so it is not the end of the catalogue."""
    base = "https://shop.example"
    unpriced = numbered_products(shopify_payload, 1)
    for product in unpriced["products"]:
        for variant in product["variants"]:
            variant["price"] = "0.00"
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=unpriced)
    )
    page2 = respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 2, count=5))
    )
    end_of_catalogue(base, page=3)

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert page2.called
    assert len(result.products) == 5


@respx.mock
async def test_the_walk_stops_before_the_page_shopify_refuses(shopify_payload):
    """Page 101 of 250 answers HTTP 400; there is no point asking for it."""
    base = "https://shop.example"
    respx.get(f"{base}/products.json?limit=250&page=100").mock(
        return_value=httpx.Response(200, json=numbered_products(shopify_payload, 100))
    )
    wall = respx.get(f"{base}/products.json?limit=250&page=101")

    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD", cursor=100)

    assert not wall.called
    assert not result.complete and result.next_cursor == 101


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
    # Nothing asked by number after the links ran out.
    assert not any("page=" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_rate_limit_is_retried_then_succeeds(shopify_payload):
    base = "https://shop.example"
    end_of_catalogue(base)
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
async def test_currency_is_read_from_the_shop_not_guessed():
    """A .com domain in Berlin still charges in euros."""
    respx.get("https://shop.example/meta.json").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/").mock(
        return_value=httpx.Response(
            200, text='<script>var Shopify = {}; Shopify.currency = {"active":"EUR","rate":"1.0"};</script>'
        )
    )
    async with httpx.AsyncClient() as client:
        assert await shopify.detect_currency(client, "https://shop.example") == "EUR"


@respx.mock
async def test_the_shops_own_base_currency_beats_what_the_storefront_shows():
    """With Shopify Markets on, the storefront names the currency chosen for
    this visitor; /products.json still quotes the base. Reading the first and
    pricing the second multiplies the whole catalogue by an exchange rate —
    www.slamcity.com quotes GBP, was recorded as NOK, and its skate shoes went
    in at $6.96 instead of about $88."""
    respx.get("https://shop.example/meta.json").mock(
        return_value=httpx.Response(200, json={"name": "Slam", "country": "GB", "currency": "GBP"})
    )
    respx.get("https://shop.example/").mock(
        return_value=httpx.Response(
            200, text='<script>Shopify.currency = {"active":"NOK","rate":"12.6"};</script>'
        )
    )
    async with httpx.AsyncClient() as client:
        assert await shopify.detect_currency(client, "https://shop.example") == "GBP"


@respx.mock
async def test_a_meta_currency_that_is_not_a_currency_is_not_believed():
    respx.get("https://shop.example/meta.json").mock(
        return_value=httpx.Response(200, json={"country": "GB", "currency": ""})
    )
    respx.get("https://shop.example/").mock(return_value=httpx.Response(200, text="<html></html>"))
    async with httpx.AsyncClient() as client:
        assert await shopify.detect_currency(client, "https://shop.example") == "GBP"


@respx.mock
async def test_the_currency_the_answer_names_beats_the_one_on_record(shopify_payload):
    """A Markets shop quotes the visitor's currency and says so in a cookie.

    www.stadiumgoods.com states USD in /meta.json and served NOK to a reader in
    Norway, so a 1,095 kr Air Force 1 was stored — and shown — as $1095.
    """
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(
            200, json=shopify_payload,
            headers={"set-cookie": "cart_currency=NOK; path=/; SameSite=Lax"},
        )
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert result.ok and result.currency == "NOK"
    assert {p.currency for p in result.products} == {"NOK"}


@respx.mock
async def test_the_cookie_carried_back_still_names_the_currency(shopify_payload):
    """Once the client holds the cookie the shop stops setting it; the request
    that carried it is then the only statement of what the page is priced in."""
    end_of_catalogue()
    route = respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient(cookies={"cart_currency": "NOK"}) as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert route.called
    assert result.currency == "NOK"
    assert {p.currency for p in result.products} == {"NOK"}


@respx.mock
async def test_a_shop_that_answers_in_its_base_keeps_it(shopify_payload):
    """The 2 September repair still holds: www.slamcity.com serves pounds and
    says GBP, whatever the storefront shows a visitor."""
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(
            200, json=shopify_payload, headers={"set-cookie": "cart_currency=GBP; path=/"}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="GBP")

    assert result.currency == "GBP"
    assert {p.currency for p in result.products} == {"GBP"}


@respx.mock
async def test_an_answer_that_names_nothing_keeps_the_currency_on_record(shopify_payload):
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert result.currency == "USD"
    assert {p.currency for p in result.products} == {"USD"}


@respx.mock
async def test_one_product_is_priced_in_the_currency_it_names(shopify_payload):
    raw = dict(shopify_payload["products"][0])
    raw["variants"] = [
        {**v, "price": "1110.00", "price_currency": "NOK"} for v in raw["variants"]
    ]
    respx.get("https://shop.example/products/thing.json").mock(
        return_value=httpx.Response(200, json={"product": raw})
    )
    async with httpx.AsyncClient() as client:
        status, product = await shopify.fetch_product(client, "shop.example", "thing")

    assert status == "ok" and product.currency == "NOK"


@respx.mock
async def test_one_product_without_price_currency_falls_back_to_the_cookie(shopify_payload):
    raw = shopify_payload["products"][0]
    respx.get("https://shop.example/products/thing.json").mock(
        return_value=httpx.Response(
            200, json={"product": raw}, headers={"set-cookie": "cart_currency=NOK; path=/"}
        )
    )
    async with httpx.AsyncClient() as client:
        status, product = await shopify.fetch_product(client, "shop.example", "thing")

    assert status == "ok" and product.currency == "NOK"


@respx.mock
async def test_only_the_first_page_needs_to_name_the_currency(shopify_payload):
    """A client that keeps its cookies to itself (curl_cffi) names nothing
    after page one; those pages are still the same catalogue in the same money."""
    base = "https://shop.example"
    repeats = -(-shopify.PAGE_SIZE // len(shopify_payload["products"]))
    full = {"products": shopify_payload["products"] * repeats}
    end_of_catalogue(base)
    respx.get(f"{base}/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=full, headers={"set-cookie": "cart_currency=NOK"})
    )
    later = dict(shopify_payload["products"][0], id=987654321)
    respx.get(f"{base}/products.json?limit=250&page=2").mock(
        return_value=httpx.Response(200, json={"products": [later]})
    )
    end_of_catalogue(base, page=3)
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert result.currency == "NOK"
    stored = [p.currency or result.currency for p in result.products]
    assert set(stored) == {"NOK"}


@respx.mock
async def test_a_redirect_that_set_the_cookie_still_counts(shopify_payload):
    respx.get("https://apex.example/products.json?limit=250").mock(
        return_value=httpx.Response(
            301, headers={"location": "https://www.apex.example/products.json?limit=250",
                          "set-cookie": "cart_currency=SEK; path=/; domain=other.example"},
        )
    )
    respx.get("https://www.apex.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get("https://apex.example/products.json?limit=250")
    assert shopify.served_currency(resp) == "SEK"


@pytest.mark.asks_meta
@respx.mock
async def test_an_answer_naming_nothing_asks_the_shop_not_the_record(shopify_payload):
    """The record may be a cookie from another address. A shop that stops
    saying what it serves is asked again, or dollars go in as kroner."""
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    meta = respx.get("https://shop.example/meta.json").mock(
        return_value=httpx.Response(200, json={"country": "US", "currency": "USD"})
    )
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="NOK")

    assert meta.call_count == 1
    assert result.currency == "USD"
    assert {p.currency for p in result.products} == {"USD"}


@pytest.mark.asks_meta
@respx.mock
async def test_a_shop_that_names_its_currency_is_not_asked_again(shopify_payload):
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload, headers={"set-cookie": "cart_currency=NOK"})
    )
    meta = respx.get("https://shop.example/meta.json")
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="USD")

    assert not meta.called and result.currency == "NOK"


@pytest.mark.asks_meta
@respx.mock
async def test_a_shop_that_answers_no_meta_keeps_the_record(shopify_payload):
    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    respx.get("https://shop.example/meta.json").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example/").mock(return_value=httpx.Response(200, text="<html></html>"))
    async with httpx.AsyncClient() as client:
        result = await shopify.fetch(client, "shop.example", currency="NOK")

    assert result.currency == "NOK"


def test_cart_currency_is_found_among_other_cookies():
    resp = httpx.Response(
        200, headers=[
            ("set-cookie", "_shopify_y=abc; path=/"),
            ("set-cookie", "cart_currency=nok; path=/; expires=Fri, 02 Oct 2026 10:48:36 GMT"),
        ],
    )
    assert shopify.served_currency(resp) == "NOK"
    # curl_cffi folds every Set-Cookie into one comma-joined value, and a
    # cookie's own expiry date has a comma in it too.
    folded = httpx.Response(200, headers={"set-cookie": (
        "_y=1; path=/; expires=Fri, 02 Oct 2026 10:48:36 GMT, cart_currency=NOK; path=/"
    )})
    assert shopify.served_currency(folded) == "NOK"
    assert shopify.served_currency(httpx.Response(200)) is None
    lookalike = httpx.Response(200, headers={"set-cookie": "old_cart_currency=EUR; path=/"})
    assert shopify.served_currency(lookalike) is None


@respx.mock
async def test_currency_falls_back_to_the_shops_country():
    """An older shop whose /meta.json names a country and no currency."""
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
    end_of_catalogue()
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

    end_of_catalogue()
    respx.get("https://shop.example/products.json?limit=250").mock(
        return_value=httpx.Response(200, json=shopify_payload)
    )
    async with httpx.AsyncClient() as client:
        await shopify.fetch(client, "shop.example", currency="USD", limiter=Watching())

    # The slot is released as soon as the response arrives; the success is
    # recorded after, once the status has been looked at. Twice: the catalogue
    # page, then the empty page that says it ended.
    assert events == [
        "enter:shop.example", "try:shop.example", "exit:shop.example", "ok:shop.example"
    ] * 2


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

    end_of_catalogue()
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
        def page(request):
            number = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=numbered_products(shopify_payload, number))

        respx.route(host="shop.example", path="/products.json").mock(side_effect=page)
        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", currency="USD", max_pages=2)

        assert result.ok
        assert not result.complete
        assert result.next_cursor == 3, "two full pages read from page one, carry on at three"

    @respx.mock
    async def test_a_finished_catalogue_asks_for_no_second_pass(self, shopify_payload):
        end_of_catalogue()
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
        end_of_catalogue(page=3)
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


class TestWhichOptionIsTheSize:
    """option1 is not always the size, and reading it as one loses the shop.

    Shopify does not type its options: a shop names them itself, and `option1`
    is simply whichever it put first. Measured across the live catalogue, 19
    shops holding 234,000 variants put the colour first — www.bananabenz.it,
    undefeated.com, www.fatbuddhastore.com among them — so their sizes read as
    NERO, BLU, BIANCO and matched nobody's size.
    """

    @staticmethod
    def _product(options, option1, option2):
        return {
            "id": 1, "handle": "thing", "title": "Thing", "options": options,
            "variants": [{
                "id": 11, "price": "50.00", "available": True, "sku": "X",
                "option1": option1, "option2": option2,
            }],
        }

    def _parse(self, options, option1, option2):
        payload = {"products": [self._product(options, option1, option2)]}
        return shopify.parse_products(payload, "https://shop.example")[0].variants[0]

    def test_the_colour_first_shop_is_read_by_name(self):
        variant = self._parse(
            [{"name": "Color", "position": 1}, {"name": "Size", "position": 2}],
            "NERO", "EU 42",
        )
        assert variant.size == "EU 42"
        assert variant.color == "NERO"

    def test_the_ordinary_shop_is_unchanged(self):
        variant = self._parse(
            [{"name": "Size", "position": 1}, {"name": "Color", "position": 2}],
            "EU 42", "NERO",
        )
        assert variant.size == "EU 42"
        assert variant.color == "NERO"

    @pytest.mark.parametrize("name", ["Taglia", "Größe", "Pointure", "Rozmiar", "Talla"])
    def test_a_size_is_a_size_in_any_language(self, name):
        """The list is not an English-speaking one: half of it is Italian."""
        variant = self._parse(
            [{"name": "Colore", "position": 1}, {"name": name, "position": 2}],
            "NERO", "EU 42",
        )
        assert variant.size == "EU 42"

    def test_naming_only_the_colour_still_says_where_the_size_is_not(self):
        variant = self._parse(
            [{"name": "Colour", "position": 1}], "NERO", "EU 42",
        )
        assert variant.size == "EU 42", "not the slot the colour was found in"
        assert variant.color == "NERO"

    def test_an_unrecognised_name_falls_back_to_the_old_order(self):
        """Most shops are right about position, so a shrug keeps the guess."""
        variant = self._parse(
            [{"name": "Style", "position": 1}, {"name": "Fit", "position": 2}],
            "EU 42", "NERO",
        )
        assert variant.size == "EU 42"
        assert variant.color == "NERO"

    def test_no_options_at_all_falls_back_too(self):
        variant = self._parse(None, "EU 42", "NERO")
        assert variant.size == "EU 42"
        assert variant.color == "NERO"


class TestTheWallAtPageOneHundred:
    """Shopify caps `page * limit` at 25,000, so the 101st page of 250 answers
    HTTP 400 "Page * Limit exceeds the 25000 limit". A cursor that walked into
    that wall used to stay there: five shops stopped being read on 1-2 September
    and 9,535 of their cards aged on the shelf until somebody asked why a fifth
    of it was three weeks old."""

    def test_the_last_page_is_derived_from_the_page_size(self):
        assert shopify.LAST_PAGE == 25_000 // shopify.PAGE_SIZE == 100

    @respx.mock
    async def test_a_cursor_past_the_wall_starts_the_catalogue_again(self):
        """Not an error to report — the catalogue is read in slices across runs,
        and after the last slice it begins again."""
        first = respx.get(
            f"https://shop.example/products.json?limit={shopify.PAGE_SIZE}"
        ).mock(return_value=httpx.Response(200, json={"products": []}))

        async with httpx.AsyncClient() as client:
            result = await shopify.fetch(client, "shop.example", "USD", cursor=101)

        assert first.called, "it asked for the first page, not the 101st"
        assert result.error is None

    @respx.mock
    async def test_a_cursor_inside_the_window_is_honoured(self):
        asked = respx.get(
            f"https://shop.example/products.json?limit={shopify.PAGE_SIZE}&page=7"
        ).mock(return_value=httpx.Response(200, json={"products": []}))

        async with httpx.AsyncClient() as client:
            await shopify.fetch(client, "shop.example", "USD", cursor=7)

        assert asked.called


@respx.mock
async def test_a_product_page_answering_something_that_is_not_an_object_is_unreachable():
    """`[]` with a 200 used to raise out of the spot-check and end the run."""
    respx.get("https://shop.example/products/thing.json").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        status, product = await shopify.fetch_product(client, "shop.example", "thing")
    assert (status, product) == ("unreachable", None)
