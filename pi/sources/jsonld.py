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
* Not every shop has a usable sitemap. Measured on the live list, 36 stores
  reported "no product URLs": for eobuwie.pl, modivo.pl, basket4ballers.com and
  dripla.com /sitemap.xml answers 404 outright, while www.ageha.it serves a
  perfectly valid sitemap containing five links, none of them a product. For
  those the catalogue is found the way a person would find it — by following
  category links from the front page — see `crawl_product_urls`.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

import httpx

from .base import FetchResult, ScrapedProduct, ScrapedVariant

log = logging.getLogger(__name__)

# Product pages read from one shop in one run. Every product costs a request,
# so this is a bargain struck with somebody else's servers rather than a number
# to maximise. At 200 the large catalogues were not moving: www.footlocker.com
# sat at 133 products and www.ssense.com at 200 while the cursor inched through
# their sitemaps. Overridable with --jsonld-budget or PI_JSONLD_BUDGET.
DEFAULT_BUDGET = 400
PER_HOST_CONCURRENCY = 4
# Two failures that say something durable about the shop rather than about the
# network, so the pipeline retires a store that has only ever produced one of
# them. The exact wording is therefore part of the contract between the two
# modules and must not be typed out twice: pipeline.py held its own copy reading
# "no product URLs in sitemap", the message here grew the words "the" and "or by
# crawling", and the two stopped matching. Nobody noticed, because the failure
# mode is silent — three shops that will never publish a price went on being
# crawled every hour for a week.
NO_PRODUCT_URLS = "no product URLs in the sitemap or by crawling"
NO_MARKUP = "no schema.org/Product markup found"
# Not one product page answered 200: a wall or an outage, not a markup problem.
PAGES_WOULD_NOT_LOAD = "product pages would not load"

SITEMAP_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap/products.xml")
# robots.txt is where a site is supposed to declare its sitemap, and plenty put
# it somewhere none of the guesses above would find: /shop/sitemapindex.xml,
# /1_index_sitemap.xml, /sitemap/<host>/sitemap.xml. Measured on the live list,
# five of six stores reporting "no product URLs" had one declared here.
_ROBOTS_SITEMAP = re.compile(r"^\s*sitemap\s*:\s*(https?://\S+)", re.IGNORECASE | re.MULTILINE)

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
    # A help article is never a product, and www.nike.com's are called
    # /help/a/product-discount, /help/a/product-availability, /help/a/
    # product-vouchers — which the "/product" hint below happily accepts. Its
    # whole sitemap is 44 such pages, so the crawl read customer-service prose
    # and reported that the shop publishes no prices.
    "/help", "/support", "/customer-service",
)

AVAILABLE = {"instock", "in_stock", "limitedavailability", "onlineonly", "presale", "backorder"}

