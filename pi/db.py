"""SQLite access. Plain SQL, one connection helper, schema versioned by PRAGMA."""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .domains import same_shop

log = logging.getLogger(__name__)

SCHEMA_VERSION = 10
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
    # Columns first, because replaying schema.sql creates indexes and a new
    # index over a new column cannot be built before the column exists. This
    # bites only on tables that already exist: CREATE TABLE IF NOT EXISTS is a
    # no-op there, so the column arrives by ALTER or not at all.
    _add_columns(conn)
    # Every statement in schema.sql is CREATE ... IF NOT EXISTS, so replaying it
    # is how both "create from scratch" and "add what version N introduced" work.
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    if current == 1:
        _migrate_1_to_2(conn)
    if current in (1, 2):
        _migrate_2_to_3(conn)
    if current in (1, 2, 3):
        _migrate_3_to_4(conn)
    if current in (1, 2, 3, 4):
        _migrate_4_to_5(conn)
    if current in (1, 2, 3, 4, 5):
        _migrate_5_to_6(conn)
    if current < 10:
        _migrate_9_to_10(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Add alerts.sent, and mark the rows an earlier `pi seed` wrote.

    Seeding inserts every qualifying deal at one instant, so a timestamp shared
    by a large batch identifies it. Nothing legitimately notifies about hundreds
    of products in the same second.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "sent" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN sent INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        UPDATE alerts SET sent = 0 WHERE ts IN (
            SELECT ts FROM alerts GROUP BY ts HAVING COUNT(*) > 100
        )
        """
    )


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """Add the native struck-through price and the separate run flags.

    Also throws away the history rows that only ever recorded an exchange rate
    moving. Until now `record_price` compared prices in dollars, so 157.50 SGD
    read on two days at rates 1.2705 and 1.2713 looked like $123.97 falling to
    $123.89 — a "price change" the shop never made. Those rows are most of the
    history: they inflate the database, they make an all-time low out of a
    currency wobble, and they are about to be the input to a 30-day reference
    price, where they would do real damage.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(price_points)")}
    if "compare_at_native" not in columns:
        conn.execute("ALTER TABLE price_points ADD COLUMN compare_at_native REAL")
        # Reconstruct it from what we stored: compare_at_usd was rounded to the
        # cent, so this is approximate, and it is only used to compare with the
        # next observation. A 1% tolerance covers the rounding.
        conn.execute(
            "UPDATE price_points SET compare_at_native = round(compare_at_usd * fx_rate, 2)"
            " WHERE compare_at_usd IS NOT NULL"
        )

    run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "capped" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN capped INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE runs SET capped = 1 WHERE note = 'capped'")
    if "blocked" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE runs SET blocked = 1 WHERE note = 'blocked'")

    dropped = drop_fx_noise(conn)
    if dropped:
        log.info("removed %d history rows that only recorded an exchange rate moving", dropped)


# Columns of stores that pi.reference fills in. Kept as a list rather than
# written into one migration because a later version added to it, and a database
# already past that version would never have seen the addition.
TRUST_COLUMNS = ("tag_share", "round_share", "blanket_pct", "blanket_share")


def _add_missing_trust_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(stores)")}
    for column in TRUST_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE stores ADD COLUMN {column} REAL")


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Add the per-shop discount profile. The keys table comes from schema.sql.

    Existing products have no keys yet — `pi reindex` fills them in, and the run
    that follows keeps them current.
    """
    _add_missing_trust_columns(conn)


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Add stores.round_share, the share of a shop's discounts on a round 5% step.

    Its own step because version 4 shipped without it, and a database already at
    4 would otherwise never be offered the column.
    """
    _add_missing_trust_columns(conn)


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """Add stores.impersonate, and drop the www duplicates of two shops.

    `sneakerjunkiesusa.com` and `www.sneakerjunkiesusa.com` were both being
    collected as separate shops — 9,868 and 9,812 products for one catalogue.
    Market comparison already collapses them (pi.domains.same_shop), so they
    never counted as two shops agreeing, but they cost a slot in every sweep and
    twenty thousand rows in the database. The site list keeps the form without
    the prefix, so that is the row that stays.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(stores)")}
    if "impersonate" not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN impersonate INTEGER NOT NULL DEFAULT 0")

    duplicates = conn.execute(
        """
        SELECT d.id, d.domain FROM stores d
        JOIN stores keep ON keep.domain = substr(d.domain, 5)
        WHERE d.domain LIKE 'www.%'
        """
    ).fetchall()
    for row in duplicates:
        log.info("dropping %s — the same shop is already tracked without www.", row["domain"])
        conn.execute("DELETE FROM stores WHERE id = ?", (row["id"],))


# Adding a column used to mean writing it down twice: in schema.sql, for
# databases created from now on, and in a list here, for databases that already
# exist. Forgetting the second is silent — every test builds its database from
# schema.sql and so has the column either way, while the live database quietly
# does not. That is exactly what happened to `runs.scope`.
#
# So there is no list. schema.sql is the single statement of what a table should
# look like, and an existing table is brought up to it.
_COLUMN_DEF = re.compile(
    r"^\s*(?!UNIQUE\b|PRIMARY\b|FOREIGN\b|CHECK\b|CONSTRAINT\b)([a-z_]+)\s+(.+)$",
    re.I,
)


def _declared_columns(sql: str) -> dict[str, dict[str, str]]:
    """Column name -> its declaration, per table, as schema.sql declares them."""
    tables: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)\s*\((.*?)\n\)", sql, re.S | re.I
    ):
        columns: dict[str, str] = {}
        for line in match.group(2).splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            found = _COLUMN_DEF.match(line)
            if found:
                columns[found.group(1)] = found.group(2).strip()
        tables[match.group(1)] = columns
    return tables


def _migrate_9_to_10(conn: sqlite3.Connection) -> None:
    """Give the alerts key a reader, so a second subscriber is not robbed.

    The column itself arrives by ALTER in `_add_columns`. What cannot arrive
    that way is the UNIQUE constraint: it used to be (product_id, price_bucket),
    meaning one announcement of a product in the whole world, and SQLite has no
    way to redefine a constraint in place. So the table is rebuilt.

    Existing rows keep user_id 0, which is exactly right for them. They were
    sent to the only reader there was, and `pi seed` wrote the rest to say a
    discount was already running before the bot existed. Both mean nobody needs
    to hear it now.
    """
    for row in conn.execute("PRAGMA index_list(alerts)"):
        if not row["unique"]:
            continue
        columns = [c["name"] for c in conn.execute(f"PRAGMA index_info({row['name']})")]
        if columns == ["product_id", "price_bucket"]:
            break
    else:
        return  # already rebuilt, or a database created fresh from schema.sql

    log.info("rebuilding alerts so notifications are deduplicated per reader")
    # Foreign keys off for the rebuild, as SQLite's own instructions for
    # redefining a table require. The copy re-validates every product_id against
    # products, and one orphan — a row whose product was deleted by some earlier
    # surgery — would abort the migration. That does not fail a query, it fails
    # `connect`, which means the collector does not start at all. A rebuild that
    # can be stopped by a row it is only moving is not one to run on somebody's
    # live database.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _rebuild_alerts(conn)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_alerts(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE alerts_new (
            id           INTEGER PRIMARY KEY,
            product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            variant_id   INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
            ts           TEXT    NOT NULL,
            price_usd    REAL    NOT NULL,
            price_bucket INTEGER NOT NULL,
            discount_pct REAL    NOT NULL,
            score        INTEGER NOT NULL,
            sent         INTEGER NOT NULL DEFAULT 1,
            user_id      INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, product_id, price_bucket)
        );
        INSERT INTO alerts_new
            (id, product_id, variant_id, ts, price_usd, price_bucket,
             discount_pct, score, sent, user_id)
        SELECT id, product_id, variant_id, ts, price_usd, price_bucket,
               discount_pct, score, sent, 0
          FROM alerts;
        DROP TABLE alerts;
        ALTER TABLE alerts_new RENAME TO alerts;
        CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts DESC);
        """
    )


