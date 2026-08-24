"""Generic adapter for everything that is not Shopify.

Magento, WooCommerce, BigCommerce, Squarespace and most bespoke shops all embed
schema.org/Product markup as JSON-LD, because Google Shopping requires it. One
parser therefore covers many platforms — which is why this replaces the
per-platform adapters entirely.

Two honest limitations, both handled rather than hidden:

* There is no `compare_at_price` in schema.org, so these stores contribute no
  "was/now" signal. Their discounts are found from our own recorded price
  history instead (see pi.deals).
* Prices live on individual product pages, so a catalogue costs one request per
  product. Each run therefore walks a bounded slice of the store's sitemap and
  saves a cursor, covering the whole catalogue over successive runs.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from .base import FetchResult, ScrapedProduct, ScrapedVariant

log = logging.getLogger(__name__)

DEFAULT_BUDGET = 200
PER_HOST_CONCURRENCY = 4
SITEMAP_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap/products.xml")

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_LD_BLOCK = re.compile(
    r"""<script[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
# Positive hints are a preference, not a requirement: plenty of shops serve
# products from clean paths like /mens/footwear/nike-air-max-90/ with no marker
# at all, and demanding a marker threw those catalogues away wholesale.
_PRODUCT_HINTS = ("/product", "/products/", "/p/", "/shop/", "/item", ".html")
_NON_PRODUCT_HINTS = (
    "/blog", "/news", "/pages/", "/collections/", "/category", "/categories",
    "/brand", "/manufacturer", "/customer", "/account", "/cart", "/checkout",
    "/search", "/sitemap", "/policies", "/pictures", "/picutres", "/about",
    "/contact", "/terms", "/privacy", "/faq", "/login", "/register", "/wishlist",
)

AVAILABLE = {"instock", "in_stock", "limitedavailability", "onlineonly", "presale", "backorder"}


def _text(value) -> str | None:
    """schema.org fields are strings, dicts or lists depending on the vendor."""
    if value is None:
        return None
    if isinstance(value, str):
        return html.unescape(value).strip() or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@id") or value.get("url"))
    if isinstance(value, list):
        for item in value:
            found = _text(item)
            if found:
                return found
    return None


def _price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,]", "", value)
        if not cleaned:
            return None
        # 1.299,00 -> 1299.00 ; 1,299.00 -> 1299.00
        if "," in cleaned and "." in cleaned:
            cleaned = (
                cleaned.replace(".", "").replace(",", ".")
                if cleaned.rindex(",") > cleaned.rindex(".")
                else cleaned.replace(",", "")
            )
        elif "," in cleaned:
            cleaned = cleaned.replace(",", "." if len(cleaned.split(",")[-1]) == 2 else "")
        try:
            amount = float(cleaned)
        except ValueError:
            return None
        return amount if amount > 0 else None
    return None


def iter_ld_products(page: str):
    """Yield every schema.org Product node found in a page's JSON-LD."""
    for block in _LD_BLOCK.findall(page):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        seen = 0
        while stack and seen < 200:
            node = stack.pop()
            seen += 1
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            for key in ("@graph", "itemListElement", "mainEntity"):
                nested = node.get(key)
                if isinstance(nested, (list, dict)):
                    stack.append(nested)
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(isinstance(t, str) and "Product" in t for t in types):
                yield node


def parse_product(page: str, url: str) -> tuple[ScrapedProduct, str] | None:
    """Extract the first usable Product from a page. Pure — no I/O.

    Returns (product, currency) or None when the page carries no priced product.
    """
    for node in iter_ld_products(page):
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = next((o for o in offers if isinstance(o, dict)), None)
        if not isinstance(offers, dict):
            continue

        price = _price(offers.get("price"))
        currency = _text(offers.get("priceCurrency"))
        if price is None or not currency:
            # Some shops put both inside priceSpecification instead of on the Offer.
            spec = offers.get("priceSpecification")
            if isinstance(spec, list):
                spec = next((s for s in spec if isinstance(s, dict)), None)
            if isinstance(spec, dict):
                price = price if price is not None else _price(spec.get("price"))
                currency = currency or _text(spec.get("priceCurrency"))
        if price is None or not currency:
            continue

        availability = (_text(offers.get("availability")) or "instock").rsplit("/", 1)[-1]
        title = _text(node.get("name"))
        if not title:
            continue
        sku = _text(node.get("sku")) or _text(node.get("mpn"))

        image = node.get("image")
        if isinstance(image, dict):
            image = image.get("url") or image.get("contentUrl")
        elif isinstance(image, list):
            image = next(
                (i if isinstance(i, str) else (i or {}).get("url") for i in image if i), None
            )
        image = urljoin(url, html.unescape(image).strip()) if isinstance(image, str) else None

        product = ScrapedProduct(
            external_id=sku or url,
            title=title,
            url=_text(node.get("url")) or url,
            brand=_text(node.get("brand")),
            image_url=image,
            category=_text(node.get("category")),
            variants=[
                ScrapedVariant(
                    external_id=sku or "default",
                    price=price,
                    compare_at=None,  # schema.org has no struck-through price
                    in_stock=availability.lower().replace(" ", "") in AVAILABLE,
                    sku=sku,
                )
            ],
        )
        return product, currency.upper()
    return None