# Paths that list products rather than being one. These overlap with
# _NON_PRODUCT_HINTS on purpose: a /collections/ URL is not a product, but it is
# exactly where products are linked from, so the sitemap filter throws it away
# and the crawler goes looking for it.
_CATEGORY_HINTS = (
    "/collections/", "/collection/", "/category", "/categories", "/shop",
    "/c/", "/catalog", "/brand", "/marken", "/marche", "/produkte",
    "/men", "/women", "/kids", "/sale", "/new", "/footwear", "/sneaker",
    "/clothing", "/apparel", "/accessories",
)
# How much a crawl may cost one shop in one run: page fetches, and how deep the
# pagination of any single category is followed.
CRAWL_PAGE_BUDGET = 30
CRAWL_PAGINATION_DEPTH = 5
_HREF = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["\']([^"\'#\s]+)["\']""", re.IGNORECASE)


def _text(value) -> str | None:
    """schema.org fields are strings, dicts or lists depending on the vendor."""
    if value is None:
        return None
    if isinstance(value, str):
        return html.unescape(value).strip() or None
    # Article numbers and sizes are quoted by most shops and left bare by some,
    # and a bare one is not a reason to lose the field: 43einhalb publishes
    # "sku": 40272, which used to read as no SKU at all.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
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


def _read_offer(offers: dict) -> tuple[float, str, bool] | None:
    """(price, currency, in stock) off one Offer node."""
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
        return None

    availability = (_text(offers.get("availability")) or "instock").rsplit("/", 1)[-1]
    return price, currency.upper(), availability.lower().replace(" ", "") in AVAILABLE


def _priced_offer(node: dict) -> tuple[float, str, bool] | None:
    """(price, currency, in stock) off a node's Offer, or None if it has none.

    A node carrying several Offers is one product in several sizes. `_offers_of`
    splits those into variants where they can be told apart; this is what is
    left when they cannot, and it takes the middle price rather than the first.

    The first was whichever size the shop's template emitted first, and both
    ends of that list are wrong. Measured on the live database: allikestore.com
    priced a Sean Wotherspoon at EUR 47.99 off a leftover child's size while
    three other shops asked $500-$1,500, and sneakers123.com priced a Vans at
    $3,824 off an odd size at the other end. Neither stayed local — a shop's
    price is what other shops are measured against through the market index, so
    one template's ordering became everyone else's reference price.

    In stock if any of them is: a shop with one size left still has the shoe.
    """
    offers = node.get("offers")
    if isinstance(offers, dict):
        return _read_offer(offers)
    if not isinstance(offers, list):
        return None
    read = []
    for item in offers:
        if isinstance(item, dict):
            got = _read_offer(item)
            if got is not None:
                read.append(got)
    if not read:
        return None
    read.sort(key=lambda got: got[0])
    price, currency, _ = read[len(read) // 2]
    return price, currency, any(in_stock for _, _, in_stock in read)


def _offers_of(node: dict) -> tuple[list[ScrapedVariant], str] | None:
    """The sizes of a Product that lists an Offer each, or None if it lists one.

    The older way of saying what `hasVariant` says: a shop with nine sizes in
    stock writes nine Offers under one Product, each with its own sku, its own
    availability and its own price. Read as one product it loses everything the
    shelf is for — which size is left, and what that size costs.

    Only expanded when the offers can be told apart by a sku or a size. Without
    one there is nothing stable to call a variant, and a positional id would
    rename every size the moment the shop reordered its list, which reads as the
    old sizes selling out and new ones arriving.
    """
    offers = node.get("offers")
    if not isinstance(offers, list):
        return None
    priced: list[tuple[dict, tuple[float, str, bool]]] = []
    for item in offers:
        if not isinstance(item, dict):
            continue
        got = _read_offer(item)
        if got is not None:
            priced.append((item, got))
    if len(priced) < 2:
        return None
    if not all(_text(item.get("sku")) or _text(item.get("size")) for item, _ in priced):
        return None

    found: list[ScrapedVariant] = []
    currency = None
    for item, (price, item_currency, in_stock) in priced:
        currency = currency or item_currency
        sku = _text(item.get("sku"))
        size = _text(item.get("size"))
        found.append(
            ScrapedVariant(
                external_id=sku or size,
                price=price,
                compare_at=None,  # schema.org has no struck-through price
                in_stock=in_stock,
                sku=sku,
                size=size,
                color=_text(item.get("color")),
            )
        )
    return (found, currency) if currency else None


# www.ssense.com writes its picture addresses as a template the page fills in
# with script: `/images/__IMAGE_PARAMS__/…`. Taken as written, every one of its
# 25,110 products had a picture that answered 404. These are the parameters its
# own pages use.
IMAGE_TEMPLATES = {"__IMAGE_PARAMS__": "b_white,g_center,f_auto,q_auto:best"}


def resolve_image_template(image: str | None) -> str | None:
    """An image address with any known page-side template filled in."""
    if not image:
        return image
    for marker, value in IMAGE_TEMPLATES.items():
        image = image.replace(marker, value)
    return image


def _image_of(node: dict, url: str) -> str | None:
    image = node.get("image")
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    elif isinstance(image, list):
        image = next(
            (i if isinstance(i, str) else (i or {}).get("url") for i in image if i), None
        )
    if not isinstance(image, str):
        return None
    return resolve_image_template(urljoin(url, html.unescape(image).strip()))


def _variants_of(node: dict) -> tuple[list[ScrapedVariant], str] | None:
    """The sizes of a ProductGroup, or None if this is not one.

    schema.org's newer way of saying "one shoe, nine sizes": the page carries a
    ProductGroup whose `hasVariant` holds a Product per size, each with its own
    Offer and its own availability. The older shape puts a single Offer on the
    Product itself and says nothing about sizes at all.

    Worth reading properly rather than skipping. A sizeless product cannot be
    matched against anyone's size, so the whole point of the shelf — "is it in
    mine?" — is lost for that shop; and per-size availability is the difference
    between a find and a sold-out listing.
    """
    variants = node.get("hasVariant")
    if not isinstance(variants, list):
        return None
    found: list[ScrapedVariant] = []
    currency = None
    for item in variants:
        if not isinstance(item, dict):
            continue
        priced = _priced_offer(item)
        if priced is None:
            continue
        price, item_currency, in_stock = priced
        currency = currency or item_currency
        sku = _text(item.get("sku"))
        size = _text(item.get("size"))
        # Not every group varies by size. `variesBy` is colour at footlocker,
        # champssports and cruisefashion, and those variants carry `color` where
        # a sized one carries `size` — worth keeping either way, because the
        # colourway is part of what the thing is.
        colour = _text(item.get("color"))
        found.append(
            ScrapedVariant(
                external_id=sku or size or colour or f"variant{len(found)}",
                price=price,
                compare_at=None,  # schema.org has no struck-through price
                in_stock=in_stock,
                sku=sku,
                size=size,
                color=colour,
            )
        )
    if not found or not currency:
        return None
    return found, currency


def parse_product(page: str, url: str) -> tuple[ScrapedProduct, str] | None:
    """Extract the first usable Product from a page. Pure — no I/O.

    Returns (product, currency) or None when the page carries no priced product.
    """
    for node in iter_ld_products(page):
        title = _text(node.get("name"))
        if not title:
            continue
        sku = _text(node.get("sku")) or _text(node.get("mpn"))

        grouped = _variants_of(node) or _offers_of(node)
        if grouped is not None:
            variants, currency = grouped
        else:
            priced = _priced_offer(node)
            if priced is None:
                continue
            price, currency, in_stock = priced
            variants = [
                ScrapedVariant(
                    external_id=sku or "default",
                    price=price,
                    compare_at=None,
                    in_stock=in_stock,
                    sku=sku,
                    # A single-offer page can still say what size it is —
                    # highsnobiety.com puts it on the Product node — and a size
                    # we were given is not one to throw away.
                    size=_text(node.get("size")),
                    color=_text(node.get("color")),
                )
            ]

        product = ScrapedProduct(
            external_id=_text(node.get("productGroupID")) or sku or url,
            title=title,
            url=_page_url(_text(node.get("url")), url),
            brand=_text(node.get("brand")),
            image_url=_image_of(node, url),
            category=_text(node.get("category")),
            variants=variants,
        )
        return product, currency
    return None


def _page_url(stated: str | None, page: str) -> str:
    """The product's own address, resolved against the page it was read from.

    Markup may give it relative — www.toddsnyder.com and www.43einhalb.com write
    `/nl/p/…` — and 2,993 products were stored that way: a dead link on every
    surface that did not resolve it again, and a Telegram button that sank the
    whole message. Anything that is not a web address falls back to the page.
    """
    if not stated:
        return page
    resolved = urljoin(page, stated)
    return resolved if urlparse(resolved).scheme in ("http", "https") else page


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


# Campaign parameters make one page look like a dozen. eobuwie.pl links the same
# brand listing five ways with itm_source/itm_medium/itm_campaign, and every one
# of them would have cost a page from the crawl budget.
_TRACKING = ("utm_", "itm_", "gclid", "fbclid", "msclkid", "_gl")


def _strip_tracking(query: str) -> str:
    kept = [
        pair for pair in query.split("&")
        if pair and not any(pair.lower().startswith(t) for t in _TRACKING)
    ]
    return "&".join(kept)


def _internal_links(page: str, page_url: str, host: str) -> list[str]:
    """Every same-host link on a page, absolute and de-duplicated."""
    out: list[str] = []
    for raw in _HREF.findall(page):
        url = urljoin(page_url, html.unescape(raw))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc != host:
            continue
        out.append(parsed._replace(fragment="", query=_strip_tracking(parsed.query)).geturl())
    return list(dict.fromkeys(out))


def _path_of(url: str) -> str:
    """The path a hint should be matched against — never the host.

    Every hint begins with a slash, and so does the "//" in a URL's scheme, so
    matching against the whole string lets the hostname answer the question:
    https://shop.example/products/one contains "/shop" and was read as a
    category listing on that basis alone.
    """
    parsed = urlparse(url.lower())
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _is_product_url(url: str) -> bool:
    """The same judgement the sitemap walk makes, applied to a crawled link."""
    path = _path_of(url)
    if path.endswith((".xml", ".xml.gz", ".jpg", ".jpeg", ".png", ".webp", ".pdf", ".svg")):
        return False
    if any(bad in path for bad in _NON_PRODUCT_HINTS):
        return False
    return any(hint in path for hint in _PRODUCT_HINTS) or _looks_like_a_page(url)


async def crawl_product_urls(
    client: httpx.AsyncClient, base: str, limit: int = 5000, budget: int = CRAWL_PAGE_BUDGET
) -> list[str]:
    """Find products the way a person would: follow category links from the front page.

    This is the fallback for shops whose sitemap cannot be used, and it is
    deliberately bounded — `budget` page fetches per shop per run — because a
    crawl has no natural end and these are somebody else's servers.
    """
    host = urlparse(base).netloc
    front = await _get(client, base)
    if front is None:
        return []
    spent = 1

    links = _internal_links(front.text, base, host)
    products = {url for url in links if _is_product_url(url)}
    root = base.rstrip("/")
    categories = [
        url for url in links
        if url.rstrip("/") != root
        and url not in products          # /shop/asics-gel-lyte-iii is both, and is a product
        and any(hint in _path_of(url) for hint in _CATEGORY_HINTS)
    ]
    # Shallow listings first: /collections/mens carries more of the catalogue per
    # request than /collections/mens/sale/nike, and the budget is small.
    categories.sort(key=lambda u: (len(urlparse(u).path.strip("/").split("/")), len(u)))

    for category in categories:
        if spent >= budget or len(products) >= limit:
            break
        for page_number in range(1, CRAWL_PAGINATION_DEPTH + 1):
            if spent >= budget or len(products) >= limit:
                break
            url = category
            if page_number > 1:
                url = f"{category}{'&' if '?' in category else '?'}page={page_number}"
            page = await _get(client, url)
            spent += 1
            if page is None:
                break
            found = {u for u in _internal_links(page.text, url, host) if _is_product_url(u)}
            # A site that ignores ?page= serves page one again, so the absence of
            # anything new is how pagination is known to have run out.
            if not found - products:
                products |= found
                break
            products |= found

    # Order matters more here than in the sitemap walk. A crawl picks up
    # listings alongside products, and the run only reads the first `budget`
    # URLs — basket4ballers.com yielded 214 URLs of which the 38 that announce
    # themselves as products all parsed, while the categories ahead of them in
    # alphabetical order parsed as nothing at all.
    marked = sorted(u for u in products if any(h in _path_of(u) for h in _PRODUCT_HINTS))
    rest = sorted(u for u in products if u not in set(marked))
    log.debug(
        "%s: crawled %d pages, %d product URLs (%d marked)",
        base, spent, len(products), len(marked),
    )
    return (marked + rest)[:limit]


async def sitemap_urls(client: httpx.AsyncClient, base: str) -> list[str]:
    """Where this site's sitemap might be: robots.txt first, then the usual guesses."""
    found: list[str] = []
    resp = await _get(client, f"{base}/robots.txt")
    if resp is not None and len(resp.text) < 500_000:
        host = urlparse(base).netloc
        found = [
            url for url in _ROBOTS_SITEMAP.findall(resp.text)
            if urlparse(url).netloc == host
        ]
    return list(dict.fromkeys(found + [base + c for c in SITEMAP_CANDIDATES]))


async def discover_product_urls(
    client: httpx.AsyncClient, base: str, limit: int = 5000
) -> list[str]:
    """Walk the sitemaps (following one level of index) for product pages.

    Every candidate is read, not just the first that answers. Stopping at the
    first non-empty sitemap sounds thrifty and is how zalando.pl came back with
    nine URLs — its /sitemap.xml lists the shop's static pages, and the
    catalogue is in a different file entirely. A sitemap of terms-and-conditions
    pages is not a reason to stop looking for the one with the shoes in it.
    """
    host = urlparse(base).netloc
    marked: list[str] = []
    plausible: list[str] = []

    for candidate in await sitemap_urls(client, base):
        if len(marked) >= limit:
            break
        resp = await _get(client, candidate)
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

        for url in pages:
            if urlparse(url).netloc != host:
                continue
            path = _path_of(url)
            if path.endswith((".xml", ".xml.gz", ".jpg", ".png", ".webp")):
                continue
            if any(bad in path for bad in _NON_PRODUCT_HINTS):
                continue
            # A URL that announces itself as a product is taken at its word;
            # anything else with a real slug is kept as a fallback and left for
            # the JSON-LD parser to judge, because plenty of shops serve
            # products from clean paths with no marker at all.
            if any(hint in path for hint in _PRODUCT_HINTS):
                marked.append(url)
            elif _looks_like_a_page(url):
                plausible.append(url)

    found = list(dict.fromkeys(marked)) or list(dict.fromkeys(plausible))
    if found:
        return found[:limit]
    # No usable sitemap. Some shops have none at all, others publish one that
    # lists only their static pages; either way the catalogue is still there to
    # be walked.
    return await crawl_product_urls(client, base, limit)


async def has_readable_products(
    client: httpx.AsyncClient, base: str, tries: int = 3
) -> str | None:
    """Can a price be read off one of this shop's product pages?

    Returns None when yes, and otherwise the reason — which is the point.
    schema.org/Product belongs on a product page, not the homepage, so asking
    the homepage produced one blanket verdict for shops with nothing in common.

    Measured after the fix, on the 45 stores that verdict covered: two are
    readable (highsnobiety.com, soletrader.co.uk) and the rest publish no
    machine-readable price at all. Their pages carry a name and an article
    number and render the price in JavaScript, which is a wall rather than a
    gap in this parser — so the reason has to say which of the two it is.
    """
    # Ask for far more URLs than will be opened. The ordering — pages that
    # announce themselves as products first — only helps if it has something to
    # order: asking for three got www.consortium.co.uk's first three category
    # pages, and none of them is a product.
    urls = await discover_product_urls(client, base, limit=max(30, tries * 10))
    if not urls:
        return "no product pages found in the sitemap or by crawling"
    opened = 0
    for url in urls[:tries]:
        page = await _get(client, url)
        if page is None:
            continue
        opened += 1
        if parse_product(page.text, url) is not None:
            return None
    if not opened:
        return "product pages found but none of them would load"
    return "product pages carry no machine-readable price (rendered in JavaScript)"


async def fetch_product(
    client: httpx.AsyncClient, url: str
) -> tuple[str, ScrapedProduct | None]:
    """One product page. Returns ("ok" | "gone" | "unreachable", product).

    These shops are read as a bounded crawl, so their catalogues are never
    enumerated and absence from a pass proves nothing. Opening the page itself
    is the only way to ask, and 731 offers across 17 shops is few enough to ask
    about one at a time.

    A page that will not parse is "unreachable", not "gone". Markup we cannot
    read is our limitation; only the shop's own 404 is the shop's answer.
    """
    try:
        resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.debug("%s: %s", url, exc)
        return "unreachable", None
    if resp.status_code in (404, 410):
        return "gone", None
    if resp.status_code != 200:
        return "unreachable", None
    parsed = parse_product(resp.text, url)
    if parsed is None:
        return "unreachable", None
    product, found_currency = parsed
    product.currency = found_currency
    return "ok", product


async def fetch(
    client: httpx.AsyncClient,
    domain: str,
    currency: str | None = None,
    budget: int = DEFAULT_BUDGET,
    cursor: int = 0,
) -> FetchResult:
    """Crawl `budget` product pages starting at `cursor`.

    Where to carry on is reported in `result.next_cursor`, the same way the
    Shopify adapter reports it — one convention, because the pipeline should not
    have to know which adapter it is talking to.
    """
    base = f"https://{domain}".rstrip("/")
    urls = await discover_product_urls(client, base)
    if not urls:
        return FetchResult(
            domain=domain, error=NO_PRODUCT_URLS
        )

    start = cursor % len(urls)
    # Never more than the catalogue: the window wraps around the end of the
    # list, and with a budget larger than the shop every page was read twice
    # in the same pass — twice the requests to a small shop for nothing.
    window = (urls + urls)[start : start + min(budget, len(urls))]
    next_cursor = (start + len(window)) % len(urls)

    semaphore = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    products: list[ScrapedProduct] = []
    currencies: dict[str, int] = {}
    # How the pages answered, so a shop that stopped letting us in is not
    # reported as a shop whose pages carry no prices: www.kickz.com said "no
    # schema.org/Product markup" for three weeks, which is a different problem
    # with a different fix.
    answered: Counter[str] = Counter()

    async def one(url: str) -> None:
        async with semaphore:
            try:
                resp = await client.get(url, follow_redirects=True)
            except httpx.HTTPError as exc:
                answered[type(exc).__name__] += 1
                return
        answered[f"HTTP {resp.status_code}"] += 1
        if resp.status_code != 200:
            return
        parsed = parse_product(resp.text, url)
        if parsed is None:
            return
        product, found_currency = parsed
        # What this page said, kept on the product. `dominant` below is only a
        # summary for the store row; it must not be what the prices are read in.
        product.currency = found_currency
        products.append(product)
        currencies[found_currency] = currencies.get(found_currency, 0) + 1

    await asyncio.gather(*(one(u) for u in window))

    if not products:
        if not answered.get("HTTP 200"):
            how = ", ".join(f"{what} ×{n}" for what, n in answered.most_common(3))
            return FetchResult(
                domain=domain, error=f"{PAGES_WOULD_NOT_LOAD} ({how})",
                next_cursor=next_cursor,
            )
        return FetchResult(
            domain=domain, error=NO_MARKUP, next_cursor=next_cursor
        )

    dominant = currency or max(currencies, key=lambda code: currencies[code])
    return FetchResult(
        domain=domain, products=products, currency=dominant, next_cursor=next_cursor
    )
