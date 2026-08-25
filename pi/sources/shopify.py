"""Shopify adapter.

Every Shopify store publishes its whole catalogue at /products.json — an
official, unauthenticated, paginated JSON endpoint carrying price,
compare_at_price, availability, SKU, options and image URLs. No HTML parsing
and no anti-bot to work around.

The one thing /products.json does NOT carry is the shop's currency, so it is
read from the storefront instead of guessed from the domain suffix: a .eu or
.com domain says nothing about whether prices are in EUR, GBP or USD.

Every request here goes through a single shared RateLimiter, because Shopify
counts requests against our IP across the whole platform rather than per shop.
See pi.throttle.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import httpx

from ..throttle import NullLimiter, RateLimiter
from .base import FetchResult, ScrapedProduct, ScrapedVariant

log = logging.getLogger(__name__)

PAGE_SIZE = 250
MAX_PAGES = 60    # 15,000 products; larger catalogues are logged, not silently cut
MAX_RETRIES = 3

_CURRENCY_JS = re.compile(
    r"""Shopify\.currency\s*=\s*\{[^}]*?["']active["']\s*:\s*["']([A-Z]{3})["']""",
    re.IGNORECASE,
)
_CURRENCY_META = re.compile(
    r"""<meta[^>]+(?:og:price:currency|product:price:currency)["'][^>]+content=["']([A-Z]{3})["']""",
    re.IGNORECASE,
)
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# Used only when the storefront gives nothing away.
_COUNTRY_CURRENCY = {
    "US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "NZ": "NZD",
    "JP": "JPY", "KR": "KRW", "CH": "CHF", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "TR": "TRY",
    "SG": "SGD", "HK": "HKD", "CN": "CNY", "BG": "BGN", "RO": "RON",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "BE": "EUR", "AT": "EUR", "PT": "EUR", "IE": "EUR", "FI": "EUR",
    "GR": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR", "LV": "EUR",
    "LT": "EUR", "LU": "EUR", "MT": "EUR", "CY": "EUR", "HR": "EUR",
}


async def detect_currency(
    client: httpx.AsyncClient, base: str, limiter: RateLimiter | NullLimiter | None = None
) -> str | None:
    """Read the shop's real currency from the storefront, then /meta.json."""
    limiter = limiter or NullLimiter()
    host = urlparse(base).netloc
    try:
        async with limiter.slot(host):
            resp = await client.get(base, follow_redirects=True)
        if resp.status_code == 200:
            html = resp.text
            for pattern in (_CURRENCY_JS, _CURRENCY_META):
                found = pattern.search(html)
                if found:
                    return found.group(1).upper()
    except httpx.HTTPError as exc:
        log.debug("%s: storefront unreadable for currency (%s)", base, exc)

    try:
        async with limiter.slot(host):
            resp = await client.get(f"{base}/meta.json")
        if resp.status_code == 200:
            country = (resp.json() or {}).get("country")
            if country:
                return _COUNTRY_CURRENCY.get(str(country).upper())
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.debug("%s: meta.json unreadable (%s)", base, exc)
    return None


