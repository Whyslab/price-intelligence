"""Build a small synthetic database so the project can be tried without a crawl.

Nothing here comes from a real shop. The stores are `*.example`, the brands are
made up, and the prices are drawn from a seeded random generator, so the same
seed gives the same file every time.

What it does keep from the real thing is the *behaviours* the project exists to
tell apart. Each store is given one way of writing prices:

* ``honest``  — shows a struck-through price only while a sale lasts, and the
  "was" price is one the shop really charged before. Discounts are irregular
  (37%, 22%, 41%) because they come from real prices.
* ``promo``   — runs "-30% / -40% on everything" sales, so the percentages are
  round, but the "was" price is the shop's real regular price. A round
  discount is not evidence of anything by itself.
* ``rule``    — applies "-40% on everything" and *derives* the "was" price from
  the sale price. Every discount is a round step and the "was" price never
  appeared as a real price.
* ``inflated`` — keeps a permanent struck-through price about 1.5x the real one.
* ``plain``   — never shows a struck-through price at all.

Usage::

    python scripts/make_demo_db.py data/demo.db
    PI_DB_PATH=data/demo.db python -m pi web
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pi import db as dbm  # noqa: E402
from pi import reference, taxonomy  # noqa: E402

DAYS = 35
PRODUCTS_PER_STORE = 70
SIZES = ["US7", "US8", "US9", "US10", "US11"]

# (domain, behaviour, currency, fx to USD)
STORES = [
    ("alpha-kicks.example", "honest", "USD", 1.0),
    ("brightsole.example", "honest", "EUR", 1.08),
    ("corner-court.example", "honest", "GBP", 1.27),
    ("deadstock-depot.example", "promo", "USD", 1.0),
    ("everyday-shoes.example", "rule", "USD", 1.0),
    ("fastlane-outlet.example", "rule", "EUR", 1.08),
    ("grandslam-store.example", "inflated", "USD", 1.0),
    ("highline-sport.example", "inflated", "EUR", 1.08),
    ("inkwell-wear.example", "plain", "USD", 1.0),
    ("juniper-street.example", "plain", "GBP", 1.27),
]

BRANDS = ["Aeroleap", "Northfield", "Kestrel", "Marlow & Co", "Vantage"]
MODELS = [
    ("Runner", "sneakers", 90, 160),
    ("Court Low", "sneakers", 70, 130),
    ("Trail Mid", "boots", 110, 190),
    ("Heritage Hoodie", "hoodie", 60, 120),
    ("Classic Tee", "t-shirt", 25, 45),
]


def _cents(x: float) -> float:
    return round(x, 2)


def _usd(native: float, fx: float) -> float:
    return _cents(native * fx)


def build(path: Path, seed: int = 7) -> None:
    rng = random.Random(seed)
    if path.exists():
        path.unlink()
    conn = dbm.connect(path)
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS)

    # A shared catalogue, so some articles turn up in several stores: that is
    # what the cross-store comparison needs in order to have anything to say.
    catalogue = []
    for n in range(PRODUCTS_PER_STORE * 2):
        brand = rng.choice(BRANDS)
        model, kind, lo, hi = rng.choice(MODELS)
        catalogue.append(
            {
                "brand": brand,
                "title": f"{brand} {model} {n:03d}",
                "kind": kind,
                "article": f"DM{1000 + n}-{rng.randint(100, 999)}",
                "base": rng.uniform(lo, hi),
            }
        )

    with dbm.transaction(conn):
        for domain, behaviour, currency, fx in STORES:
            store_id = dbm.upsert_store(
                conn,
                domain,
                name=domain.split(".")[0].replace("-", " ").title(),
                platform="shopify",
                currency=currency,
                country={"USD": "US", "EUR": "DE", "GBP": "GB"}[currency],
                status="ok",
                last_ok=end.isoformat(timespec="seconds"),
                last_checked=end.isoformat(timespec="seconds"),
            )
            chosen = rng.sample(catalogue, PRODUCTS_PER_STORE)
            for item in chosen:
                _fill_product(conn, rng, store_id, domain, behaviour, currency, fx, item, start)

        for pid, brand, title, skus in conn.execute(
            """SELECT p.id, p.brand, p.title, group_concat(v.sku, char(10))
               FROM products p LEFT JOIN variants v ON v.product_id = p.id GROUP BY p.id"""
        ).fetchall():
            dbm.set_product_keys(
                conn, pid, reference.keys_for(brand, title, (skus or "").split("\n"))
            )
    taxonomy.classify(conn)
    conn.close()


def _fill_product(conn, rng, store_id, domain, behaviour, currency, fx, item, start):
    # Shops price the same article a little differently.
    regular = item["base"] * rng.uniform(0.92, 1.12)
    regular_native = _cents(regular / fx)
    external = f"{domain}-{item['article']}"
    pid = dbm.upsert_product(
        conn,
        store_id,
        external,
        item["title"],
        f"https://{domain}/products/{item['article'].lower()}",
        brand=item["brand"],
        image_url=_placeholder(item),
        category=item["kind"],
    )
    conn.execute("UPDATE products SET last_seen = ? WHERE id = ?",
                 (datetime.now(UTC).isoformat(timespec="seconds"), pid))
    sale = _sale_plan(rng, behaviour)
    for size in SIZES:
        vid = dbm.upsert_variant(
            conn, pid, f"{external}-{size}", sku=item["article"], size=size, size_norm=size
        )
        for day in range(DAYS + 1):
            ts = start + timedelta(days=day, hours=rng.randint(0, 5))
            price_native, compare_native = _price_on(behaviour, regular_native, sale, day)
            in_stock = ((vid * 31 + day * 7) % 11) != 0
            dbm.record_price(
                conn,
                vid,
                _usd(price_native, fx),
                _usd(compare_native, fx) if compare_native else None,
                in_stock,
                currency,
                price_native,
                fx,
                ts=ts.isoformat(timespec="seconds"),
                compare_at_native=compare_native,
            )


def _placeholder(item) -> str:
    """A flat-colour SVG standing in for a product photo (no real images here)."""
    hue = sum(map(ord, item["article"])) * 37 % 360
    letter = item["brand"][0]
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='400'>"
        f"<rect width='400' height='400' fill='hsl({hue},35%,86%)'/>"
        f"<text x='200' y='235' font-size='140' font-family='sans-serif' text-anchor='middle' "
        f"fill='hsl({hue},35%,40%)'>{letter}</text></svg>"
    )
    return "data:image/svg+xml;utf8," + svg.replace("#", "%23")


def _sale_plan(rng, behaviour):
    """Which days a product is on sale and at what percentage."""
    if behaviour == "plain" and rng.random() < 0.7:
        return None
    first = rng.randint(3, 28)
    length = rng.randint(6, 16)
    if behaviour == "honest":
        pct = rng.choice([0.17, 0.22, 0.28, 0.33, 0.37, 0.41, 0.46])
        pct += rng.uniform(-0.012, 0.012)  # real prices make irregular percentages
    elif behaviour in ("rule", "inflated", "promo"):
        pct = rng.choice([0.2, 0.3, 0.4, 0.5])
    else:
        pct = rng.choice([0.15, 0.25, 0.35])
    return first, first + length, pct


def _price_on(behaviour, regular, sale, day):
    if sale is None:  # a plain shop with nothing on offer
        return regular, None
    on_sale = sale[0] <= day < sale[1]
    if behaviour == "inflated":
        # A permanent tag 1.5x the real price; sales happen on top of it.
        price = _cents(regular * (1 - sale[2])) if on_sale else regular
        return price, _cents(regular * 1.5)
    if behaviour == "rule":
        # "-40% on everything", all the time: the "was" price is computed from
        # the sale price (price / (1 - pct)) and was never charged by anyone.
        price = _cents(regular * (1 - sale[2]))
        return price, _cents(price / (1 - sale[2]))
    if behaviour in ("honest", "promo"):
        if on_sale:
            return _cents(regular * (1 - sale[2])), regular
        return regular, None
    # plain: price moves, never a tag
    if on_sale:
        return _cents(regular * (1 - sale[2])), None
    return regular, None


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("out", type=Path, help="where to write the SQLite file")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    build(args.out, args.seed)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
