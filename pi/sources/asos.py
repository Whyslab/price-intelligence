"""ASOS, read through the API its own storefront calls.

ASOS is the largest catalogue on the list and, until now, one of the least
readable: its product pages carry no schema.org markup at all, so the generic
crawler classified the whole shop as `no schema.org/Product markup found` and
retired it. Nothing about that verdict was wrong — the prices genuinely are not
in the HTML. They are in a JSON endpoint the page fetches a moment later:

    /api/product/search/v2/categories/{id}?store=US&currency=USD&limit=200

which is unauthenticated, pages with `offset` to the end of a category, and
answers 200 to a browser's TLS fingerprint. One request brings 200 products
with price, brand, image, and the figure that makes this shop worth the special
case at all — see `reference` below.

There is no size information in that endpoint, and getting it costs one request
per product, so an ASOS product arrives as a single unsized variant. That is a
real loss: a size filter cannot match it. It is not a fatal one, because a size
you do not take raises the bar for a find rather than disqualifying it (see
pi.personal), and 26,000 products at an honest reference price are worth more
than nothing at all.
"""
from __future__ import annotations

import logging

import httpx

from .base import FetchResult, ScrapedProduct, ScrapedVariant

log = logging.getLogger(__name__)

DOMAINS = frozenset({"asos.com", "www.asos.com"})

# Only the sale sections are swept. ASOS carries about 100,000 products and
# roughly 26,000 of them are marked down; collecting the other three quarters
# would quadruple the cost of the shop to record full-price items that can
# never be a find. `range=sale` is ASOS's own flag, not a discount threshold of
# ours, so nothing is judged here — the scoring still happens later.
CATALOGUES: tuple[tuple[int, str], ...] = (
    (8409, "Men Sale"),
    (7046, "Women Sale"),
)
PAGE_SIZE = 200
# Pages per pass. 26,000 products is 130 pages; a pass takes a slice and says
# where to resume, the same way the Shopify and jsonld adapters do.
DEFAULT_BUDGET = 40
# Cursor packing: category index × STRIDE + offset within it. Larger than any
# single ASOS category (the biggest is about 22,000), so the two never collide.
CURSOR_STRIDE = 1_000_000

# ASOS serves one catalogue per market and names its markets by store code.
# The currency we already recorded for the shop decides which one to ask for.
MARKETS = {
    "USD": {"store": "US", "country": "US", "lang": "en-US"},
    "GBP": {"store": "COM", "country": "GB", "lang": "en-GB"},
    "EUR": {"store": "ROE", "country": "DE", "lang": "en-GB"},
    "AUD": {"store": "AU", "country": "AU", "lang": "en-AU"},
}
DEFAULT_CURRENCY = "USD"


def handles(domain: str) -> bool:
    """Is this a shop this adapter knows how to read?"""
    return domain.lower().lstrip(".") in DOMAINS


async def probe(client: httpx.AsyncClient, domain: str) -> dict | None:
    """One cheap request to confirm the API answers. None if it does not.

    Called from pi.sources.detect, which will retry the whole probe behind a
    browser fingerprint if the plain client gets nowhere — that retry is what
    ASOS actually needs, and it is the same machinery every other blocked shop
    already goes through.
    """
    try:
        payload = await _page(client, domain, CATALOGUES[0][0], MARKETS[DEFAULT_CURRENCY], 0, 1)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("%s: catalogue API did not answer (%s)", domain, exc)
        return None
    if not payload.get("products"):
        return None
    return {
        "platform": "asos",
        "name": "ASOS",
        "currency": _currency_of(payload) or DEFAULT_CURRENCY,
        "country": MARKETS[DEFAULT_CURRENCY]["country"],
    }