async def _get_page(
    client: httpx.AsyncClient, url: str, limiter: RateLimiter | NullLimiter, host: str = ""
) -> httpx.Response | None:
    """GET through the shared limiter, backing off this shop — or all of them — on a 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        if getattr(limiter, "blocked", False):
            return None  # the platform has shut us out; retrying only prolongs it
        try:
            # The slot is held across the request: Shopify objects to parallel
            # requests from one IP, not to their rate.
            async with limiter.slot(host):
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            log.debug("%s: %s", url, exc)
            return None
        if resp.status_code == 200:
            limiter.note_success(host)
            return resp
        if resp.status_code in (429, 503):
            if attempt == MAX_RETRIES:
                return None
            # Shopify sends no Retry-After and stays angry for minutes, so the
            # pause is ours to choose. It applies to this shop; the limiter
            # decides for itself whether the whole sweep should slow down too.
            await limiter.penalise(_retry_after(resp, fallback=None), host=host)
            continue
        return None
    return None


def _retry_after(resp: httpx.Response, fallback: float | None) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return min(float(raw), 120.0) if raw else fallback
    except ValueError:
        return fallback


def _money(value) -> float | None:
    if value in (None, "", "0.00", "0"):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _image_for(product: dict, variant: dict) -> str | None:
    featured = variant.get("featured_image") or {}
    if isinstance(featured, dict) and featured.get("src"):
        return featured["src"]
    for image in product.get("images") or []:
        if isinstance(image, dict) and image.get("src"):
            return image["src"]
    return None


def parse_products(payload: dict, base: str) -> list[ScrapedProduct]:
    """Turn one /products.json body into our own shapes. Pure — no I/O, easy to test."""
    out: list[ScrapedProduct] = []
    for raw in payload.get("products") or []:
        handle = raw.get("handle") or ""
        variants: list[ScrapedVariant] = []
        image = None
        for rv in raw.get("variants") or []:
            price = _money(rv.get("price"))
            if price is None:
                continue
            compare = _money(rv.get("compare_at_price"))
            # A struck-through price at or below the asking price is noise.
            if compare is not None and compare <= price:
                compare = None
            image = image or _image_for(raw, rv)
            variants.append(
                ScrapedVariant(
                    external_id=str(rv.get("id") or ""),
                    price=price,
                    compare_at=compare,
                    in_stock=bool(rv.get("available", True)),
                    sku=(rv.get("sku") or None),
                    size=(rv.get("option1") or None),
                    color=(rv.get("option2") or None),
                )
            )
        if not variants:
            continue
        out.append(
            ScrapedProduct(
                external_id=str(raw.get("id") or handle),
                title=raw.get("title") or "Untitled",
                url=f"{base}/products/{handle}" if handle else base,
                brand=(raw.get("vendor") or None),
                image_url=image or _image_for(raw, {}),
                category=(raw.get("product_type") or None),
                variants=variants,
            )
        )
    return out


async def fetch(
    client: httpx.AsyncClient,
    domain: str,
    currency: str | None = None,
    max_pages: int = MAX_PAGES,
    limiter: RateLimiter | NullLimiter | None = None,
) -> FetchResult:
    """Pull a whole Shopify catalogue, paginating with ?page=N."""
    limiter = limiter or NullLimiter()
    if getattr(limiter, "blocked", False):
        return FetchResult(domain=domain, currency=currency, error="skipped: Shopify blocked this IP")
    base = f"https://{domain}".rstrip("/")
    if not currency:
        currency = await detect_currency(client, base, limiter)
        if not currency:
            return FetchResult(domain=domain, error="could not determine shop currency")

    products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()
    url = f"{base}/products.json?limit={PAGE_SIZE}"
    page_number = 1
    exhausted = False

    for _ in range(max_pages):
        resp = await _get_page(client, url, limiter, domain)
        if resp is None:
            if not products:
                return FetchResult(domain=domain, currency=currency, error="products.json unreachable")
            break
        try:
            payload = resp.json()
        except ValueError:
            break
        if not isinstance(payload, dict) or "products" not in payload:
            return FetchResult(domain=domain, currency=currency, error="not a Shopify catalogue")

        raw = payload.get("products") or []
        for product in parse_products(payload, base):
            if product.external_id not in seen_ids:
                seen_ids.add(product.external_id)
                products.append(product)

        # The storefront endpoint paginates with ?page=N and sends no Link header —
        # unlike the Admin API, whose cursor style is what Link is for. Honour a
        # Link header if one does turn up, otherwise step the page number on.
        link = _LINK_NEXT.search(resp.headers.get("Link", ""))
        if link:
            url = link.group(1)
            continue
        if len(raw) < PAGE_SIZE:
            exhausted = True
            break  # a short page is the last page
        page_number += 1
        url = f"{base}/products.json?limit={PAGE_SIZE}&page={page_number}"

    if not exhausted and len(products) >= max_pages * PAGE_SIZE:
        # Say so out loud. A catalogue quietly cut off at the page cap is the
        # kind of missing data nobody notices for months.
        log.warning(
            "%s: stopped at the %d-page cap with %d products — catalogue is larger",
            domain, max_pages, len(products),
        )

    return FetchResult(domain=domain, products=products, currency=currency)
