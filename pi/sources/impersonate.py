"""An HTTP client that presents a browser's TLS fingerprint.

Twenty-five stores answer our ordinary requests with 403 and nothing else.
Measured directly: sending Chrome's User-Agent and Accept-Language headers with
curl changes nothing, because what Akamai and Cloudflare are matching on is the
TLS handshake itself — cipher order, extensions, ALPN — which no amount of
header setting alters. curl_cffi links against a curl built to reproduce a real
browser's handshake, and 9 of those 25 then answer 200.

The dependency is optional. Without it `available()` is False, those stores stay
switched off exactly as they are today, and nothing else changes.

The shim exists so the adapters do not have to care which client they were
given: it takes httpx's argument names and raises httpx's exceptions, because
every caller in pi.sources already handles those.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on what is installed
    from curl_cffi import requests as _cffi
except ImportError:  # pragma: no cover
    _cffi = None

# Which browser to imitate. Kept current deliberately: an old Chrome is itself a
# fingerprint, and the point is to look like traffic a shop wants to serve.
BROWSER = "chrome"


def available() -> bool:
    """Is browser impersonation possible in this installation?"""
    return _cffi is not None


# curl has already decompressed the body, but the response still carries the
# Content-Encoding header saying otherwise. Handing both to httpx makes it decode
# a second time and fail — silently, as an unreadable page. www.offspring.co.uk
# publishes its sitemap in robots.txt and looked to us like a shop that has no
# robots.txt at all.
_ALREADY_APPLIED = ("content-encoding", "content-length")


def usable_headers(raw) -> dict[str, str]:
    """The response headers minus the ones curl has already acted on."""
    return {
        name: value for name, value in dict(raw).items()
        if name.lower() not in _ALREADY_APPLIED
    }


class ImpersonatingClient:
    """The part of httpx.AsyncClient the adapters actually use.

    Only `get` — that is the whole surface pi.sources needs, and a smaller shim
    is a shim that cannot drift away from what it is imitating.
    """

    def __init__(self, timeout: float = 30.0, headers: dict[str, str] | None = None):
        if _cffi is None:  # pragma: no cover - guarded by available()
            raise RuntimeError("curl_cffi is not installed")
        self.timeout = timeout
        self.headers = httpx.Headers(headers or {})
        self._session = _cffi.AsyncSession(
            impersonate=BROWSER, timeout=timeout, verify=True
        )

    async def __aenter__(self) -> ImpersonatingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._session.close()

    async def get(
        self,
        url: str,
        follow_redirects: bool = True,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """`headers` are merged over the session's, the way httpx merges them.

        An adapter that asks a shop's own JSON API has to send a Referer and an
        Accept with the request — httpx takes those per call, so a shim that
        silently did not was a shim the adapters could not actually be unaware
        of. It failed as a TypeError inside a probe that catches everything, so
        ASOS came back classified "blocked" with nothing in the log to say why.
        """
        merged = httpx.Headers(self.headers)
        merged.update(headers or {})
        try:
            resp = await self._session.get(
                str(url), allow_redirects=follow_redirects, headers=dict(merged)
            )
        except Exception as exc:  # curl_cffi raises its own hierarchy
            # Every caller in pi.sources catches httpx.HTTPError. Translating
            # here is what keeps the adapters unaware of which client they hold.
            raise httpx.ConnectError(f"{type(exc).__name__}: {exc}") from exc
        return httpx.Response(
            status_code=resp.status_code,
            headers=usable_headers(resp.headers),
            content=resp.content,
            request=httpx.Request("GET", url),
        )
