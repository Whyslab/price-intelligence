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

Bound to localhost by default. Reading needs no login and never has: the worst
a stranger on this machine could learn from it is what is on sale.

Writing is another matter, and the page writes now — a star against a product is
a row in somebody's name. Every write is signed by Telegram (pi.webauth), with
no exemption for localhost, because an exemption is invisible from outside and
`--host 0.0.0.0` is a flag that exists. Outside Telegram the only way to be
somebody is `pi web --owner <id>`, typed by the person it names.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as dbm
from . import webauth

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("shelf.html")
PAGE_SIZE = 60
MAX_PAGE_SIZE = 200
# How many shops one article lookup returns. Enough that a popular sneaker
# shows its whole spread — CW2288-111 is stocked by 26 merchants — without a
# name search dumping a page of unrelated things.
LOOKUP_LIMIT = 40

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
        # Not a filter on the shelf but a different list entirely — see
        # db.favorites_for. Read here so a link to it can be sent to somebody.
        "favorites": (query.get("favorites", ["0"])[0] or "0").lower() in ("1", "true", "yes"),
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


def lookup_json(row: sqlite3.Row) -> dict:
    """One shop's live price for a looked-up article."""
    return {
        "shop": row["store_name"] or row["domain"],
        "domain": row["domain"],
        "country": row["country"],
        "url": row["url"],
        "title": row["title"],
        "brand": row["brand_norm"],
        "price": round(row["price_usd"], 2),
        "currency": row["currency"],
        "price_native": row["price_native"],
        "discount_pct": row["discount_pct"],
        "checked_at": row["last_ok"],
        "product_id": row["product_id"],
    }


def lookup_page(conn: sqlite3.Connection, query: str) -> dict:
    """What every shop charges for what was typed.

    `same_thing` is the part that must survive the trip to the browser: an
    article number gives one product priced by several merchants, a name gives
    several different products, and a page that drew both the same way would
    call the cheapest of 242 unrelated shoes a saving.
    """
    found = dbm.lookup_article(conn, query, limit=LOOKUP_LIMIT)
    return {
        "query": found["query"],
        "matched_by": found["matched_by"],
        "key": found["key"],
        "same_thing": found["same_thing"],
        "found": found["found"],
        "too_common": found["too_common"],
        "shops": [lookup_json(row) for row in found["shops"]],
    }