def _add_columns(conn: sqlite3.Connection) -> None:
    """Give every existing table the columns schema.sql says it should have.

    Only touches tables that already exist — one missing entirely is created
    complete by the schema replay that follows. Runs *before* that replay,
    because an index over a new column cannot be built before the column is
    there, and schema.sql declares both.

    A NOT NULL column with no default cannot be added to a table with rows in
    it. That is a real limit rather than something to work around: the fix is a
    default in schema.sql, so let SQLite say so instead of swallowing it.
    """
    declared = _declared_columns(SCHEMA_SQL.read_text(encoding="utf-8"))
    for table, columns in declared.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, definition in columns.items():
            if name not in existing:
                log.info("adding %s.%s", table, name)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def drop_fx_noise(conn: sqlite3.Connection) -> int:
    """Delete points identical to the one before them in the shop's own currency.

    "Identical" means the same asking price, the same currency, the same stock
    state and a struck-through price within 1% (it was reconstructed from a
    rounded dollar figure, so it cannot be compared exactly). The *earliest* row
    of such a run is kept: it is the one that records when the price actually
    took effect.
    """
    cur = conn.execute(
        """
        DELETE FROM price_points WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid,
                       price_native,
                       currency,
                       in_stock,
                       compare_at_native,
                       LAG(price_native)      OVER w AS prev_price,
                       LAG(currency)          OVER w AS prev_currency,
                       LAG(in_stock)          OVER w AS prev_stock,
                       LAG(compare_at_native) OVER w AS prev_compare
                FROM price_points
                WINDOW w AS (PARTITION BY variant_id ORDER BY ts)
            )
            WHERE prev_currency IS NOT NULL
              AND currency = prev_currency
              AND in_stock = prev_stock
              AND abs(price_native - prev_price) < 0.005
              AND (
                    (compare_at_native IS NULL AND prev_compare IS NULL)
                 OR (compare_at_native IS NOT NULL AND prev_compare IS NOT NULL
                     AND abs(compare_at_native - prev_compare)
                         <= 0.01 * max(compare_at_native, prev_compare))
              )
        )
        """
    )
    return cur.rowcount


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
    # Least-recently-collected first, so a run that cannot finish still makes
    # progress: the next one picks up where this one stopped instead of starting
    # at the top of the alphabet and re-collecting the same shops forever.
    return conn.execute(
        sql + " ORDER BY last_ok IS NOT NULL, last_ok, domain", params
    ).fetchall()


