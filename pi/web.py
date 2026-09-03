"""A browsable shelf, because notifications are not a catalogue.

Telegram is right for news and wrong for browsing. It has no filters, no way
back to yesterday, and it only ever shows what was new enough to be worth
interrupting somebody about — which on this database is 665 of the 20,934
discounts standing right now. The rest were suppressed by `pi seed` as "already
running when the bot arrived", which is true and is also the reason nobody has
ever seen them.

So the same shelf is served as a page: everything on offer, filtered by the
things a person actually decides on — their size, what kind of thing it is, who
it is for — and ordered by how good the discount is rather than by when we
happened to notice it.

Deliberately small. The stdlib's HTTP server is enough for a shelf that is read
far more often than it changes, and adding a framework for one page and two
JSON endpoints would be a dependency to maintain for no answer this cannot give.

Bound to localhost by default. There is no login: anything reachable from
outside this machine has to get its authentication first, and pretending
otherwise by binding to 0.0.0.0 with a comment about it would be worse than
requiring the flag.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as dbm

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("shelf.html")
PAGE_SIZE = 60
MAX_PAGE_SIZE = 200

# Which orderings the page may ask for, and what each means in SQL. A map
# rather than a string from the query: the value lands in an ORDER BY.
# The default takes each shop's best find before anyone's second best. Without
# it one shop owns the whole first screen: www.freshmansarchive.com lists the
# same vintage blazer in eight sizes at −90%, all scored alike, and a page of
# eight identical blazers is a worse answer than eight different shops even
# when every one of the claims is true. The other sorts stay literal — "по
# скидке" is asked precisely when the deepest cut is the whole question.
BY_SHOP_THEN_SCORE = (
    "ROW_NUMBER() OVER (PARTITION BY p.store_id ORDER BY o.score DESC, o.discount_pct DESC), "
    "o.score DESC, o.discount_pct DESC"
)
SORTS = {
    "score": BY_SHOP_THEN_SCORE,
    "discount": "o.discount_pct DESC, o.score DESC",
    "saving": "o.saving_usd DESC",
    "cheapest": "o.price_usd ASC",
    "newest": "o.found_at DESC",
    "freshest": "o.checked_at DESC",
}
DEFAULT_SORT = "score"


def _list(query: dict, name: str) -> list[str]:
    """One repeated or comma-separated parameter, as a clean list."""
    out: list[str] = []
    for raw in query.get(name, []):
        out += [part.strip() for part in raw.split(",") if part.strip()]
    return out


def _float(query: dict, name: str) -> float | None:
    """An optional number. An unreadable one is no opinion, not zero."""
    raw = (query.get(name, [""])[0] or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def read_variant(raw: str) -> int | None:
    """Which size a product link was opened from. Absent is not zero."""
    try:
        return int((parse_qs(raw).get("variant", [""])[0] or "").strip())
    except ValueError:
        return None


def _int(query: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def read_query(raw: str) -> dict:
    """Turn a query string into the arguments the shelf query takes.

    Split out from the request handling so it can be tested without a socket,
    and because everything that can go wrong with a URL goes wrong here.
    """
    query = parse_qs(raw)
    sort = (query.get("sort", [DEFAULT_SORT])[0] or DEFAULT_SORT).lower()
    return {
        "genders": _list(query, "gender"),
        "kinds": _list(query, "kind"),
        "sizes": [s.upper() for s in _list(query, "size")],
        "brands": _list(query, "brand"),
        "search": (query.get("q", [""])[0] or "").strip(),
        "min_price": _float(query, "min_price"),
        "max_price": _float(query, "max_price"),
        "min_discount": _float(query, "min_discount"),
        "sort": sort if sort in SORTS else DEFAULT_SORT,
        # The one filter that is on unless switched off. Children's clothing is
        # 447 of the 33,738 offers standing right now and none of them is what
        # this shelf is for — but it stays reachable, because a hidden
        # misclassification is one nobody can report.
        "kids": (query.get("kids", ["0"])[0] or "0").lower() in ("1", "true", "yes"),
        "limit": _int(query, "limit", PAGE_SIZE, 1, MAX_PAGE_SIZE),
        "page": _int(query, "page", 0, 0, 10_000),
    }


def offer_json(row: sqlite3.Row) -> dict:
    """One card's worth of an offer.

    `checked_at` is on every card on purpose. It is the difference between a
    shelf and a graveyard, and it is the one thing a page like this normally
    hides: a listing that four days ago was 60% off may simply be gone.
    """
    return {
        "id": row["product_id"],
        # Which size this card is, so opening it lands on the same row rather
        # than on whichever one the database happened to return first.
        "variant": row["variant_id"],
        "title": row["title"],
        "url": row["url"],
        "image": row["image_url"],
        "brand": row["brand_family"] or row["brand"],
        "shop": row["store_name"] or row["domain"],
        "domain": row["domain"],
        "country": row["country"],
        "price": round(row["price_usd"], 2),
        "was": round(row["reference_usd"], 2),
        "source": row["reference_source"],
        "discount": round(row["discount_pct"]),
        "saving": round(row["saving_usd"], 2),
        "score": row["score"],
        "size": row["size_norm"] or row["size"],
        "kind": row["kind"],
        "gender": row["gender"],
        "all_time_low": bool(row["all_time_low"]),
        "found_at": row["found_at"],
        "checked_at": row["checked_at"],
    }


def product_page(
    conn: sqlite3.Connection, product_id: int, variant_id: int | None = None
) -> dict:
    """One product, and what everyone else charges for the same article.

    The comparison is the point. A shop's own struck-through price is a claim;
    another shop asking twice as much for the same article is evidence, and a
    third asking less is the answer to the only question that matters. 82% of
    the shelf has nobody to compare against and says so rather than implying it.

    `offers` is keyed by variant, so a product on sale in several sizes has
    several rows: 322 of the 23,934 products on the shelf, and 188 of those at
    prices that differ between sizes — one of them $90 in one size and $180 in
    another. Taking whichever row came back first meant a card showing one price
    could open onto a different one. So the variant the card was built from is
    passed back and wins; without one the best-scoring row does, which is at
    least the same row every time.
    """
    row = conn.execute(
        """
        SELECT o.*, v.size_norm, v.size, v.sku,
               p.title, p.url, p.image_url, p.brand, p.brand_norm, p.brand_family,
               p.gender, p.kind, s.domain, s.name AS store_name, s.country, s.currency
          FROM offers o
          JOIN variants v ON v.id = o.variant_id
          JOIN products p ON p.id = o.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE o.product_id = ?
         ORDER BY (o.variant_id = ?) DESC, o.score DESC, o.price_usd ASC
         LIMIT 1
        """,
        (product_id, -1 if variant_id is None else variant_id),
    ).fetchone()
    if row is None:
        return {}

    elsewhere = [
        {
            "shop": other["store_name"] or other["domain"],
            "domain": other["domain"],
            "country": other["country"],
            "url": other["url"],
            "title": other["title"],
            "price": round(other["price_usd"], 2),
            "checked_at": other["last_ok"],
        }
        for other in dbm.same_article(conn, product_id)
    ]
    cheaper = [o for o in elsewhere if o["price"] < row["price_usd"]]
    return {
        "ours": offer_json(row),
        "sizes": [
            {"size": size, "in_stock": bool(in_stock)}
            for size, in_stock in dbm.sizes_in_stock(conn, product_id)
        ],
        "elsewhere": elsewhere,
        # Said plainly, because a page that only ever flatters the offer it is
        # showing is an advertisement. Sometimes the answer is "not here".
        "cheapest_elsewhere": cheaper[0] if cheaper else None,
    }


def shelf_page(conn: sqlite3.Connection, args: dict) -> dict:
    """One page of the shelf, with enough around it to render the controls."""
    rows, total = dbm.offers_for(
        conn,
        genders=args["genders"] or None,
        kinds=args["kinds"] or None,
        sizes=args["sizes"] or None,
        brands=args["brands"] or None,
        limit=args["limit"],
        offset=args["page"] * args["limit"],
        order_by=SORTS[args["sort"]],
        search=args["search"] or None,
        min_price=args["min_price"],
        max_price=args["max_price"],
        min_discount=args["min_discount"],
        kids=args["kids"],
    )
    return {
        "total": total,
        "page": args["page"],
        "pages": max(1, -(-total // args["limit"])),
        "offers": [offer_json(row) for row in rows],
    }


def render_page(conn: sqlite3.Connection, args: dict) -> bytes:
    """The page with its first screenful already in it.

    Sending an empty shell and letting it ask twice puts two round trips between
    opening the link and seeing anything, which on a phone is the whole
    impression the page makes. The markup is unchanged; only the seed differs,
    and a page served without one behaves identically.
    """
    seed = json.dumps(
        {"seed": {
            "facets": dbm.shelf_facets(conn, kids=args["kids"]),
            "offers": shelf_page(conn, args),
        }},
        ensure_ascii=False,
    )
    # A JSON string may contain "</script>"; inside a script element that ends
    # it. Escaping the slash is invisible to JSON.parse and not to the parser.
    seed = seed.replace("</", "<\\/")
    return (
        PAGE.read_text(encoding="utf-8")
        .replace('{"seed": null}', seed, 1)
        .encode("utf-8")
    )


class Handler(BaseHTTPRequestHandler):
    """One request. A connection per request, because the server is threaded."""

    db_path: Path = Path("data/pi.db")
    server_version = "price-intelligence"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is the only thing allowed to script this origin, and it
        # carries no third-party anything.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline' data: https:")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _open(self) -> sqlite3.Connection:
        # Read-only: this process must never be the reason a sweep cannot write.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                with self._open() as conn:
                    body = render_page(conn, read_query(parsed.query))
                self._send(200, body, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/facets":
                with self._open() as conn:
                    self._json(
                        dbm.shelf_facets(conn, kids=read_query(parsed.query)["kids"])
                    )
                return
            if parsed.path.startswith("/api/product/"):
                try:
                    product_id = int(parsed.path.rsplit("/", 1)[1])
                except ValueError:
                    self._json({"error": "not a product id"}, 400)
                    return
                with self._open() as conn:
                    found = product_page(
                        conn, product_id, read_variant(parsed.query)
                    )
                self._json(found or {"error": "not on the shelf"}, 200 if found else 404)
                return
            if parsed.path == "/api/offers":
                with self._open() as conn:
                    self._json(shelf_page(conn, read_query(parsed.query)))
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # a browsable page must not take the process down
            log.exception("%s failed", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    do_HEAD = do_GET


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run until interrupted."""
    handler = type("BoundHandler", (Handler,), {"db_path": Path(db_path)})
    server = ThreadingHTTPServer((host, port), handler)
    log.info("shelf on http://%s:%d — Ctrl-C to stop", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
