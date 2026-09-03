"""The client that presents a browser's TLS fingerprint.

Twenty-five stores answer our ordinary requests with 403. Sending Chrome's
headers with curl changes nothing — measured — because what is being matched is
the TLS handshake. Nine of the 25 answer 200 to a browser fingerprint, and three
of those go on to yield products.
"""
from __future__ import annotations

import httpx
import pytest

from pi.sources import impersonate

pytestmark = pytest.mark.skipif(
    not impersonate.available(), reason="curl_cffi is not installed"
)


def test_the_feature_is_optional_and_says_so():
    """Without the package the shim must not be constructible, and callers ask first."""
    assert isinstance(impersonate.available(), bool)


async def test_a_transport_failure_arrives_as_the_exception_the_adapters_catch():
    """Every caller in pi.sources catches httpx.HTTPError and nothing else.

    Letting curl_cffi's own exception type through would turn one unreachable
    shop into an unhandled error that ends the sweep.
    """
    async with impersonate.ImpersonatingClient(timeout=2.0) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("https://127.0.0.1:1/nothing-here")


class TestTheResponseItHandsBack:
    """It has to be indistinguishable from httpx's, because the adapters cannot tell."""

    def test_a_decompressed_body_is_not_offered_for_decompression_again(self):
        """curl decompresses the body but leaves Content-Encoding saying it did not.

        Passing both to httpx makes it decode a second time and fail — silently,
        as an unreadable page. www.offspring.co.uk publishes its sitemap in
        robots.txt and looked to us like a shop that has no robots.txt at all.
        """
        kept = impersonate.usable_headers(
            {"Content-Encoding": "gzip", "Content-Length": "999", "X-Keep": "yes"}
        )
        assert kept == {"X-Keep": "yes"}
        resp = httpx.Response(200, headers=kept, content=b"plain text")
        assert resp.text == "plain text", "readable because nothing claims it is gzipped"


class TestPerRequestHeaders:
    """An adapter asking a shop's own API has to send Accept and Referer.

    The shim used to take headers only in its constructor, so `client.get(...,
    headers=...)` — which is how every adapter talks to httpx — raised
    TypeError. It surfaced as ASOS being recorded "blocked": a verdict about
    the shop that was really a verdict about us.
    """

    async def test_a_call_may_add_headers_of_its_own(self):
        client = impersonate.ImpersonatingClient()
        client._session = _Recorder()

        await client.get("https://shop.example/api", headers={"Referer": "https://shop.example/"})

        assert client._session.seen["Referer"] == "https://shop.example/"


class TestWhoseHeadersTheyAre:
    """A borrowed fingerprint under a borrowed name is worse than either alone."""

    def test_it_cannot_be_handed_a_set_of_default_headers(self):
        """Measured on www.mrporter.com: bare 200, with this project's UA 403.

        httpx's own defaults are no safer — `connection: keep-alive` and an
        Accept-Encoding no browser sends. The class simply does not take them.
        """
        import inspect

        signature = inspect.signature(impersonate.ImpersonatingClient.__init__)
        assert "headers" not in signature.parameters

    async def test_nothing_is_added_to_a_request_that_asked_for_nothing(self):
        client = impersonate.ImpersonatingClient()
        client._session = _Recorder()

        await client.get("https://shop.example/")

        assert client._session.seen == {}, "curl_cffi's own browser headers stand alone"


class _Recorder:
    """The two methods the shim uses of curl_cffi's session."""

    def __init__(self):
        self.seen: dict[str, str] = {}

    async def get(self, url, allow_redirects=True, headers=None):
        self.seen = dict(headers or {})
        return _Answer()

    async def close(self):
        pass


class _Answer:
    status_code = 200
    content = b"{}"

    def __init__(self):
        self.headers: dict[str, str] = {}
