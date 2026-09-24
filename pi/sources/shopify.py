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
from collections.abc import Collection
from urllib.parse import urlparse

import httpx

from ..throttle import NullLimiter, RateLimiter
from .base import FetchResult, ScrapedProduct, ScrapedVariant

log = logging.getLogger(__name__)

PAGE_SIZE = 250
# Pages read in one pass. Not a limit on catalogue size: a shop with more than
# this many pages is picked up where it was left off on the next run, which is
# the same mechanism that resumes a sweep Shopify cut short.
MAX_PAGES = 60
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
_CART_CURRENCY = re.compile(r"(?:^|[;,\s])cart_currency=([A-Za-z]{3})(?:[;,\s]|$)")


def served_currency(resp: httpx.Response) -> str | None:
    """The currency this particular answer is priced in, as the shop said so.

    With Shopify Markets on, /products.json is not quoted in the shop's base
    currency but in the one it chose for the visitor, and it names that choice
    in the `cart_currency` cookie of the very same answer — or, once the cookie
    is held, in the one the request carried back. Measured on 18.09.2026 from
    Norway: www.stadiumgoods.com states USD in /meta.json and served NOK, and
    seven other shops did the same, so from 2 September their kroner went into
    the database as dollars — a 1,095 kr Air Force 1 shown at $1095. The same
    cookie says GBP at www.slamcity.com, whose prices really are pounds, so
    trusting the answer over /meta.json keeps that fix without the damage.
    """
    # The last statement wins: a redirect may set the cookie on the way in,
    # and a later answer that sets it again has changed its mind.
    stated = None
    for answer in (*resp.history, resp):
        for value in answer.headers.get_list("set-cookie"):
            for found in _CART_CURRENCY.finditer(value):
                stated = found.group(1).upper()
    if stated:
        return stated
    try:
        sent = resp.request.headers.get("cookie", "")
    except RuntimeError:  # a response built by hand has no request behind it
        return None
    found = _CART_CURRENCY.search(sent)
    return found.group(1).upper() if found else None


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
    """Which currency /products.json is denominated in, before it has answered.

    Only the first guess: a Markets shop may quote the visitor's currency
    instead, and then the answer itself says so — see `served_currency`, which
    wins over this whenever the shop names one.

    `/meta.json` is asked first because it is the shop's own statement of its
    base currency, and that is what /products.json quotes otherwise. The storefront names
    something else: with Shopify Markets on, `Shopify.currency.active` is the
    currency chosen for *this visitor*, converted from the base for display, and
    which one that is depends on where the request appeared to come from.

    Reading the display currency and pricing the base-currency catalogue with it
    multiplies the whole shop by an exchange rate. Measured: www.slamcity.com
    quotes GBP and was recorded as NOK, so its Adidas Glenburn skate shoes went
    in at $6.96 instead of about $88 — and eleven shops were carrying a currency
    that does not belong to their country. It does not stay local either: those
    prices are what other shops are measured against through the market index,
    and slamcity disagreed with the consensus on 323 of 323 shared articles,
    more than any other shop in the catalogue.

    The storefront is still read, because a shop that answers no /meta.json
    still has to be priced somehow, and a display currency is right whenever
    the shop has no second market.
    """
    limiter = limiter or NullLimiter()
    host = urlparse(base).netloc
    try:
        async with limiter.slot(host):
            resp = await client.get(f"{base}/meta.json")
        if resp.status_code == 200:
            meta = resp.json() or {}
            stated = str(meta.get("currency") or "").strip().upper()
            if len(stated) == 3 and stated.isalpha():
                return stated
            country = meta.get("country")
            if country:
                return _COUNTRY_CURRENCY.get(str(country).upper())
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.debug("%s: meta.json unreadable (%s)", base, exc)

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
    return None


async def currency_when_unstated(
    client: httpx.AsyncClient, base: str, limiter: RateLimiter | NullLimiter,
    recorded: str | None,
) -> str | None:
    """The shop's own statement, for an answer that named no currency at all."""
    return await detect_currency(client, base, limiter) or recorded