def productive_store_ids(conn: sqlite3.Connection, days: int) -> set[int]:
    """Stores that actually produced a notification in the last N days.

    The whole reason the queue is uneven. Measured on the live database: 38
    shops of 155 accounted for all 578 notifications ever sent, and ten of them
    for 412 of those. Collecting the other 117 as often as these is how an
    hourly sweep would turn back into a six-hourly one for the shops that matter.

    Seeded rows are excluded — `sent = 0` marks a discount that was already
    standing when collection began, which says nothing about the shop.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT DISTINCT p.store_id
          FROM alerts a
          JOIN products p ON p.id = a.product_id
         WHERE a.sent = 1 AND a.ts >= ?
        """,
        (since,),
    )
    return {row["store_id"] for row in rows}


# --- offers -----------------------------------------------------------------

def record_offers(
    conn: sqlite3.Connection,
    scored: list[int],
    qualifying: list,
    ts: str,
) -> tuple[int, int]:
    """Update what is on offer after a run. Returns (written, withdrawn).

    `scored` is every variant this run looked at, and `qualifying` the subset
    still worth showing. Everything scored and not
    qualifying is withdrawn — that is how a sale ending removes itself, and why
    the two lists have to be passed together rather than only the good news.

    `found_at` is when the price actually dropped, taken from the deal's own
    reading of the history rather than from the clock. A shop that has been
    running the same sale for a month would otherwise be presented as a fresh
    find every time it is scored — and on a first fill, everything at once.

    It also survives a rewrite: overwriting it each time the shop confirms the
    same price would reset it on every run.

    `checked_at` is the shop's `last_ok`, not the clock. Scoring happens over the
    whole catalogue while a run reads a slice of it, so stamping "now" said every
    offer had just been confirmed when most had not been looked at for days —
    and a dead listing from a shop last read on Tuesday sat at the top of the
    shelf looking like this morning's find. allikestore.com's -93% Wotherspoon
    was one: last read 28.08, and the URL now answers 404.
    """
    now = datetime.fromisoformat(ts)

    def dropped_at(deal) -> str | None:
        """When the price fell, or None if the history cannot say.

        Most variants have been seen exactly once — 2.7 million of 2.76 — so for
        most offers there is no earlier price to have fallen from. None here
        means "ask the database", and the statement below falls back to the
        first time this price was ever seen, which is a thing we do know.
        """
        hours = deal.dropped_hours_ago
        if hours is None:
            return None
        return (now - timedelta(hours=hours)).isoformat(timespec="seconds")

    keep = {deal.variant_id for deal in qualifying}
    stale = [(variant_id,) for variant_id in scored if variant_id not in keep]
    if stale:
        conn.executemany("DELETE FROM offers WHERE variant_id = ?", stale)
    conn.executemany(
        """
        INSERT INTO offers (
            variant_id, product_id, found_at, checked_at, price_usd,
            reference_usd, reference_source, discount_pct, saving_usd, score,
            all_time_low
        ) VALUES (
            ?, ?,
            COALESCE(
                ?,
                (SELECT MIN(ts) FROM price_points WHERE variant_id = ?),
                ?
            ),
            COALESCE(
                (SELECT s.last_ok FROM stores s
                   JOIN products p ON p.store_id = s.id
                  WHERE p.id = ?),
                ?
            ),
            ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT (variant_id) DO UPDATE SET
            checked_at       = excluded.checked_at,
            price_usd        = excluded.price_usd,
            reference_usd    = excluded.reference_usd,
            reference_source = excluded.reference_source,
            discount_pct     = excluded.discount_pct,
            saving_usd       = excluded.saving_usd,
            score            = excluded.score,
            all_time_low     = excluded.all_time_low,
            -- Only when the price actually moved: see the docstring.
            found_at = CASE
                WHEN abs(offers.price_usd - excluded.price_usd) < 0.005
                THEN offers.found_at ELSE excluded.found_at END
        """,
        [
            (
                deal.variant_id, deal.product_id,
                dropped_at(deal), deal.variant_id, ts,   # found_at's COALESCE
                deal.product_id, ts,                     # checked_at's COALESCE
                deal.price_usd,
                deal.reference_usd, deal.reference_source, deal.discount_pct,
                deal.saving_usd, deal.score, int(deal.all_time_low),
            )
            for deal in qualifying
        ],
    )
    return len(qualifying), len(stale)


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
    compare_at_native: float | None = None,
) -> bool:
    """Append a price point, but only if the shop actually changed something.

    "Changed" is judged in the shop's own currency. Judging it in dollars made
    every daily exchange-rate tick look like a price move: 157.50 SGD read twice
    at rates 1.2705 and 1.2713 became $123.97 and $123.89, and the second one was
    written down as news. That noise was most of the database, and it turned a
    currency wobble into an all-time low.

    Returns True when a row was written.
    """
    prev = latest_point(conn, variant_id)
    if prev is not None and (
        prev["currency"] == currency
        and abs(prev["price_native"] - price_native) < 0.005
        and _same_money(prev["compare_at_native"], compare_at_native)
        and bool(prev["in_stock"]) is bool(in_stock)
    ):
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO price_points
            (variant_id, ts, price_usd, compare_at_usd, in_stock, currency,
             price_native, compare_at_native, fx_rate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variant_id, ts or utcnow(), price_usd, compare_at_usd,
            int(in_stock), currency, price_native, compare_at_native, fx_rate,
        ),
    )
    return True