def _looks_like_a_page(url: str) -> bool:
    """Drop the homepage and bare section roots; keep anything with a real slug.

    A product slug almost always carries a hyphen ("nike-air-max-90"), while
    section roots ("/mens/footwear/") do not.
    """
    path = urlparse(url).path.strip("/")
    if not path:
        return False
    last = path.rsplit("/", 1)[-1]
    return "-" in last or "_" in last or len(last) > 24


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return None
    return resp if resp.status_code == 200 else None


async def discover_product_urls(
    client: httpx.AsyncClient, base: str, limit: int = 5000
) -> list[str]:
    """Walk sitemap.xml (following one level of sitemap index) for product pages."""
    host = urlparse(base).netloc
    for candidate in SITEMAP_CANDIDATES:
        resp = await _get(client, base + candidate)
        if resp is None:
            continue
        locs = [html.unescape(loc) for loc in _LOC.findall(resp.text)]
        if not locs:
            continue

        children = [loc for loc in locs if loc.lower().endswith((".xml", ".xml.gz")) or "sitemap" in loc.lower()]
        pages = [loc for loc in locs if loc not in children]

        # Prefer child sitemaps whose name suggests products.
        children.sort(key=lambda u: 0 if "product" in u.lower() else 1)
        for child in children[:12]:
            if len(pages) >= limit:
                break
            child_resp = await _get(client, child)
            if child_resp is not None:
                pages.extend(html.unescape(loc) for loc in _LOC.findall(child_resp.text))

        candidates = [
            u for u in dict.fromkeys(pages)
            if urlparse(u).netloc == host
            and not u.lower().endswith((".xml", ".xml.gz", ".jpg", ".png", ".webp"))
            and not any(bad in u.lower() for bad in _NON_PRODUCT_HINTS)
        ]
        # A URL that announces itself as a product is taken at its word.
        marked = [u for u in candidates if any(h in u.lower() for h in _PRODUCT_HINTS)]
        if marked:
            return marked[:limit]
        # Otherwise fall back to every URL with a real slug and let the JSON-LD
        # parser be the judge. Crawling is budgeted per run anyway, so a few
        # pages that turn out not to be products cost little — while demanding
        # a marker discards whole catalogues that use clean paths.
        plausible = [u for u in candidates if _looks_like_a_page(u)]
        if plausible:
            return plausible[:limit]
    return []


async def fetch(
    client: httpx.AsyncClient,
    domain: str,
    currency: str | None = None,
    budget: int = DEFAULT_BUDGET,
    cursor: int = 0,
) -> tuple[FetchResult, int]:
    """Crawl `budget` product pages starting at `cursor`. Returns (result, next_cursor)."""
    base = f"https://{domain}".rstrip("/")
    urls = await discover_product_urls(client, base)
    if not urls:
        return FetchResult(domain=domain, error="no product URLs in sitemap"), 0

    start = cursor % len(urls)
    window = (urls + urls)[start : start + budget]
    next_cursor = (start + len(window)) % len(urls)

    semaphore = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    products: list[ScrapedProduct] = []
    currencies: dict[str, int] = {}

    async def one(url: str) -> None:
        async with semaphore:
            resp = await _get(client, url)
        if resp is None:
            return
        parsed = parse_product(resp.text, url)
        if parsed is None:
            return
        product, found_currency = parsed
        products.append(product)
        currencies[found_currency] = currencies.get(found_currency, 0) + 1

    await asyncio.gather(*(one(u) for u in window))

    if not products:
        return FetchResult(domain=domain, error="no schema.org/Product markup found"), next_cursor

    dominant = currency or max(currencies, key=currencies.get)
    return FetchResult(domain=domain, products=products, currency=dominant), next_cursor