async def _get_page(
    client: httpx.AsyncClient, url: str, limiter: RateLimiter | NullLimiter, host: str = ""
) -> httpx.Response | None:
    """GET through the shared limiter, backing off this shop — or all of them — on a 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        if await limiter.confirm_blocked():
            return None  # the platform has shut us out; retrying only prolongs it
        try:
            # The slot is held across the request: Shopify objects to parallel
            # requests from one IP, not to their rate.
            async with limiter.slot(host):
                limiter.note_attempt(host)
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


# What shops call the two options that matter, in the languages the list uses.
# Shopify does not type its options — a shop names them itself and the names are
# all it publishes about which is which.
_SIZE_NAMES = {
    "size", "sizes", "shoe size", "taglia", "taglie", "größe", "grösse", "grosse",
    "talla", "tallas", "taille", "pointure", "maat", "rozmiar", "storlek",
    "størrelse", "koko", "méret", "velikost", "mărime", "размер",
}
_COLOUR_NAMES = {
    "color", "colour", "colors", "colours", "colore", "colori", "farbe", "kleur",
    "couleur", "kolor", "färg", "farve", "väri", "szín", "barva", "culoare", "цвет",
}


def _option_slots(raw: dict) -> tuple[int, int]:
    """Which option holds the size and which the colour, as 0-based positions.

    Shopify variants carry option1/2/3 with no indication of what they mean, and
    reading option1 as the size is a guess that a lot of shops break: measured
    across the live catalogue, 19 shops holding 234,000 variants put the colour
    first, so their sizes read as NERO, BLU, BIANCO. Nothing then matches a size
    filter, and the bot offers a shoe in "MULTICOLORE".

    The product's own `options` array names them, so ask it. Positions are the
    fallback for a shop that names nothing recognisable — the old behaviour, kept
    because for most shops it is right.
    """
    size = colour = None
    for option in raw.get("options") or []:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name") or "").strip().lower()
        try:
            slot = int(option.get("position", 0)) - 1
        except (TypeError, ValueError):
            continue
        if slot < 0:
            continue
        if size is None and name in _SIZE_NAMES:
            size = slot
        elif colour is None and name in _COLOUR_NAMES:
            colour = slot
    if size is None:
        # Never hand back the slot the colour was found in: a shop that names
        # only its colour has still told us where the size is not.
        size = 1 if colour == 0 else 0
    if colour is None:
        colour = 1 if size != 1 else 0
    return size, colour


def _listed_id(raw) -> str:
    """How a page names one of its products, whether or not it parses."""
    if isinstance(raw, dict):
        return str(raw.get("id") or raw.get("handle") or "")
    return ""


def parse_products(payload: dict, base: str) -> list[ScrapedProduct]:
    """Turn one /products.json body into our own shapes. Pure — no I/O, easy to test."""
    out: list[ScrapedProduct] = []
    for raw in payload.get("products") or []:
        if not isinstance(raw, dict):
            continue
        handle = raw.get("handle") or ""
        variants: list[ScrapedVariant] = []
        image = None
        size_slot, colour_slot = _option_slots(raw)
        options = ("option1", "option2", "option3")
        for rv in raw.get("variants") or []:
            if not isinstance(rv, dict):
                continue
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
                    size=(rv.get(options[size_slot]) or None) if size_slot < 3 else None,
                    color=(rv.get(options[colour_slot]) or None) if colour_slot < 3 else None,
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


async def fetch_product(
    client: httpx.AsyncClient,
    domain: str,
    handle: str,
    limiter: RateLimiter | NullLimiter | None = None,
) -> tuple[str, ScrapedProduct | None]:
    """One product by its handle. Returns ("ok" | "gone" | "unreachable", product).

    The single-product form of the catalogue endpoint, and the only affordable
    way to ask about a product in a shop too large to read in one pass. One
    request, one answer, through the same limiter as everything else — the
    Shopify quota counts our IP, and a check that ignored it would simply take
    the sweep's requests.

    The three answers are kept apart on purpose. A 404 is the shop saying the
    product is gone, and that is what this is asked for. A timeout, a 429 or a
    503 is our side of the conversation failing, and reading it as "gone" would
    eventually delete a shop's catalogue because its server had a bad minute.
    """
    limiter = limiter or NullLimiter()
    if await limiter.confirm_blocked():
        return "unreachable", None
    base = f"https://{domain}".rstrip("/")
    try:
        async with limiter.slot(domain):
            limiter.note_attempt(domain)
            resp = await client.get(f"{base}/products/{handle}.json")
    except httpx.HTTPError as exc:
        log.debug("%s/%s: %s", domain, handle, exc)
        return "unreachable", None

    if resp.status_code in (404, 410):
        # The shop answered, and clearly. That is a successful conversation.
        limiter.note_success(domain)
        return "gone", None
    if resp.status_code != 200:
        if resp.status_code in (429, 503):
            await limiter.penalise(_retry_after(resp, fallback=None), host=domain)
        return "unreachable", None
    limiter.note_success(domain)
    try:
        payload = resp.json()
    except ValueError:
        return "unreachable", None
    if not isinstance(payload, dict):
        # `[]` or `null` with a 200: not an answer about this product either way.
        return "unreachable", None
    raw = payload.get("product")
    if not isinstance(raw, dict):
        return "unreachable", None
    parsed = parse_products({"products": [raw]}, base)
    if parsed:
        # The single-product form names its currency on every variant; the
        # cookie is the same statement, kept for an answer that leaves it out.
        stated = next(
            (str(v.get("price_currency") or "") for v in raw.get("variants") or []
             if isinstance(v, dict) and v.get("price_currency")),
            "",
        ).strip().upper()
        parsed[0].currency = (
            stated if len(stated) == 3 and stated.isalpha() else served_currency(resp)
        )
    # Still published, but with nothing left that has a price: no variant can be
    # bought, which is the same thing as gone as far as a shelf is concerned.
    return ("ok", parsed[0]) if parsed else ("gone", None)


# The furthest `?page=` Shopify will answer, given the page size above: it caps
# `page * limit` at 25,000 and returns HTTP 400 past it.
SHOPIFY_PAGE_WINDOW = 25_000
LAST_PAGE = SHOPIFY_PAGE_WINDOW // PAGE_SIZE


async def fetch(
    client: httpx.AsyncClient,
    domain: str,
    currency: str | None = None,
    max_pages: int = MAX_PAGES,
    limiter: RateLimiter | NullLimiter | None = None,
    cursor: int = 0,
    known: Collection[str] | None = None,
) -> FetchResult:
    """Read a slice of a Shopify catalogue, paginating with ?page=N.

    Starts at `cursor` (a page number, 0 meaning the beginning) and reports in
    `next_cursor` where to carry on, so a catalogue larger than one pass — or a
    pass that Shopify cut short — is finished by the following run instead of
    being quietly truncated to whatever arrived first.

    `known` is the ids of the products the database holds as on sale here. A
    pass from the beginning that meets an empty page while some of them are
    still unlisted asks one page further before calling it the end (see below).
    """
    limiter = limiter or NullLimiter()
    # Waits out a suspected block rather than giving up on the spot. A store
    # failed in the same second the breaker tripped can never benefit from the
    # breaker being wrong, and it has been wrong on every run so far.
    if await limiter.confirm_blocked():
        return FetchResult(
            domain=domain, currency=currency, error="skipped: Shopify blocked this IP",
            next_cursor=cursor,
        )
    base = f"https://{domain}".rstrip("/")
    if not currency:
        currency = await detect_currency(client, base, limiter)
        if not currency:
            return FetchResult(domain=domain, error="could not determine shop currency")

    products: list[ScrapedProduct] = []
    seen_ids: set[str] = set()
    # Ids as the pages listed them, before parsing drops the unpriced: a page
    # of products that all fail to parse is still a page of the catalogue.
    listed_ids: set[str] = set()
    followed_link = previous_full = probed = False
    page_number = max(1, cursor)
    # Shopify refuses `page * limit` beyond 25,000 — the 101st page of 250
    # answers HTTP 400 "Page * Limit exceeds the 25000 limit" — and a cursor
    # that walked into that wall used to stay there. Five shops stopped being
    # read on 1-2 September and 9,535 of their cards aged on the shelf until
    # somebody asked why a fifth of it was three weeks old. The catalogue is
    # read in slices across runs; after the last slice it starts again.
    if page_number > LAST_PAGE:
        page_number = 1
    url = f"{base}/products.json?limit={PAGE_SIZE}"
    if page_number > 1:
        url += f"&page={page_number}"
    exhausted = False

    for _ in range(max_pages):
        resp = await _get_page(client, url, limiter, domain)
        if resp is None:
            if not products:
                return FetchResult(
                    domain=domain, currency=currency,
                    error="products.json unreachable", next_cursor=cursor,
                )
            break
        try:
            payload = resp.json()
        except ValueError:
            break
        if not isinstance(payload, dict) or "products" not in payload:
            if products:
                break  # pages already read are still a result; resume from here
            return FetchResult(domain=domain, currency=currency, error="not a Shopify catalogue")

        raw = payload.get("products") or []
        served = served_currency(resp)
        if served is None and not products:
            # The record may be a cookie from another day and another address:
            # a collector that moves country and meets a shop which no longer
            # says what it serves would otherwise store dollars as kroner, ten
            # times too cheap and every one of them a "deal". Only the first
            # page asks — later ones are the same answer, and a client that
            # keeps its cookies to itself (curl_cffi) names nothing after it.
            served = await currency_when_unstated(client, base, limiter, currency)
        if served and served != currency:
            log.info("%s: priced in %s, not the %s on record", domain, served, currency)
            currency = served
        for product in parse_products(payload, base):
            if product.external_id not in seen_ids:
                seen_ids.add(product.external_id)
                product.currency = served
                products.append(product)

        # The storefront endpoint paginates with ?page=N and sends no Link header —
        # unlike the Admin API, whose cursor style is what Link is for. A Link
        # header, if one does turn up, is the authority on whether there is more,
        # so it is consulted before the short-page rule.
        link = _LINK_NEXT.search(resp.headers.get("Link", ""))
        if link:
            followed_link = True
            page_number += 1
            url = link.group(1)
            continue
        if followed_link:
            # A walk the shop led by Link headers ends where the links end; its
            # page numbers mean nothing to ?page=, and asking one anyway could
            # leave a cursor pointing at a page that will never answer.
            exhausted = True
            break
        # Only an empty page ends the catalogue. A short one does not: Shopify
        # cuts the page first and removes what this visitor may not buy
        # afterwards, so a page comes back short in the middle of the list.
        # Measured on 23.09.2026: shop.simon.com answered 245 products on page
        # one and 250 on page two, www.sneakersnstuff.com 221 and then 227.
        # Reading the short page as the last one told the run it had seen the
        # whole shop, and 76,062 of shop.simon.com's products — and 23,955 of
        # italist's, 4,182 of Sneakersnstuff's — were marked as withdrawn while
        # every one of them was still for sale. The price of knowing is one
        # empty page per finished pass.
        fresh = {_listed_id(item) for item in raw} - listed_ids
        listed_ids |= fresh
        if raw and not fresh:
            # A page of nothing new: a storefront ignoring ?page=, or the list
            # shifting under the walk. Neither says the catalogue ended, so the
            # pass stops without claiming it read everything.
            break
        if not raw:
            unlisted = cursor <= 1 and known is not None and any(
                product_id not in listed_ids for product_id in known
            )
            if (previous_full or unlisted) and not probed:
                # An empty page is either the end of the catalogue or a page
                # whose every product this visitor may not buy — 250 in a row
                # filtered out after the cut. After a full page either is as
                # likely. After a short one it is usually the end, and asking
                # every time would cost a request per pass; but when products
                # the shop was selling have not come up yet, calling it the end
                # marks them withdrawn. www.italist.com on 24.09.2026: a pass
                # ended on an empty page after 2,841 of its 44,342 products.
                # One more page tells which.
                probed = True
                page_number += 1
                if page_number > LAST_PAGE:
                    break
                url = f"{base}/products.json?limit={PAGE_SIZE}&page={page_number}"
                continue
            exhausted = True
            break
        previous_full, probed = len(raw) >= PAGE_SIZE, False
        page_number += 1
        if page_number > LAST_PAGE:
            # Past the window Shopify answers at all. Asking anyway costs a
            # request and returns HTTP 400; the next pass starts again at one.
            break
        url = f"{base}/products.json?limit={PAGE_SIZE}&page={page_number}"

    if exhausted:
        # Everything the shop lists, but only when this pass began at the
        # beginning. Resuming at page 40 and reaching the end reads the tail of
        # a catalogue, and calling that an enumeration would mark the first
        # thirty-nine pages as withdrawn.
        return FetchResult(
            domain=domain, products=products, currency=currency,
            enumerated=cursor <= 1,
        )

    # Stopped without reaching the end: the page cap, or a refusal part way
    # through. Either way say where to resume. Reporting this as a finished
    # catalogue is how www.flatspot.com came to sit at 1,000 products.
    log.info(
        "%s: read %d products and stopped at page %d — resuming there next run",
        domain, len(products), page_number,
    )
    return FetchResult(
        domain=domain, products=products, currency=currency, next_cursor=page_number
    )