def _same_money(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 0.005


def set_product_keys(conn: sqlite3.Connection, product_id: int, keys: set[tuple[str, str]]) -> None:
    """Replace the handles under which this product can be matched elsewhere."""
    conn.execute("DELETE FROM product_keys WHERE product_id = ?", (product_id,))
    if keys:
        conn.executemany(
            "INSERT OR IGNORE INTO product_keys (product_id, key_type, key) VALUES (?, ?, ?)",
            [(product_id, key_type, key) for key_type, key in keys],
        )


def price_history(conn: sqlite3.Connection, variant_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM price_points WHERE variant_id = ? ORDER BY ts",
        (variant_id,),
    ).fetchall()


def prune_history(conn: sqlite3.Connection, keep_days: int = 180) -> int:
    """Delete price points older than keep_days, returning how many went.

    The newest point per variant is always kept whatever its age: it is the
    current price, and losing it would make the variant look unpriced. Only
    history beyond the window used for the median comparison is dropped.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(days=keep_days)
    ).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        DELETE FROM price_points
        WHERE ts < ?
          AND ts <> (SELECT MAX(ts) FROM price_points p WHERE p.variant_id = price_points.variant_id)
        """,
        (cutoff,),
    )
    return cur.rowcount


def drop_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove products and variants that no longer have any recorded price.

    A shop that delists a product stops returning it, so its rows linger with
    no history behind them. They cost space and can never produce a deal.
    """
    variants = conn.execute(
        "DELETE FROM variants WHERE id NOT IN (SELECT DISTINCT variant_id FROM price_points)"
    ).rowcount
    products = conn.execute(
        "DELETE FROM products WHERE id NOT IN (SELECT DISTINCT product_id FROM variants)"
    ).rowcount
    return {"variants": variants, "products": products}


# --- bot users --------------------------------------------------------------

_PROFILE_FIELDS = ("genders", "kinds", "sizes", "brands")


def get_bot_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bot_users WHERE id = ?", (user_id,)).fetchone()


def upsert_bot_user(
    conn: sqlite3.Connection, user_id: int, chat_id: str, username: str | None = None, **fields
) -> sqlite3.Row:
    """Create the row on first contact, then update only what was passed.

    Talking to the bot marks the reader reachable again. A run gives up on a
    chat Telegram calls closed, and the only evidence that it reopened is the
    person turning up in it — which is exactly this.
    """
    conn.execute(
        """
        INSERT INTO bot_users (id, chat_id, username, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET chat_id = excluded.chat_id,
                                       username = excluded.username,
                                       active = 1
        """,
        (user_id, chat_id, username, utcnow()),
    )
    if fields:
        assigns = ", ".join(f"{key} = :{key}" for key in fields)
        conn.execute(
            f"UPDATE bot_users SET {assigns} WHERE id = :id", {**fields, "id": user_id}
        )
    return get_bot_user(conn, user_id)


def offers_for(
    conn: sqlite3.Connection,
    genders: list[str] | None = None,
    kinds: list[str] | None = None,
    sizes: list[str] | None = None,
    brands: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "o.score DESC, o.discount_pct DESC",
    search: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """What is on offer for one person, best first. Returns (page, total).

    Every filter is optional and an absent one means "no opinion", so the
    default is the whole shelf. Sizes are matched against the variant on offer,
    not against the product: a shoe on sale in EU38 is not on sale in EU44, and
    saying otherwise is the fastest way to make the list untrustworthy.

    Gender allows NULL through whenever men are wanted. 87% of the catalogue
    never states a gender, so excluding the unknown would hide almost everything;
    asking for women is the narrow, clean filter, and that one does exclude it.

    `order_by` is interpolated, so it must never be built from anything a
    caller was handed: pi.web picks it out of a fixed map by key, which is the
    only way it is meant to be chosen.
    """
    where = ["1 = 1"]
    params: list = []
    if genders:
        if "women" in genders and "men" not in genders:
            where.append("p.gender = 'women'")
        elif "men" in genders and "women" not in genders:
            where.append("(p.gender = 'men' OR p.gender IS NULL)")
    if kinds:
        where.append(f"p.kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    if sizes:
        where.append(f"v.size_norm IN ({','.join('?' * len(sizes))})")
        params += sizes
    if brands:
        where.append(
            "(" + " OR ".join(["lower(p.brand_family) = ?"] * len(brands)) + ")"
        )
        params += [b.lower() for b in brands]
    if search:
        # Title and brand together, because people search for both in the same
        # box — "carhartt" and "cargo pant" are the same gesture.
        where.append("(p.title LIKE ? OR p.brand_family LIKE ? OR p.brand LIKE ?)")
        params += [f"%{search}%"] * 3
    clause = " AND ".join(where)

    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM offers o
          JOIN variants v ON v.id = o.variant_id
          JOIN products p ON p.id = o.product_id
         WHERE {clause}
        """,
        params,
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT o.*, v.size_norm, v.size, v.sku,
               p.title, p.url, p.image_url, p.brand, p.brand_norm, p.brand_family,
               p.gender, p.kind, s.domain, s.name AS store_name, s.country, s.currency
          FROM offers o
          JOIN variants v ON v.id = o.variant_id
          JOIN products p ON p.id = o.product_id
          JOIN stores s   ON s.id = p.store_id
         WHERE {clause}
         ORDER BY {order_by}
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return rows, total


def sizes_in_stock(conn: sqlite3.Connection, product_id: int) -> list[tuple[str, bool]]:
    """Every size of a product with whether the shop still has it.

    Shown on the card rather than filtered on, because the sizes that are left
    are the answer to a different question than "is mine there" — buying a
    present is exactly the case where the size that matters is not yours.
    """
    rows = conn.execute(
        """
        SELECT v.size_norm, v.size, pp.in_stock
          FROM variants v
          JOIN price_points pp ON pp.variant_id = v.id
         WHERE v.product_id = ?
           AND pp.ts = (SELECT MAX(ts) FROM price_points WHERE variant_id = v.id)
        """,
        (product_id,),
    ).fetchall()

    # Collapsed by label, because one label can be several variants: a shop that
    # sells a jacket in two colourways has "L" twice, and a shop that puts the
    # colour in the size field has "BIANCO" six times. Listing them as written
    # produced "Есть: BIANCO, BIANCO", which tells the reader nothing and looks
    # broken. A label counts as available if any variant behind it is.
    available: dict[str, bool] = {}
    for row in rows:
        label = (row["size_norm"] or row["size"] or "—").strip()
        available[label] = available.get(label, False) or bool(row["in_stock"])
    return sorted(available.items(), key=lambda item: _size_order(item[0]))


def _size_order(label: str) -> tuple:
    """Sort sizes the way a person reads them: EU44 before EU44.5 before EU45.

    Plain string order puts US10 before US2 and XL before XS, which makes a list
    of a dozen sizes hard to scan for the one you take.
    """
    clothing = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "OS"]
    if label in clothing:
        return (0, clothing.index(label), "")
    match = re.match(r"^([A-Z]+)(\d+(?:\.5)?)$", label)
    if match:
        return (1, 0.0, match.group(1), float(match.group(2)))
    return (2, 0.0, label)


