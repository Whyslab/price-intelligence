"""SQLite access. Plain SQL, one connection helper, schema versioned by PRAGMA."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    """Timestamp in the single format the whole project stores and compares."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database, creating and migrating the schema as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema is version {current}, this build understands {SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return
    # Every statement in schema.sql is CREATE ... IF NOT EXISTS, so replaying it
    # is how both "create from scratch" and "add what version N introduced" work.
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- stores -----------------------------------------------------------------

def upsert_store(conn: sqlite3.Connection, domain: str, **fields) -> int:
    """Insert the store if new, update only the fields given, return its id."""
    conn.execute("INSERT OR IGNORE INTO stores (domain) VALUES (?)", (domain,))
    if fields:
        assigns = ", ".join(f"{k} = :{k}" for k in fields)
        conn.execute(
            f"UPDATE stores SET {assigns} WHERE domain = :domain",
            {**fields, "domain": domain},
        )
    return conn.execute("SELECT id FROM stores WHERE domain = ?", (domain,)).fetchone()[0]


def get_stores(
    conn: sqlite3.Connection, platforms: tuple[str, ...] = (), domains: tuple[str, ...] = ()
) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM stores", []
    where = []
    if platforms:
        where.append(f"platform IN ({','.join('?' * len(platforms))})")
        params += list(platforms)
    if domains:
        where.append(f"domain IN ({','.join('?' * len(domains))})")
        params += list(domains)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY domain", params).fetchall()


# --- catalogue --------------------------------------------------------------

def upsert_product(
    conn: sqlite3.Connection,
    store_id: int,
    external_id: str,
    title: str,
    url: str,
    brand: str | None = None,
    image_url: str | None = None,
    category: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO products (store_id, external_id, title, brand, url, image_url, category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (store_id, external_id) DO UPDATE SET
            title     = excluded.title,
            brand     = excluded.brand,
            url       = excluded.url,
            -- keep the last known image if this fetch happens to lack one
            image_url = COALESCE(excluded.image_url, products.image_url),
            category  = excluded.category
        """,
        (store_id, external_id, title, brand, url, image_url, category),
    )
    return conn.execute(
        "SELECT id FROM products WHERE store_id = ? AND external_id = ?",
        (store_id, external_id),
    ).fetchone()[0]


def upsert_variant(
    conn: sqlite3.Connection,
    product_id: int,
    external_id: str,
    sku: str | None = None,
    size: str | None = None,
    size_norm: str | None = None,
    color: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO variants (product_id, external_id, sku, size, size_norm, color)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (product_id, external_id) DO UPDATE SET
            sku       = excluded.sku,
            size      = excluded.size,
            size_norm = excluded.size_norm,
            color     = excluded.color
        """,
        (product_id, external_id, sku, size, size_norm, color),
    )
    return conn.execute(
        "SELECT id FROM variants WHERE product_id = ? AND external_id = ?",
        (product_id, external_id),
    ).fetchone()[0]


def latest_point(conn: sqlite3.Connection, variant_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM price_points WHERE variant_id = ? ORDER BY ts DESC LIMIT 1",
        (variant_id,),
    ).fetchone()


def record_price(
    conn: sqlite3.Connection,
    variant_id: int,
    price_usd: float,
    compare_at_usd: float | None,
    in_stock: bool,
    currency: str,
    price_native: float,
    fx_rate: float,
    ts: str | None = None,
) -> bool:
    """Append a price point, but only if something actually changed.

    Returns True when a row was written. Skipping unchanged observations keeps
    the history small enough that the median queries below stay cheap.
    """
    prev = latest_point(conn, variant_id)
    if prev is not None and (
        abs(prev["price_usd"] - price_usd) < 0.005
        and _same_money(prev["compare_at_usd"], compare_at_usd)
        and bool(prev["in_stock"]) is bool(in_stock)
    ):
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO price_points
            (variant_id, ts, price_usd, compare_at_usd, in_stock, currency, price_native, fx_rate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variant_id, ts or utcnow(), price_usd, compare_at_usd,
            int(in_stock), currency, price_native, fx_rate,
        ),
    )
    return True


def _same_money(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 0.005


def price_history(conn: sqlite3.Connection, variant_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM price_points WHERE variant_id = ? ORDER BY ts",
        (variant_id,),
    ).fetchall()
