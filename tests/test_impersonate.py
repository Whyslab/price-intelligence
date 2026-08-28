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