def shelf_facets(conn: sqlite3.Connection) -> dict:
    """What the shelf actually contains, for building filters out of.

    Offered rather than hardcoded because a filter listing a size nothing is on
    sale in is worse than no filter: it invites a click that returns an empty
    page and says nothing about why. Counts come along for the same reason —
    "EU44 (312)" is a decision, "EU44" is a guess.
    """
    def tally(sql: str) -> list[dict]:
        return [
            {"value": row[0], "count": row[1]}
            for row in conn.execute(sql).fetchall()
            if row[0]
        ]

    return {
        "kinds": tally(
            """
            SELECT p.kind, COUNT(*) FROM offers o
              JOIN products p ON p.id = o.product_id
             GROUP BY p.kind ORDER BY 2 DESC
            """
        ),
        "genders": tally(
            """
            SELECT p.gender, COUNT(*) FROM offers o
              JOIN products p ON p.id = o.product_id
             GROUP BY p.gender ORDER BY 2 DESC
            """
        ),
        # Ordered by how much is on offer, not alphabetically: the sizes a
        # person scans for are the ones with anything behind them.
        "sizes": tally(
            """
            SELECT v.size_norm, COUNT(*) FROM offers o
              JOIN variants v ON v.id = o.variant_id
             GROUP BY v.size_norm ORDER BY 2 DESC LIMIT 60
            """
        ),
        "brands": tally(
            """
            SELECT p.brand_family, COUNT(*) FROM offers o
              JOIN products p ON p.id = o.product_id
             GROUP BY p.brand_family ORDER BY 2 DESC LIMIT 80
            """
        ),
        "total": conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
    }