async def fetch(
    client: httpx.AsyncClient,
    domain: str,
    currency: str | None = None,
    budget: int = DEFAULT_BUDGET,
    cursor: int = 0,
) -> FetchResult:
    """Read up to `budget` pages of the sale catalogues, resuming at `cursor`."""
    market = MARKETS.get((currency or DEFAULT_CURRENCY).upper(), MARKETS[DEFAULT_CURRENCY])
    index, offset = divmod(max(0, int(cursor or 0)), CURSOR_STRIDE)
    if index >= len(CATALOGUES):  # a stale cursor from a shorter list of sections
        index, offset = 0, 0

    products: list[ScrapedProduct] = []
    found_currency: str | None = None
    spent = 0

    while index < len(CATALOGUES) and spent < budget:
        category, label = CATALOGUES[index]
        try:
            payload = await _page(client, domain, category, market, offset, PAGE_SIZE)
        except (httpx.HTTPError, ValueError) as exc:
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
            if not products:
                return FetchResult(domain=domain, error=error)
            # A page that failed half way is still a slice worth keeping; the
            # cursor points at the page that failed, so the next pass retries it.
            log.warning("%s: stopping after %d page(s) (%s)", domain, spent, error)
            break
        spent += 1
        found_currency = found_currency or _currency_of(payload)
        batch = payload.get("products") or []
        for item in batch:
            product = _product(item, label, domain)
            if product is not None:
                products.append(product)
        offset += len(batch)
        total = _int(payload.get("itemCount"))
        if not batch or (total is not None and offset >= total):
            index += 1
            offset = 0

    done = index >= len(CATALOGUES)
    return FetchResult(
        domain=domain,
        products=products,
        currency=found_currency or market_currency(market),
        next_cursor=0 if done else index * CURSOR_STRIDE + offset,
    )


def market_currency(market: dict) -> str:
    for code, spec in MARKETS.items():
        if spec is market:
            return code
    return DEFAULT_CURRENCY


async def _page(
    client: httpx.AsyncClient,
    domain: str,
    category: int,
    market: dict,
    offset: int,
    limit: int,
) -> dict:
    url = (
        f"https://{domain}/api/product/search/v2/categories/{category}"
        f"?store={market['store']}&currency={market_currency(market)}"
        f"&country={market['country']}&lang={market['lang']}"
        f"&limit={limit}&offset={offset}&range=sale"
    )
    resp = await client.get(
        url,
        headers={"Accept": "application/json", "Referer": f"https://{domain}/"},
        follow_redirects=True,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("catalogue API did not answer with an object")
    return payload


def _product(item: dict, label: str, domain: str) -> ScrapedProduct | None:
    external_id = str(item.get("id") or "").strip()
    price = item.get("price") or {}
    current = _money(price.get("current"))
    path = str(item.get("url") or "").strip()
    if not external_id or current is None or not path:
        return None
    variant = ScrapedVariant(
        external_id=external_id,
        price=current,
        compare_at=reference(price, current),
        in_stock=True,  # the catalogue only lists what can be bought
        sku=str(item.get("productCode") or "") or None,
        color=(item.get("colour") or None),
    )
    return ScrapedProduct(
        external_id=external_id,
        title=str(item.get("name") or "").strip() or external_id,
        url=path if path.startswith("http") else f"https://{domain}/{path.lstrip('/')}",
        brand=(item.get("brandName") or None),
        image_url=_image(item.get("imageUrl")),
        # Free text elsewhere in this project, and read for gender and kind.
        # "Men Sale" is what tells the classifier who these clothes are for:
        # an ASOS title alone very often does not say.
        category=label,
        variants=[variant],
        currency=str(price.get("currency") or "").upper() or None,
    )


def reference(price: dict, current: float) -> float | None:
    """What this product demonstrably used to cost.

    ASOS publishes `lowestPriceInLast30Days` because EU price-indication law
    requires the figure, and it is the same quantity this project computes from
    its own history and trusts above every other reference. So it is preferred
    over `previous`, which is a marketing anchor nobody has to justify.

    The consequence is deliberate: a product that has sat at its sale price for
    a month has a 30-day low equal to what it costs today, and therefore scores
    as no discount at all — which is exactly true. Only when the figure is
    missing does the struck-through price stand in for it.
    """
    if "lowestPriceInLast30Days" in price:
        # Present but not above today's price means the shop has not been
        # cheaper this month, and that settles it. Falling through to
        # `previous` here would put the anchor back — measured on the live
        # catalogue, a Nike top at $10 all month carries a $20 anchor last
        # charged in December and would have gone out as "half price".
        low = _money(price.get("lowestPriceInLast30Days"))
        return low if low is not None and low > current else None
    for key in ("previous", "rrp"):
        value = _money(price.get(key))
        if value is not None and value > current:
            return value
    return None


def _money(node) -> float | None:
    if isinstance(node, dict):
        node = node.get("value")
    try:
        value = float(node)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _image(raw) -> str | None:
    url = str(raw or "").strip()
    if not url:
        return None
    return url if url.startswith("http") else f"https://{url}"


def _currency_of(payload: dict) -> str | None:
    for item in payload.get("products") or []:
        code = str((item.get("price") or {}).get("currency") or "").upper()
        if len(code) == 3 and code.isalpha():
            return code
    return None