def favorite_json(item: dict) -> dict:
    """One followed product, in the shape the page's cards already read.

    `price` may be None and `discount` usually is. A followed product is
    normally at its ordinary price — waiting for it to stop being is the point —
    so the card has to be able to say "still $180" and "sold out" as plainly as
    it says "−40%".
    """
    return {
        "id": item["product_id"],
        "variant": item["variant_id"],
        "title": item["title"],
        "url": item["url"],
        "image": item["image_url"],
        "brand": item["brand"],
        "shop": item["store_name"] or item["domain"],
        "domain": item["domain"],
        "country": item["country"],
        "price": item["price_usd"],
        # What it cost when it was starred, which is the comparison this list is
        # for. The shelf compares against the market; this compares against the
        # moment somebody said they wanted it.
        "since": item["since_usd"],
        "discount": round(item["discount_pct"]) if item["discount_pct"] else None,
        "added_at": item["added_at"],
        "checked_at": item["checked_at"],
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


# What a read answers when the reader is not paying. 402 rather than 403: the
# request is understood and well formed, and the only thing missing is payment.
SUBSCRIPTION_REQUIRED = {
    "error": "subscription required",
    "detail": "Витрина открывается по подписке. Оформить её можно в боте.",
}


def locked_page(conn: sqlite3.Connection) -> bytes:
    """What somebody without a subscription gets instead of the shelf.

    A page rather than a status code. Whoever lands here followed a button out
    of the bot, and the two things they need are what this is and how to open
    it; an error tells them neither and reads as a broken link.

    It carries the same three arguments the bot's pitch does, with the numbers
    counted here and now for the same reason: the size of the catalogue is the
    one claim a reader can check in the next thirty seconds.
    """
    shelf = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    compared = conn.execute(
        "SELECT COUNT(*) FROM offers WHERE reference_source = 'market'"
    ).fetchone()[0]
    body = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Полка — нужна подписка</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0 auto; padding: 2rem 1.25rem; max-width: 34rem;
         font: 16px/1.55 system-ui, sans-serif; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  p.lead {{ opacity: .75; margin-top: 0; }}
  ol {{ padding-left: 1.2rem; }}
  li {{ margin-bottom: .9rem; }}
  .price {{ margin-top: 1.75rem; padding: 1rem; border-radius: .75rem;
            background: rgba(128, 128, 128, .14); text-align: center; }}
  button {{ margin-top: 1rem; width: 100%; padding: .8rem; font: inherit;
            font-weight: 600; border: 0; border-radius: .6rem;
            background: #2481cc; color: #fff; cursor: pointer; }}
</style></head><body>
<h1>💎 Полка открывается по подписке</h1>
<p class="lead">Здесь {_spaced(shelf)} предложений со скидкой, с поиском по
артикулу, размеру и марке.</p>
<ol>
  <li><b>Дешевле, чем у соседей.</b> {_spaced(compared)} предложений сравнены
      с ценой на ту же вещь в других магазинах по артикулу производителя.
      Канал со скидками пересылает ярлык — сравнить ему не с чем.</li>
  <li><b>Цена подтверждена, а не найдена когда-то.</b> На каждой карточке
      написано, когда магазин в последний раз показал эту цену.</li>
  <li><b>Цена на руках</b> — с доставкой и пошлиной в вашу страну, а не только
      та, что на ярлыке.</li>
</ol>
<div class="price">⭐ <b>150 звёзд в месяц</b><br>или 1500 за год — два месяца в подарок</div>
<button onclick="if (window.Telegram?.WebApp) Telegram.WebApp.close(); else history.back();">
  Вернуться в бота и оформить
</button>
</body></html>
"""
    return body.encode("utf-8")


def _spaced(n: int) -> str:
    """Thousands separated the way Russian writes them."""
    return f"{n:,}".replace(",", "\u00a0")


def render_page(
    conn: sqlite3.Connection, args: dict, user_id: int | None = None
) -> bytes:
    """The page with its first screenful already in it.

    Sending an empty shell and letting it ask twice puts two round trips between
    opening the link and seeing anything, which on a phone is the whole
    impression the page makes. The markup is unchanged; only the seed differs,
    and a page served without one behaves identically.

    The reader's name goes in it whenever the server already knows it, which is
    the `--owner` case: their hearts are then drawn in the first paint instead of
    appearing a moment later. Inside Telegram it cannot be known here — the
    signature travels in the URL fragment, which browsers do not send — so the
    page asks for it and the seed says nobody.
    """
    seed = json.dumps(
        {"seed": {
            "facets": dbm.shelf_facets(conn, kids=args["kids"]),
            "offers": shelf_page(conn, args),
            "me": user_id,
            "favorites": sorted(dbm.favorite_ids(conn, user_id)) if user_id else [],
            # Only when the link asked for that list, so an ordinary page does
            # not pay for a list nobody opened.
            "favorites_items": [
                favorite_json(item) for item in dbm.favorites_for(conn, user_id)
            ] if user_id and args["favorites"] else None,
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


# The largest body a write is allowed to send. A favourite is two integers;
# anything larger is either a mistake or somebody probing.
MAX_BODY = 4096


class Handler(BaseHTTPRequestHandler):
    """One request. A connection per request, because the server is threaded."""

    db_path: Path = Path("data/pi.db")
    bot_token: str | None = None
    # Whose shelf this is when there is no Telegram to ask. None means nobody:
    # the page then hides its hearts entirely rather than offering a button that
    # answers 401.
    owner_id: int | None = None
    # Who never has to pay: the person who runs the collector. Unlike owner_id
    # this grants no identity — the reader still proves who they are with
    # Telegram's signature — so it is safe on an address other people reach.
    exempt_id: int | None = None
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

    def _open_rw(self) -> sqlite3.Connection:
        """A writable connection, for the one thing this server writes.

        Separate from `_open` so that reading stays incapable of writing, and
        with a busy timeout because the other writer is a sweep that runs for
        minutes. WAL means a reader never blocks it; a second writer waits, and
        five seconds is far longer than the single INSERT here can need.
        """
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _reader(self) -> int | None:
        """Whose request this is: Telegram's signature, or the owner flag.

        In that order, and with no third answer. A page inside Telegram sends
        its initData with every call; a person debugging on their own machine
        passes --owner and is that person. Anyone else is nobody, and nobody
        cannot write.
        """
        signed = webauth.verify(
            self.headers.get("X-Telegram-Init-Data", ""), self.bot_token
        )
        if signed is not None:
            return signed
        # The owner flag is an identity nobody proved, so it may only be
        # believed when this process is talking to the person directly. Applied
        # here rather than at each call site: every verb resolves identity
        # through this method, and a guard that has to be remembered three times
        # gets remembered twice — un-starring was the one that was missed, and
        # through a tunnel it let the internet empty the owner's list one id at
        # a time, learning which ids were on it from the answer.
        return self.owner_id if self._direct() else None

    def _paying(self, conn: sqlite3.Connection) -> bool:
        """Whether this request may see the shelf at all.

        The shelf is the thing being sold, so unlike the hearts — which a
        signed-out reader simply does not get — this is the gate. Two ways
        through it: a live subscription, or being the person who runs the
        collector. The second is not a courtesy; a shop owner locked out of
        their own shop by their own paywall cannot debug it.

        `is_subscribed` and not `subscription_state`, deliberately: the grace
        period keeps a feed alive through a failed renewal, and handing back the
        thing being sold as well would make grace a free month.
        """
        reader = self._reader()
        if reader is None:
            return False
        # `exempt_id` is safe anywhere, because Telegram had to sign the request
        # naming that person for `_reader` to have returned it at all.
        if self.exempt_id is not None and reader == self.exempt_id:
            return True
        # `owner_id` is deliberately not a second way through. It says "whoever
        # reaches me is that person", which was true while the only way to reach
        # this process was to be sitting at it, and stopped being true the day a
        # tunnel pointed at 127.0.0.1. `_direct` narrows that, but it is a
        # blocklist and blocklists fail open: an ssh -L forward, a socat, an
        # nginx without proxy_set_header — none of them announce themselves, and
        # each would hand the whole shelf away.
        #
        # So the inference is gone rather than qualified. The flag still names a
        # reader, which is all it was ever for; an operator who wants the shelf
        # opens the mini-app, or gives themselves access with `pi grant`.
        return dbm.is_subscribed(conn, reader)

    # Headers a reverse proxy adds and a browser talking to us directly does
    # not. cloudflared sends the first two. Not a security boundary on its own —
    # see `_paying` — but enough to keep an unproven identity off a tunnel.
    _PROXY_HEADERS = ("X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP", "Forwarded")

    def _direct(self) -> bool:
        """Whether this request reached us without passing through anything."""
        if any(self.headers.get(name) for name in self._PROXY_HEADERS):
            return False
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _body(self) -> dict:
        """The JSON a write sent, or {}."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _product_id(self, path: str) -> int | None:
        try:
            return int(path.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                with self._open() as conn:
                    if not self._paying(conn):
                        # A page, not a 402. Somebody who followed a link here
                        # from the bot should find out what this is and how to
                        # open it, and an error code tells them neither.
                        self._send(
                            200, locked_page(conn), "text/html; charset=utf-8"
                        )
                        return
                    body = render_page(conn, read_query(parsed.query), self._reader())
                self._send(200, body, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/facets":
                with self._open() as conn:
                    if not self._paying(conn):
                        self._json(SUBSCRIPTION_REQUIRED, 402)
                        return
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
                    if not self._paying(conn):
                        self._json(SUBSCRIPTION_REQUIRED, 402)
                        return
                    found = product_page(
                        conn, product_id, read_variant(parsed.query)
                    )
                self._json(found or {"error": "not on the shelf"}, 200 if found else 404)
                return
            if parsed.path == "/api/lookup":
                wanted = (parse_qs(parsed.query).get("q", [""])[0] or "").strip()
                with self._open() as conn:
                    if not self._paying(conn):
                        self._json(SUBSCRIPTION_REQUIRED, 402)
                        return
                    self._json(lookup_page(conn, wanted))
                return
            if parsed.path == "/api/offers":
                with self._open() as conn:
                    if not self._paying(conn):
                        self._json(SUBSCRIPTION_REQUIRED, 402)
                        return
                    self._json(shelf_page(conn, read_query(parsed.query)))
                return
            if parsed.path == "/api/favorites":
                # 401 is the page's signal to hide its hearts, not an error to
                # show anybody: a shelf opened in a plain browser is still a
                # perfectly good shelf, it just cannot star anything.
                user_id = self._reader()
                if user_id is None:
                    self._json({"error": "not signed in"}, 401)
                    return
                with self._open() as conn:
                    # Gated like the shelf, because it *is* the shelf. A starred
                    # row carries the title, the shop, the live price and the
                    # discount — the same fields the card draws. Left on the
                    # identity check alone, a reader who never paid could star
                    # product ids in a loop and read the whole priced catalogue
                    # back through here, past four 402s.
                    if not self._paying(conn):
                        self._json(SUBSCRIPTION_REQUIRED, 402)
                        return
                    items = dbm.favorites_for(conn, user_id)
                self._json({"user": user_id, "items": [favorite_json(i) for i in items]})
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # a browsable page must not take the process down
            log.exception("%s failed", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/favorites":
                self._json({"error": "not found"}, 404)
                return
            user_id = self._reader()
            if user_id is None:
                self._json({"error": "not signed in"}, 401)
                return
            payload = self._body()
            try:
                product_id = int(payload["product_id"])
            except (KeyError, TypeError, ValueError):
                self._json({"error": "product_id required"}, 400)
                return
            try:
                variant_id = int(payload["variant_id"])
            except (KeyError, TypeError, ValueError):
                variant_id = None
            with self._open_rw() as conn:
                # Starring is a subscriber feature: `pipeline` only sends
                # followed-price alerts to `subscribers()`, so a free reader
                # gets nothing from a star except a row they could read a price
                # out of. Removing one stays open — see do_DELETE.
                if not self._paying(conn):
                    self._json(SUBSCRIPTION_REQUIRED, 402)
                    return
                exists = conn.execute(
                    "SELECT 1 FROM products WHERE id = ?", (product_id,)
                ).fetchone()
                if exists is None:
                    self._json({"error": "no such product"}, 404)
                    return
                added = dbm.add_favorite(conn, user_id, product_id, variant_id)
                conn.commit()
            # 200 rather than 201 for one already there: starring twice is the
            # same wish stated twice, and the page should not have to care.
            self._json({"product_id": product_id, "added": added}, 201 if added else 200)
            return
        except Exception as exc:
            log.exception("%s failed", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not parsed.path.startswith("/api/favorites/"):
                self._json({"error": "not found"}, 404)
                return
            user_id = self._reader()
            if user_id is None:
                self._json({"error": "not signed in"}, 401)
                return
            product_id = self._product_id(parsed.path)
            if product_id is None:
                self._json({"error": "not a product id"}, 400)
                return
            # Deliberately not behind the paywall. Somebody whose subscription
            # lapsed must still be able to take their own things off their own
            # list, and un-starring reveals nothing: it reads no product row and
            # answers with the id the caller already sent.
            with self._open_rw() as conn:
                removed = dbm.remove_favorite(conn, user_id, product_id)
                conn.commit()
            self._json({"product_id": product_id, "removed": removed})
            return
        except Exception as exc:
            log.exception("%s failed", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    bot_token: str | None = None,
    owner_id: int | None = None,
    exempt_id: int | None = None,
) -> None:
    """Run until interrupted.

    `owner_id` says who an unsigned request is, and only makes sense on
    localhost. `exempt_id` says who never has to pay, and is safe anywhere
    because it grants nothing on its own — the reader still has to prove they
    are that person with Telegram's signature.
    """
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "db_path": Path(db_path), "bot_token": bot_token,
            "owner_id": owner_id, "exempt_id": exempt_id,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    log.info("shelf on http://%s:%d — Ctrl-C to stop", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
