"""When a shop refuses us, and what counts as refusing.

Detection has to tell three things apart that all look like failure: a shop that
has gone, a shop that is throttling us, and a shop that is turning us away
because of what our connection looks like. Only the third is worth a second try.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from pi.sources import detect


def _a_browser_would_see(html: str):
    """Stand in for the impersonating client, which needs a real socket."""

    class Browser:
        headers = httpx.Headers({})
        timeout = httpx.Timeout(30.0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, follow_redirects: bool = True):
            if "products.json" in str(url):
                return httpx.Response(404, request=httpx.Request("GET", url))
            return httpx.Response(
                200, html=html, request=httpx.Request("GET", url)
            )

    return lambda **kwargs: Browser()


def _a_browser_would_not_reach_it_either():
    """The impersonating client for a domain that is genuinely gone."""

    class Browser:
        headers = httpx.Headers({})
        timeout = httpx.Timeout(30.0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, follow_redirects: bool = True, headers=None):
            raise httpx.ConnectError("no such host")

    return lambda **kwargs: Browser()


PRODUCT_PAGE = """
<html><head><script type="application/ld+json">
{"@type": "Product", "name": "Shoe",
 "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "USD",
            "availability": "https://schema.org/InStock"}}
</script></head><body>a shop</body></html>
"""


@pytest.mark.asyncio
@respx.mock
async def test_a_shop_that_answers_403_is_tried_as_a_browser(monkeypatch):
    """The behaviour that already existed, kept honest while the next one lands."""
    monkeypatch.setattr(detect.impersonate, "available", lambda: True)
    monkeypatch.setattr(
        detect.impersonate, "ImpersonatingClient", _a_browser_would_see(PRODUCT_PAGE)
    )
    respx.get(url__regex=r"https://shy\.example.*").mock(return_value=httpx.Response(403))

    async with httpx.AsyncClient() as client:
        verdict = await detect.probe(client, "shy.example")

    assert verdict["platform"] == "jsonld"
    assert verdict["impersonate"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_shop_that_never_answers_is_tried_as_a_browser_too(monkeypatch):
    """Not answering is also how a shop refuses.

    www.asos.com, www.mrporter.com and www.revolve.com let the connection hang
    rather than sending 403, so the retry never happened and all three were
    written off as dead. asos answers 200 to Chrome's handshake.
    """
    monkeypatch.setattr(detect.impersonate, "available", lambda: True)
    monkeypatch.setattr(
        detect.impersonate, "ImpersonatingClient", _a_browser_would_see(PRODUCT_PAGE)
    )
    respx.get(url__regex=r"https://silent\.example.*").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    async with httpx.AsyncClient() as client:
        verdict = await detect.probe(client, "silent.example")

    assert verdict["platform"] == "jsonld"
    assert verdict["impersonate"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_shop_that_is_really_gone_is_still_called_dead(monkeypatch):
    """The retry must not turn every dead domain into a maybe."""
    monkeypatch.setattr(detect.impersonate, "available", lambda: True)
    monkeypatch.setattr(
        detect.impersonate,
        "ImpersonatingClient",
        _a_browser_would_not_reach_it_either(),
    )
    respx.get(url__regex=r"https://gone\.example.*").mock(
        side_effect=httpx.ConnectError("no such host")
    )

    async with httpx.AsyncClient() as client:
        verdict = await detect.probe(client, "gone.example")

    assert verdict["platform"] == "dead"


@pytest.mark.asyncio
@respx.mock
async def test_a_certificate_problem_is_not_mistaken_for_a_refusal(monkeypatch):
    """A fixable certificate has its own repair path and must reach it."""
    monkeypatch.setattr(detect.impersonate, "available", lambda: True)
    monkeypatch.setattr(
        detect.impersonate, "ImpersonatingClient", _a_browser_would_see(PRODUCT_PAGE)
    )
    respx.get(url__regex=r"https://badcert\.example.*").mock(
        side_effect=httpx.ConnectError("CERTIFICATE_VERIFY_FAILED: unable to get issuer")
    )

    async with httpx.AsyncClient() as client:
        verdict = await detect.probe(client, "badcert.example")

    assert verdict["platform"] == "tls"


class TestHowLongASlowShopIsGiven:
    """Three shops were written off as dead for being slow rather than shut."""

    async def test_the_browser_retry_waits_longer_than_the_sweep_does(self, monkeypatch):
        import httpx

        from pi.sources import detect, impersonate

        seen: dict = {}

        class Fake:
            def __init__(self, timeout=30.0, headers=None):
                seen["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        monkeypatch.setattr(impersonate, "available", lambda: True)
        monkeypatch.setattr(impersonate, "ImpersonatingClient", Fake)
        monkeypatch.setattr(detect, "probe", _refuse)

        async with httpx.AsyncClient(timeout=5.0) as client:
            await detect._probe_as_a_browser(client, "slow.example", None, None)

        assert seen["timeout"] == detect.BROWSER_PROBE_TIMEOUT
        assert seen["timeout"] > 5.0, "the sweep's impatience must not carry over"


async def _refuse(*args, **kwargs):
    return {"platform": "dead"}


class TestWhatTheBrowserProbeIsAllowedToSay:
    """A shop that answers a browser but hides its prices is not a shut door.

    www.revolve.com, www.mrporter.com and www.zalando.de all answer 200 to a
    browser fingerprint and all render their prices in JavaScript. Throwing
    that away left them recorded as "не отвечает" and "закрыт анти-ботом",
    verdicts whose remedy is to wait. The real remedy is an adapter.
    """

    async def test_the_browser_saying_unknown_is_kept(self, monkeypatch):
        verdict = await _browser_verdict(monkeypatch, "unknown")
        assert verdict is not None
        assert verdict["platform"] == "unknown"
        assert verdict["impersonate"] == 1

    async def test_the_browser_saying_blocked_too_adds_nothing(self, monkeypatch):
        assert await _browser_verdict(monkeypatch, "blocked") is None

    async def test_a_readable_verdict_is_still_kept(self, monkeypatch):
        verdict = await _browser_verdict(monkeypatch, "shopify")
        assert verdict is not None and verdict["platform"] == "shopify"


async def _browser_verdict(monkeypatch, platform):
    import httpx

    from pi.sources import detect, impersonate

    class Fake:
        def __init__(self, timeout=30.0):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    async def answer(*args, **kwargs):
        return {"platform": platform}

    monkeypatch.setattr(impersonate, "available", lambda: True)
    monkeypatch.setattr(impersonate, "ImpersonatingClient", Fake)
    monkeypatch.setattr(detect, "probe", answer)

    async with httpx.AsyncClient(timeout=5.0) as client:
        return await detect._probe_as_a_browser(client, "shop.example", None, None)
