"""The ASOS adapter, against a page captured from the live catalogue API."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from pi.sources import asos, detect

FIXTURE = Path(__file__).parent / "fixtures" / "asos_search.json"
API = "https://www.asos.com/api/product/search/v2/categories/"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text())


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


class TestReadingTheCatalogue:
    @pytest.mark.asyncio
    @respx.mock
    async def test_a_captured_page_becomes_products(self, page):
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=page))

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1)

        assert result.ok
        assert len(result.products) == len(page["products"])
        assert result.currency == "USD"
        for product in result.products:
            assert product.title
            assert product.url.startswith("https://www.asos.com/")
            assert product.variants and product.variants[0].price > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_section_says_who_the_clothes_are_for(self, page):
        """An ASOS title very often does not; the category it came from does."""
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=page))

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1)

        from pi import taxonomy

        assert all(p.category == "Men Sale" for p in result.products)
        assert taxonomy.gender(result.products[0].title, result.products[0].category) == "men"

    @pytest.mark.asyncio
    @respx.mock
    async def test_an_unpriced_row_is_dropped_rather_than_priced_at_zero(self, page):
        broken = {**page, "products": [*page["products"], {"id": 1, "url": "prd/1", "name": "x"}]}
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=broken))

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1)

        assert len(result.products) == len(page["products"])


class TestWhatTheProductUsedToCost:
    """ASOS publishes the figure this project trusts most, so it is preferred."""

    def test_the_thirty_day_low_beats_the_struck_through_price(self):
        price = {
            "current": {"value": 55.0},
            "previous": {"value": 100.0},
            "lowestPriceInLast30Days": {"value": 70.0},
        }
        assert asos.reference(price, 55.0) == 70.0

    def test_a_price_that_has_not_moved_in_a_month_is_not_a_discount(self):
        """Captured live: $10 today, $10 all month, and a $20 anchor from December."""
        price = {
            "current": {"value": 10.0},
            "previous": {"value": 20.0},
            "lowestPriceInLast30Days": {"value": 10.0},
        }
        assert asos.reference(price, 10.0) is None

    def test_the_anchor_stands_in_when_there_is_no_thirty_day_figure(self):
        price = {"current": {"value": 30.0}, "previous": {"value": 50.0}}
        assert asos.reference(price, 30.0) == 50.0

    def test_the_fixture_disagrees_with_the_anchor_often_enough_to_matter(self, page):
        """Not a unit test of the rule but of why it exists."""
        differ = 0
        for item in page["products"]:
            price = item["price"]
            current = price["current"]["value"]
            anchor = (price.get("previous") or {}).get("value")
            if anchor and asos.reference(price, current) != anchor:
                differ += 1
        assert differ, "the captured page should show the two figures disagreeing"


class TestResumingWhereItStopped:
    @pytest.mark.asyncio
    @respx.mock
    async def test_a_full_section_moves_on_to_the_next_one(self, page):
        small = {**page, "itemCount": len(page["products"])}
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=small))

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1)

        assert result.next_cursor == asos.CURSOR_STRIDE  # section 1, offset 0
        assert not result.complete

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_last_section_finishing_means_the_catalogue_is_done(self, page):
        small = {**page, "itemCount": len(page["products"])}
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=small))
        start = (len(asos.CATALOGUES) - 1) * asos.CURSOR_STRIDE

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1, cursor=start)

        assert result.next_cursor == 0
        assert result.complete

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_cursor_carries_the_offset_within_a_section(self, page):
        seen: list[str] = []

        def record(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=page)

        respx.get(url__startswith=API).mock(side_effect=record)

        async with _client() as client:
            await asos.fetch(client, "www.asos.com", "USD", budget=1, cursor=400)

        assert "offset=400" in seen[0]

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_cursor_left_over_from_a_longer_list_starts_again(self, page):
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=page))
        stale = (len(asos.CATALOGUES) + 5) * asos.CURSOR_STRIDE

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=1, cursor=stale)

        assert result.products, "a stale cursor must not silently collect nothing"


class TestWhenTheApiStopsAnswering:
    @pytest.mark.asyncio
    @respx.mock
    async def test_a_first_page_that_fails_is_an_error_not_an_empty_shop(self):
        respx.get(url__startswith=API).mock(return_value=httpx.Response(403))

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=2)

        assert not result.ok
        assert not result.products

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_later_page_that_fails_keeps_the_slice_and_retries_it(self, page):
        answers = [httpx.Response(200, json=page), httpx.Response(503)]
        respx.get(url__startswith=API).mock(side_effect=answers)

        async with _client() as client:
            result = await asos.fetch(client, "www.asos.com", "USD", budget=5)

        assert result.ok
        assert result.products
        # Still inside the first section, pointing at the page that failed.
        assert result.next_cursor == len(page["products"])


class TestBeingRecognised:
    @pytest.mark.asyncio
    @respx.mock
    async def test_detection_reaches_for_the_adapter_before_the_generic_tests(self, page):
        respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=page))
        products = respx.get(url__startswith="https://www.asos.com/products.json")

        async with _client() as client:
            verdict = await detect.probe(client, "www.asos.com", allow_impersonation=False)

        assert verdict["platform"] == "asos"
        assert verdict["currency"] == "USD"
        assert not products.called, "the generic probes should not even be tried"

    @pytest.mark.asyncio
    @respx.mock
    async def test_an_adapter_that_stops_working_is_not_reported_as_working(self):
        """A hand-written adapter must fail loudly, not claim the shop is fine."""
        respx.get(url__startswith=API).mock(return_value=httpx.Response(403))
        respx.get(url__startswith="https://www.asos.com/products.json").mock(
            return_value=httpx.Response(403)
        )
        respx.get(url="https://www.asos.com").mock(return_value=httpx.Response(403))

        async with _client() as client:
            verdict = await detect.probe(client, "www.asos.com", allow_impersonation=False)

        assert verdict["platform"] != "asos"