# A key carried by more distinct products than this is not an article number.
# Measured on the live catalogue: genuine ones top out around thirteen —
# HF0794-600 is thirteen shops' worth of the same Foamposite — while `DIME` is
# carried by 413 unrelated products, because that shop puts the brand in the SKU
# field. Between those two numbers there is nothing, so the line is drawn wide.
MAX_KEY_FANOUT = 40


def same_article(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    """Every other shop selling what this product is, cheapest first.

    This is the question a shelf cannot answer on its own. 82% of what is on
    offer is on offer in one shop only, so for most things the honest answer is
    "nothing to compare with" — and for the rest, the comparison is the whole
    proof that a discount is real. A shop's own struck-through price is a claim;
    another shop charging twice as much for the same article is evidence.

    Only what is in stock right now, because a price nobody can pay is not a
    price. One row per merchant, at their cheapest, since a chain agreeing with
    itself across four countries is one opinion — see pi.domains.same_shop.
    """
    rows = conn.execute(
        """
        WITH mine AS (
            SELECT key_type, key FROM product_keys WHERE product_id = :pid
        ),
        usable AS (
            SELECT m.key_type, m.key FROM mine m
             WHERE (
                 SELECT COUNT(DISTINCT x.product_id) FROM product_keys x
                  WHERE x.key_type = m.key_type AND x.key = m.key
             ) <= :fanout
        ),
        siblings AS (
            SELECT DISTINCT pk.product_id
              FROM product_keys pk
              JOIN usable u ON u.key_type = pk.key_type AND u.key = pk.key
             WHERE pk.product_id <> :pid
        ),
        latest AS (
            SELECT variant_id, price_usd, currency, price_native
              FROM (
                SELECT variant_id, price_usd, currency, price_native, in_stock,
                       ROW_NUMBER() OVER (PARTITION BY variant_id ORDER BY ts DESC) AS rn
                  FROM price_points
                 WHERE variant_id IN (
                     SELECT id FROM variants WHERE product_id IN (SELECT product_id FROM siblings)
                 )
              )
             WHERE rn = 1 AND in_stock = 1
        )
        SELECT s.domain, s.name AS store_name, s.country, s.last_ok,
               p.title, p.url, p.id AS product_id,
               MIN(latest.price_usd) AS price_usd,
               latest.currency, latest.price_native
          FROM latest
          JOIN variants v ON v.id = latest.variant_id
          JOIN products p ON p.id = v.product_id
          JOIN stores   s ON s.id = p.store_id
         GROUP BY p.id
         ORDER BY price_usd
        """,
        {"pid": product_id, "fanout": MAX_KEY_FANOUT},
    ).fetchall()

    ours = conn.execute(
        "SELECT s.domain FROM products p JOIN stores s ON s.id = p.store_id WHERE p.id = ?",
        (product_id,),
    ).fetchone()
    us = same_shop(ours["domain"]) if ours else ""

    # Folded here rather than in SQL because "same merchant" is a judgement
    # about hostnames, not a column. A shop listing the same shoe twice, or
    # selling it in four countries, is one price to compare against.
    best: dict[str, sqlite3.Row] = {}
    for row in rows:
        shop = same_shop(row["domain"])
        if shop == us:
            continue
        if shop not in best or row["price_usd"] < best[shop]["price_usd"]:
            best[shop] = row
    return sorted(best.values(), key=lambda r: r["price_usd"])
