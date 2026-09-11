"""SQLite access. Plain SQL, one connection helper, schema versioned by PRAGMA."""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import reference
from .domains import load_excluded, same_host, same_shop

log = logging.getLogger(__name__)

SCHEMA_VERSION = 15
SCHEMA_SQL = Path(__file__).with_name("schema.sql")


# How much write-ahead log to keep on disk between checkpoints. Big enough that
# a sweep writing a hundred thousand price points never has to stop and wait for
# one, small enough that it is not the largest thing in the data directory.
WAL_SIZE_LIMIT = 64 * 1024 * 1024


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
    # A checkpoint moves the journal into the database but leaves the file at
    # whatever size it reached, and nothing here ever shrinks it: the write-ahead
    # log had grown to 1,061 MB beside a 1,492 MB database, every byte of it
    # already checkpointed and none of it needed. With a limit set, each
    # checkpoint truncates the file back down to it.
    conn.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT}")
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
    rows = conn.execute(
        sql + " ORDER BY last_ok IS NOT NULL, last_ok, domain", params
    ).fetchall()

    # A shop that asked not to be visited is dropped here rather than in the
    # sweep, because this is the single door every path goes through: the sweep,
    # `detect`, `verify`, and `--stores` naming one by hand. The last one on
    # purpose — "we do not want you here" is not a preference a debug flag gets
    # to override.
    excluded = load_excluded()
    if not excluded:
        return rows
    kept = [row for row in rows if same_host(row["domain"]) not in excluded]
    if len(kept) != len(rows):
        log.info(
            "%d shop(s) skipped: they are in data/excluded.txt", len(rows) - len(kept)
        )
    return kept


# The reader the free digest files its publications under. Defined here as well
# as in pi.digest because the two counters below have to exclude it, and a
# module that counts alerts importing the module that sends them would be a
# cycle. See pi.digest.FREE_READER for what it means.
DIGEST_READER = -1


def productive_store_ids(conn: sqlite3.Connection, days: int) -> set[int]:
    """Stores that actually produced a notification in the last N days.

    The whole reason the queue is uneven. Measured on the live database: 38
    shops of 155 accounted for all 578 notifications ever sent, and ten of them
    for 412 of those. Collecting the other 117 as often as these is how an
    hourly sweep would turn back into a six-hourly one for the shops that matter.

    Seeded rows are excluded — `sent = 0` marks a discount that was already
    standing when collection began, which says nothing about the shop. So are
    the free digest's, for exactly the same reason: being picked as one of two
    advertisements says nothing about the shop either, and letting it count
    would give sweep priority to whichever shops the digest happened to choose.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT DISTINCT p.store_id
          FROM alerts a
          JOIN products p ON p.id = a.product_id
         WHERE a.sent = 1 AND a.user_id != ? AND a.ts >= ?
        """,
        (DIGEST_READER, since),
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
            category  = excluded.category,
            -- Back in the catalogue, so it was never gone. Cleared here rather
            -- than anywhere cleverer because this runs for every product of
            -- every fetch — full, partial or a single-product check — and the
            -- rule is the same for all three: seeing it is proof enough.
            missing_since = NULL
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


def snapshot(conn: sqlite3.Connection, dest: Path) -> int:
    """Write a consistent copy of the database to `dest`, returning its size.

    Copying the file is not the same thing, and the difference is not academic.
    The collector writes every hour into a write-ahead log that reaches tens of
    megabytes; a copy taken mid-sweep catches the database without the tail of
    the log that completes it. The backup that runs on this machine copies files
    and deliberately has no pre-run hooks, so the only place this can be made
    right is here.

    `sqlite3`'s online backup API reads through the same locking the collector
    uses, so the copy is a transaction-consistent database and the sweep is not
    interrupted to make it. The result has no `-wal` beside it — it is finished.

    Written to a temporary name and renamed, because the point of the file is to
    be a good copy: a backup that starts while this is half-written should find
    yesterday's whole snapshot rather than today's partial one.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".part")
    staging.unlink(missing_ok=True)
    target = sqlite3.connect(staging)
    try:
        conn.backup(target)
    finally:
        target.close()
    staging.replace(dest)
    return dest.stat().st_size


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


def mark_missing(
    conn: sqlite3.Connection, store_id: int, seen: Sequence[int], ts: str
) -> int:
    """Record that this shop no longer lists its other products. Returns how many.

    The caller must have read the shop's **whole** catalogue — see
    `FetchResult.enumerated`. Absence from a slice means nothing at all, and
    this is the one place in the project where a wrong reading eventually
    deletes somebody's data.

    Products already marked keep their original date: the grace period runs
    from when a product first went missing, not from the last time we noticed.

    Their offers go immediately. A card cannot both be gone from the shop and
    sit on the shelf saying "checked today", and that card — allikestore.com's
    -93% Wotherspoon, a 404 for a fortnight — is the single worst thing this
    project has ever shown anybody.
    """
    conn.execute("DROP TABLE IF EXISTS temp.pi_seen")
    conn.execute("CREATE TEMP TABLE pi_seen (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO pi_seen (id) VALUES (?)", ((pid,) for pid in seen)
    )
    gone = conn.execute(
        """
        UPDATE products SET missing_since = ?
         WHERE store_id = ?
           AND missing_since IS NULL
           AND id NOT IN (SELECT id FROM pi_seen)
        """,
        (ts, store_id),
    ).rowcount
    conn.execute(
        """
        DELETE FROM offers WHERE product_id IN (
            SELECT id FROM products
             WHERE store_id = ? AND missing_since IS NOT NULL
        )
        """,
        (store_id,),
    )
    conn.execute("DROP TABLE IF EXISTS temp.pi_seen")
    return gone


def mark_product_missing(conn: sqlite3.Connection, product_id: int, ts: str) -> bool:
    """One product, checked on its own page and answered with a 404.

    The same verdict `mark_missing` reaches for a whole shop at once, reached
    the expensive way for the shops a full catalogue read never covers.
    """
    changed = conn.execute(
        "UPDATE products SET missing_since = ? WHERE id = ? AND missing_since IS NULL",
        (ts, product_id),
    ).rowcount
    conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
    return changed > 0


def drop_delisted(conn: sqlite3.Connection, grace_days: int) -> int:
    """Delete products missing for longer than the grace period. Returns how many.

    The irreversible end of the three steps, and the reason there are three:
    a product is taken off the shelf the moment it goes missing, held while it
    might come back, and only then deleted with its variants and history. A
    shop that drops a shoe for a week and restocks it loses nothing.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(days=grace_days)
    ).isoformat(timespec="seconds")
    return conn.execute(
        "DELETE FROM products WHERE missing_since IS NOT NULL AND missing_since < ?",
        (cutoff,),
    ).rowcount


def confirm_offer(conn: sqlite3.Connection, product_id: int, ts: str) -> None:
    """Stamp an offer with a real check of this product, not of its shop.

    `record_offers` has to use the shop's `last_ok`, because scoring runs over
    the whole catalogue while a sweep reads a slice of it. `pi verify` opens
    one product's own page, so it can say something stronger, and this is the
    only place allowed to.
    """
    conn.execute("UPDATE offers SET checked_at = ? WHERE product_id = ?", (ts, product_id))


def latest_price_usd(conn: sqlite3.Connection, variant_id: int) -> float | None:
    """The newest price recorded for one variant, or None if it has none."""
    row = conn.execute(
        """
        SELECT price_usd FROM price_points
         WHERE variant_id = ?
         -- `rowid` breaks the tie, and there is one to break: a run that
         -- collects a price and then confirms it before announcing writes both
         -- points in the same second, and `ts DESC` alone may then answer with
         -- the older of the two.
         ORDER BY ts DESC, rowid DESC LIMIT 1
        """,
        (variant_id,),
    ).fetchone()
    return None if row is None else row[0]


def offers_to_confirm(
    conn: sqlite3.Connection, product_ids: Sequence[int], platforms: Sequence[str]
) -> list[sqlite3.Row]:
    """The same shape `stale_offers` returns, for named products.

    What a notification is about has to be asked after the run has decided to
    send it and before it arrives: the price it quotes was read from a catalogue
    page that may be an hour old, and an hour is long enough for the shop to
    have put it back up. Same columns as `stale_offers` so the one fetch path
    serves both.
    """
    if not product_ids or not platforms:
        return []
    pids = ",".join("?" * len(product_ids))
    marks = ",".join("?" * len(platforms))
    return conn.execute(
        f"""
        SELECT p.id AS product_id, p.url, p.external_id, p.store_id,
               s.domain, s.platform, s.currency,
               MIN(o.checked_at) AS checked_at, MAX(o.score) AS score
          FROM offers o
          JOIN products p ON p.id = o.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE p.id IN ({pids}) AND s.platform IN ({marks})
         GROUP BY p.id
        """,
        [*product_ids, *platforms],
    ).fetchall()


def stale_offers(conn: sqlite3.Connection, limit: int, platforms: Sequence[str]) -> list[sqlite3.Row]:
    """What is on the shelf and has gone longest without being looked at.

    Ordered by how long ago, then by score: a dead card at -80% on the first
    screen does more damage than a dead one at -31% on the fifth. One row per
    product — the check opens a page, and a page is a product.
    """
    if not platforms:
        return []
    marks = ",".join("?" * len(platforms))
    return conn.execute(
        f"""
        SELECT p.id AS product_id, p.url, p.external_id, p.store_id,
               s.domain, s.platform, s.currency,
               MIN(o.checked_at) AS checked_at, MAX(o.score) AS score
          FROM offers o
          JOIN products p ON p.id = o.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE p.missing_since IS NULL AND s.platform IN ({marks})
         GROUP BY p.id
         ORDER BY checked_at ASC, score DESC
         LIMIT ?
        """,
        [*platforms, limit],
    ).fetchall()


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


# Every column anything may write through `upsert_bot_user`. The union of what
# the bot writes and what `comp` needs — narrower policies live closer to their
# callers (see Bot.WRITABLE), but the SQL below is built from caller-supplied
# keys, and that is the surface an allowlist has to cover. Until recently this
# table held nothing but profile fields and the absence of a check here cost
# nothing; the day it held `paid_until`, a forged button payload was a free
# subscription. A future caller cannot forget to consult this one.
BOT_USER_COLUMNS = frozenset(
    {"chat_id", "username", "genders", "kinds", "sizes", "brands",
     "wizard_step", "onboarded", "active"}
)


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
        unknown = set(fields) - BOT_USER_COLUMNS
        if unknown:
            raise ValueError(f"not writable through upsert_bot_user: {sorted(unknown)}")
        assigns = ", ".join(f"{key} = :{key}" for key in fields)
        conn.execute(
            f"UPDATE bot_users SET {assigns} WHERE id = :id", {**fields, "id": user_id}
        )
    return get_bot_user(conn, user_id)


# --- subscription -----------------------------------------------------------

# How long a lapsed subscription keeps its feed after the date it was paid to.
#
# A card that fails once is the usual reason a subscription ends, and it is
# almost never the reason the person wanted it to. Cutting everything at
# midnight of the paid-to date turns a bank's hiccup into a lost reader, so the
# feed keeps running for three more days while the shelf does not — enough to
# notice the reminder, not enough to be a free month.
SUBSCRIPTION_GRACE_DAYS = 3


def _moment(stamp: str) -> datetime:
    """A stored timestamp as an aware datetime, whatever shape it was written in.

    `grant` writes `2026-09-05T12:56:00+00:00`, and everything in this project
    that writes a timestamp writes that. A human comping a subscriber does not:
    `UPDATE bot_users SET paid_until = datetime('now','+30 days')` is the obvious
    thing to type, and SQLite answers it with a space instead of the T and no
    offset at all.

    Both shapes have to be read, and neither may be compared as a string. ' '
    sorts below 'T', so the two forms of the same instant do not compare equal
    or even consistently — a hand-written row reads as earlier than an ISO one
    and gets swept out of a subscription up to a day early. And a naive datetime
    raises against an aware one, which took down the whole hourly notification
    pass for every reader, from one row.
    """
    parsed = datetime.fromisoformat(stamp.strip().replace(" ", "T", 1))
    # SQLite's datetime() is UTC. So is everything this project writes.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _paid_until(
    conn: sqlite3.Connection, only_active_plans: bool = False
) -> list[tuple[int, datetime]]:
    """Every reader with a paid-to date, parsed. The one place that reads them.

    Readers are counted in the dozens, so filtering them in Python costs
    nothing — and it buys the thing the SQL version could not have: exactly one
    interpretation of a timestamp in the whole subscription module.

    `only_active_plans` drops the long-lapsed, whose `plan` is already 'free'
    and who therefore cannot be the answer to "who should be expired". Without
    it the list grows with every reader who ever paid, forever, to build an
    `IN (...)` that matches none of them.
    """
    sql = "SELECT id, paid_until FROM bot_users WHERE paid_until IS NOT NULL"
    if only_active_plans:
        sql += " AND plan != 'free'"
    out = []
    for row in conn.execute(sql):
        try:
            out.append((int(row["id"]), _moment(row["paid_until"])))
        except ValueError:
            log.warning(
                "reader %s has an unreadable paid_until (%r) and is treated as "
                "unsubscribed", row["id"], row["paid_until"],
            )
    return out


def unreadable_dates(conn: sqlite3.Connection) -> int:
    """How many readers have a paid_until nothing can parse.

    Surfaced in the daily summary rather than only in the log. A row like this
    logs a warning on every state check — hourly, forever — and a warning
    nobody counts is a warning nobody acts on.
    """
    bad = 0
    for row in conn.execute(
        "SELECT paid_until FROM bot_users WHERE paid_until IS NOT NULL"
    ):
        try:
            _moment(row["paid_until"])
        except ValueError:
            bad += 1
    return bad


def subscription_state(
    conn: sqlite3.Connection, user_id: int, now: str | None = None
) -> str:
    """'paid', 'grace' or 'free' — the single answer to what this reader may see.

    Three states, not a boolean, because the two things a subscription gates
    stop at different moments: the shelf closes on the paid-to date, the feed
    runs three days longer. One flag cannot say that, and every place that tried
    would have to reinvent the same date arithmetic slightly differently.

    The comparison is done here in Python rather than in SQL. Timestamps in this
    database are `isoformat` — `2026-09-05T12:56:00+00:00` — and SQLite's own
    `datetime('now')` produces a space instead of the T and no offset at all.
    Comparing the two as strings is not an error; it silently matches nothing,
    which would read as "nobody is subscribed" and never raise.
    """
    row = get_bot_user(conn, user_id)
    if row is None or row["paid_until"] is None:
        return "free"
    moment = _moment(now) if now else datetime.now(UTC)
    try:
        until = _moment(row["paid_until"])
    except ValueError:
        log.warning(
            "reader %s has an unreadable paid_until (%r) and is treated as "
            "unsubscribed", user_id, row["paid_until"],
        )
        return "free"
    if moment <= until:
        return "paid"
    if moment <= until + timedelta(days=SUBSCRIPTION_GRACE_DAYS):
        return "grace"
    return "free"


def is_subscribed(conn: sqlite3.Connection, user_id: int, now: str | None = None) -> bool:
    """Whether the shelf opens for this reader. Grace does not count.

    Deliberately stricter than `subscription_state`: the grace period exists to
    keep a feed running through a failed renewal, not to hand out the thing
    being sold. Anything gating the shelf, the search or the article lookup asks
    this; only the feed asks for the state itself.
    """
    return subscription_state(conn, user_id, now) == "paid"


def grant(
    conn: sqlite3.Connection,
    user_id: int,
    days: int = 30,
    charge_id: str | None = None,
    stars: int = 0,
    recurring: bool = False,
) -> sqlite3.Row:
    """Extend a subscription, returning the reader.

    Time is added to whichever is later, now or the date already paid to, so
    that paying early adds a month instead of throwing away the rest of the one
    already bought. Paying after a lapse starts from today, because the days in
    between were not sold to anybody.

    `stars_paid` accumulates and `plan_since` is set once and never moved: a
    reader who leaves and comes back is not a new reader, and a refund of the
    last month should not erase that the year before was paid for.

    `recurring` says this charge is the one that renews itself, and only such a
    charge is remembered as `sub_charge_id`. A monthly subscriber who also buys
    a year would otherwise leave /cancel holding a one-off id, which Telegram
    refuses to cancel while the monthly charge goes on firing.
    """
    moment = datetime.now(UTC)
    # Read and write together. `--refund` clears four columns at once while this
    # computes a date from a row it read earlier; interleaved, the write here
    # would put back both the date and the charge id that were cleared to stop a
    # second refund.
    with transaction(conn):
        row = get_bot_user(conn, user_id)
        start = moment
        if row is not None and row["paid_until"]:
            start = max(moment, _moment(row["paid_until"]))
        fields: dict[str, object] = {
            "plan": "paid",
            "paid_until": (start + timedelta(days=days)).isoformat(timespec="seconds"),
            "plan_since": (
                row["plan_since"] if row and row["plan_since"]
                else moment.isoformat(timespec="seconds")
            ),
            "stars_paid": (row["stars_paid"] if row else 0) + stars,
        }
        if charge_id:
            fields["charge_id"] = charge_id
            if recurring:
                fields["sub_charge_id"] = charge_id
        assigns = ", ".join(f"{key} = :{key}" for key in fields)
        cursor = conn.execute(
            f"UPDATE bot_users SET {assigns} WHERE id = :id", {**fields, "id": user_id}
        )
    if cursor.rowcount != 1:
        # An UPDATE against a reader who is not there matches nothing and says
        # nothing, and the caller has already taken the money. Everywhere this
        # is reached the row was upserted first, so this cannot happen — which
        # is exactly why it must be loud if it ever does.
        raise LookupError(f"no reader {user_id} to grant a subscription to")
    return get_bot_user(conn, user_id)


# What "access, indefinitely" is written as. A date rather than a null or a flag,
# because every question this module answers is "until when?" — a second way of
# saying yes would have to be handled in `subscription_state`, `expire_due`,
# `expiring_soon`, the reminder and the summary, and the one that gets forgotten
# is the bug.
COMP_DAYS = 365 * 50


def comp(
    conn: sqlite3.Connection, user_id: int, days: int = COMP_DAYS
) -> sqlite3.Row:
    """Give somebody access without them paying for it.

    Exists because the alternative is a hand-written UPDATE, and that is not a
    hypothetical: `paid_until = datetime('now','+30 days')` is the obvious thing
    to type, it stores a timestamp in a shape nothing else here writes, and it
    used to take down the notification pass for every reader at once. A command
    that writes the right shape is the fix that removes the reason to type it.

    Creates the reader if they have never spoken to the bot, using their user id
    as the chat id — in a private chat Telegram makes those the same number, so
    the bot can write to them the moment they say /start, and the subscription
    is already waiting.

    No stars are recorded: nothing was paid, and the revenue line must not say
    otherwise.
    """
    if user_id <= 0:
        # Telegram gives groups and channels negative ids. Comping one makes it
        # a subscriber with an empty profile — which means no filtering — and
        # the next run publishes the whole discount feed into it. Refused here
        # rather than only in the caller, because there are two callers.
        raise ValueError(f"{user_id} is not a reader — group and channel ids are negative")
    if get_bot_user(conn, user_id) is None:
        upsert_bot_user(conn, user_id, chat_id=str(user_id))
    return grant(conn, user_id, days=days)


class StillRecurring(Exception):
    """Raised when taking access away would not take it away."""


def is_comped(row: sqlite3.Row | None) -> bool:
    """Whether this access was given rather than bought.

    Asked of the money, not of the date. A first attempt used "paid_until more
    than five years out", and it was wrong in both directions at once: `grant`
    is additive, so six yearly renewals cross five years and turn a customer who
    paid 9,000 stars into a gift — subtracted from the very number the summary
    exists to give — while a 90-day comp, which is what the panel's own help
    text offers, was counted as a purchase. Duration was never the question.
    `stars_paid == 0` beside a date is exactly "has access, paid nothing".

    Reporting only. Nothing about access consults this: a comped reader is a
    subscriber in every path that matters, which is the point of there being
    one notion of access and one column holding it.
    """
    return bool(row is not None and row["paid_until"] and not row["stars_paid"])


def is_open_ended(row: sqlite3.Row | None) -> bool:
    """Whether to write "indefinitely" instead of a date.

    What the date test is actually good at. A paid-to date in 2076 is correct
    and reads as a bug, so it is shown as what it means rather than as what it
    says; nothing decides anything from this.
    """
    if row is None or not row["paid_until"]:
        return False
    try:
        return _moment(row["paid_until"]) > datetime.now(UTC) + timedelta(days=365 * 5)
    except ValueError:
        return False


def revoke(conn: sqlite3.Connection, user_id: int, force: bool = False) -> bool:
    """Take access away now, keeping the profile and the starred things.

    Does not touch `stars_paid` or `charge_id`: this is not a refund, and
    somebody comped by mistake never paid anything to give back. A reader whose
    money must go back is `pi subscriptions --refund`.

    Refuses outright while a recurring charge is live, because otherwise this
    only appears to work: Telegram was never told to stop, so the next month's
    charge fires, `on_paid` grants a fresh month, and the reader has access
    again — silently, and still paying for it. Revoking worked permanently on
    comped readers and temporarily on exactly the ones it mattered for.
    """
    row = get_bot_user(conn, user_id)
    if row is not None and row["sub_charge_id"] and not force:
        raise StillRecurring(
            f"reader {user_id} has a live recurring subscription; "
            "refund it instead, or pass force to take access anyway"
        )
    cursor = conn.execute(
        "UPDATE bot_users SET plan = 'free', paid_until = NULL WHERE id = ?",
        (user_id,),
    )
    return cursor.rowcount > 0


def readers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everyone the bot knows, newest last. For the owner to look at."""
    return conn.execute(
        "SELECT * FROM bot_users ORDER BY created_at, id"
    ).fetchall()


def expire_due(conn: sqlite3.Connection, now: str | None = None) -> int:
    """Return readers whose grace has run out to the free plan, and count them.

    Only `plan` moves. The profile, the starred things and `plan_since` all stay
    exactly where they were: somebody coming back after six months should find
    their list, not an empty bot, and that is the cheapest subscriber there is
    to win back.
    """
    moment = _moment(now) if now else datetime.now(UTC)
    cutoff = moment - timedelta(days=SUBSCRIPTION_GRACE_DAYS)
    due = [
        user_id
        for user_id, until in _paid_until(conn, only_active_plans=True)
        if until < cutoff
    ]
    if not due:
        return 0
    marks = ",".join("?" * len(due))
    cursor = conn.execute(
        f"UPDATE bot_users SET plan = 'free' WHERE plan != 'free' AND id IN ({marks})",
        due,
    )
    return cursor.rowcount


def expiring_soon(
    conn: sqlite3.Connection, within_days: int = 3, now: str | None = None
) -> list[sqlite3.Row]:
    """Paid readers whose subscription ends within the window.

    What the reminder is sent from. Readers already past their date are not
    here: they are in grace and have had the reminder already.
    """
    moment = _moment(now) if now else datetime.now(UTC)
    horizon = moment + timedelta(days=within_days)
    due = sorted(
        (until, user_id)
        for user_id, until in _paid_until(conn)
        if moment <= until < horizon
    )
    if not due:
        return []
    ids = [user_id for _, user_id in due]
    marks = ",".join("?" * len(ids))
    rows = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT * FROM bot_users WHERE active = 1 AND id IN ({marks})", ids
        )
    }
    return [rows[user_id] for user_id in ids if user_id in rows]


def offers_for(
    conn: sqlite3.Connection,
    genders: list[str] | None = None,
    kinds: list[str] | None = None,
    sizes: list[str] | None = None,
    brands: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "o.score DESC, o.discount_pct DESC, o.variant_id DESC",
    search: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_discount: float | None = None,
    kids: bool = False,
    women: bool = False,
) -> tuple[list[sqlite3.Row], int]:
    """What is on offer for one person, best first. Returns (page, total).

    Every filter is optional and an absent one means "no opinion", so the
    default is the whole shelf. Sizes are matched against the variant on offer,
    not against the product: a shoe on sale in EU38 is not on sale in EU44, and
    saying otherwise is the fastest way to make the list untrustworthy.

    Gender allows NULL through whenever men are wanted. 87% of the catalogue
    never states a gender, so excluding the unknown would hide almost everything;
    asking for women is the narrow, clean filter, and that one does exclude it.

    Children's clothing is the one filter that is on by default, because it is
    the one nobody browsing this shelf has ever wanted and it is the only
    reading here confident enough to hide something on. `kids=True` puts it
    back, which is also how a misclassification is meant to be found.

    `order_by` is interpolated, so it must never be built from anything a
    caller was handed: pi.web picks it out of a fixed map by key, which is the
    only way it is meant to be chosen.
    """
    where = ["1 = 1"]
    params: list = []
    if not kids:
        where.append("(p.audience IS NULL OR p.audience <> 'kids')")
    # This is a men's shop, so what is read as women's is not a filter the
    # reader turns off — it is not stocked. The row is still written and still
    # counts as a price witness for the same article elsewhere; it is simply
    # never shown. `women=True` exists for the owner, because a misclassified
    # men's item is otherwise invisible and therefore unreportable.
    if not women:
        where.append("(p.gender IS NULL OR p.gender <> 'women')")
    # What is left is "confirmed men" and "nobody said". Asking for men narrows
    # to the confirmed half; 57% of the shelf says nothing, so this is a
    # preference rather than the shop's own boundary. Asking for anything else
    # answers "nothing", which is the truth: it is not that the filter found no
    # match today, it is that the shop does not carry it.
    if genders:
        if "men" in genders:
            where.append("p.gender = 'men'")
        else:
            where.append("1 = 0")
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
    # Price and discount are the two things a person narrows by before anything
    # else — "nothing over two hundred", "only real cuts" — and they are the two
    # the shelf could not express at all.
    if min_price is not None:
        where.append("o.price_usd >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("o.price_usd <= ?")
        params.append(max_price)
    if min_discount is not None:
        where.append("o.discount_pct >= ?")
        params.append(min_discount)
    if search:
        # Title and brand together, because people search for both in the same
        # box — "carhartt" and "cargo pant" are the same gesture.
        where.append("(p.title LIKE ? OR p.brand_family LIKE ? OR p.brand LIKE ?)")
        params += [f"%{search}%"] * 3
    clause = " AND ".join(where)

    # A product's sizes are folded into one card only when they all cost the
    # same. Measured on the live shelf: 1,539 products hold more than one card,
    # but 910 of them are priced differently by size — one Salomon is $90 in
    # US10 and $180 in US11 — and folding those would put a price on a card that
    # is not the price of the size somebody wants. The other 629 are the same
    # shoe listed three times at one price, which is the shelf repeating itself,
    # and folding them removes 636 cards.
    #
    # So the count is neither offers nor products: it is cards, and it has to be
    # counted the same way the page builds them.
    # `KEEP` is the rule, written once and used by both queries so the count can
    # never disagree with the page: keep the chosen row of a product whose sizes
    # all cost the same, and keep every row of one whose sizes do not.
    KEEP = "o.pick = 1 OR o.lo <> o.hi"
    folded = f"""
        SELECT o.*, v.size_norm, v.size, v.sku,
               p.title, p.url, p.image_url, p.brand, p.brand_norm,
               p.brand_family, p.gender, p.kind, p.store_id,
               s.domain, s.name AS store_name, s.country, s.currency,
               ROW_NUMBER() OVER (
                   PARTITION BY o.product_id
                   ORDER BY o.score DESC, o.price_usd ASC, o.variant_id DESC
               ) AS pick,
               COUNT(*) OVER (PARTITION BY o.product_id) AS folded_sizes,
               MIN(o.price_usd) OVER (PARTITION BY o.product_id) AS lo,
               MAX(o.price_usd) OVER (PARTITION BY o.product_id) AS hi
          FROM offers o
          JOIN variants v ON v.id = o.variant_id
          JOIN products p ON p.id = o.product_id
          JOIN stores s   ON s.id = p.store_id
         WHERE {clause}
    """

    total = conn.execute(
        f"SELECT COUNT(*) FROM ({folded}) o WHERE {KEEP}", params
    ).fetchone()[0]

    # Folding happens after the WHERE, which is what keeps a size filter
    # meaningful: asking for EU44 narrows to the offers in EU44 and only then
    # picks one per product, rather than picking first and then finding that the
    # winner was a different size.
    #
    # `sizes_on_offer` is how many rows went into this card — 1 whenever nothing
    # was folded, so the page can say "3 sizes at this price" and never imply it
    # about a card that is only one of several prices.
    rows = conn.execute(
        f"""
        SELECT *, CASE WHEN o.lo = o.hi THEN o.folded_sizes ELSE 1 END AS sizes_on_offer
          FROM ({folded}) o
         WHERE {KEEP}
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


def shelf_facets(conn: sqlite3.Connection, kids: bool = False,
                 women: bool = False) -> dict:
    """What the shelf actually contains, for building filters out of.

    Offered rather than hardcoded because a filter listing a size nothing is on
    sale in is worse than no filter: it invites a click that returns an empty
    page and says nothing about why. Counts come along for the same reason —
    "EU44 (312)" is a decision, "EU44" is a guess.

    Which is exactly why `kids` has to be passed in rather than assumed. These
    counts are a promise about what a click returns, and the shelf hides
    children's clothing unless asked — so a facet counting it would be a
    promise the page then breaks.

    Counted in products for the same reason, since `offers_for` folds a
    product's sizes into one card. Counting offers here would promise 33,215
    where the page then draws 31,653.
    """
    hide = "" if kids else " AND (p.audience IS NULL OR p.audience <> 'kids')"
    if not women:
        hide += " AND (p.gender IS NULL OR p.gender <> 'women')"

    def tally(sql: str) -> list[dict]:
        return [
            {"value": row[0], "count": row[1]}
            for row in conn.execute(sql).fetchall()
            if row[0]
        ]

    return {
        "kinds": tally(
            f"""
            SELECT p.kind, COUNT(DISTINCT o.product_id) FROM offers o
              JOIN products p ON p.id = o.product_id
             WHERE 1 = 1{hide}
             GROUP BY p.kind ORDER BY 2 DESC
            """
        ),
        "genders": tally(
            f"""
            SELECT p.gender, COUNT(DISTINCT o.product_id) FROM offers o
              JOIN products p ON p.id = o.product_id
             WHERE 1 = 1{hide}
             GROUP BY p.gender ORDER BY 2 DESC
            """
        ),
        # Ordered by how much is on offer, not alphabetically: the sizes a
        # person scans for are the ones with anything behind them.
        "sizes": tally(
            f"""
            SELECT v.size_norm, COUNT(DISTINCT o.product_id) FROM offers o
              JOIN variants v ON v.id = o.variant_id
              JOIN products p ON p.id = o.product_id
             WHERE 1 = 1{hide}
             GROUP BY v.size_norm ORDER BY 2 DESC LIMIT 60
            """
        ),
        "brands": tally(
            f"""
            SELECT p.brand_family, COUNT(DISTINCT o.product_id) FROM offers o
              JOIN products p ON p.id = o.product_id
             WHERE 1 = 1{hide}
             GROUP BY p.brand_family ORDER BY 2 DESC LIMIT 80
            """
        ),
        # Counted in cards by the same rule `offers_for` folds by, or the page
        # would head itself with a number it then fails to draw.
        "total": conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT ROW_NUMBER() OVER (
                           PARTITION BY o.product_id
                           ORDER BY o.score DESC, o.price_usd ASC, o.variant_id DESC
                       ) AS pick,
                       MIN(o.price_usd) OVER (PARTITION BY o.product_id) AS lo,
                       MAX(o.price_usd) OVER (PARTITION BY o.product_id) AS hi
                  FROM offers o
                  JOIN products p ON p.id = o.product_id
                 WHERE 1 = 1{hide}
            ) WHERE pick = 1 OR lo <> hi
            """
        ).fetchone()[0],
        # Where the prices actually start and stop, so a range control has ends
        # rather than guesses. Rounded outwards: a slider that cannot reach the
        # cheapest thing on the shelf is a bug people report as missing stock.
        "price": dict(
            zip(
                ("min", "max"),
                conn.execute(
                    f"SELECT COALESCE(MIN(o.price_usd), 0), COALESCE(MAX(o.price_usd), 0)"
                    f"  FROM offers o JOIN products p ON p.id = o.product_id"
                    f" WHERE 1 = 1{hide}"
                ).fetchone(),
                strict=True,
            )
        ),
        # The owner's own profile, so the page can open on their sizes instead
        # of on whatever the catalogue happens to have most of — which is
        # women's EU36, and is nobody's idea of a first screen.
        "mine": _owner_profile(conn),
    }


def _owner_profile(conn: sqlite3.Connection) -> dict:
    """Sizes, kinds and genders of the first reader, if there is one."""
    row = conn.execute(
        "SELECT genders, kinds, sizes FROM bot_users WHERE active = 1"
        " ORDER BY created_at, id LIMIT 1"
    ).fetchone()
    if row is None:
        return {}

    def split(value: str | None) -> list[str]:
        return [part.strip() for part in (value or "").split(",") if part.strip()]

    return {
        "genders": split(row["genders"]),
        "kinds": split(row["kinds"]),
        "sizes": split(row["sizes"]),
    }


# A key carried by more distinct products than this is not an article number.
# Measured on the live catalogue: genuine ones top out around thirteen —
# HF0794-600 is thirteen shops' worth of the same Foamposite — while `DIME` is
# carried by 413 unrelated products, because that shop puts the brand in the SKU
# field. Between those two numbers there is nothing, so the line is drawn wide.
MAX_KEY_FANOUT = 40


# How the cheapest live price is read for a set of products. Shared by
# `same_article`, which asks it about one product's siblings, and by
# `lookup_article`, which asks it about whatever a person typed. One copy,
# because both want the same three things and getting any of them differently
# would make the two disagree about the same catalogue: only the latest point
# per variant, only what is in stock, and one row per product at its cheapest.
_CHEAPEST_SQL = """
    WITH latest AS (
        SELECT variant_id, price_usd, currency, price_native
          FROM (
            SELECT variant_id, price_usd, currency, price_native, in_stock,
                   ROW_NUMBER() OVER (PARTITION BY variant_id ORDER BY ts DESC) AS rn
              FROM price_points
             WHERE variant_id IN (
                 SELECT id FROM variants WHERE product_id IN ({placeholders})
             )
          )
         WHERE rn = 1 AND in_stock = 1
    )
    SELECT s.domain, s.name AS store_name, s.country, s.last_ok,
           p.title, p.url, p.id AS product_id, p.brand_norm, p.audience,
           MIN(latest.price_usd) AS price_usd,
           latest.currency, latest.price_native,
           (SELECT MAX(discount_pct) FROM offers WHERE product_id = p.id) AS discount_pct
      FROM latest
      JOIN variants v ON v.id = latest.variant_id
      JOIN products p ON p.id = v.product_id
      JOIN stores   s ON s.id = p.store_id
     GROUP BY p.id
     ORDER BY price_usd
"""

# How many products one lookup will price. Well above MAX_KEY_FANOUT, because a
# search by name legitimately matches more rows than an article number does,
# and far below the point where the query stops being instant.
MAX_LOOKUP_PRODUCTS = 300


def _cheapest_per_product(
    conn: sqlite3.Connection, product_ids: Sequence[int]
) -> list[sqlite3.Row]:
    """The live price of each of these products, cheapest first."""
    ids = list(product_ids)[:MAX_LOOKUP_PRODUCTS]
    if not ids:
        return []
    sql = _CHEAPEST_SQL.format(placeholders=",".join("?" * len(ids)))
    return conn.execute(sql, ids).fetchall()


def _one_row_per_merchant(
    rows: list[sqlite3.Row], exclude: str | None = None
) -> list[sqlite3.Row]:
    """Fold a chain into one opinion, at its cheapest.

    Done here rather than in SQL because "same merchant" is a judgement about
    hostnames, not a column. A shop listing the same shoe twice, or selling it
    in four countries, is one price to compare against.
    """
    best: dict[str, sqlite3.Row] = {}
    for row in rows:
        shop = same_shop(row["domain"])
        if exclude is not None and shop == exclude:
            continue
        if shop not in best or row["price_usd"] < best[shop]["price_usd"]:
            best[shop] = row
    return sorted(best.values(), key=lambda row: row["price_usd"])


# Two rows may share a key and still be different things, and the shapes that
# invite it are the loose ones: `\d{6}-\d{2}` was taken for Puma and also
# matches a shop's own id with a European size stuck on the end, so `103134-40`
# claimed a Hey Dude and a Nike are the same article. Measured across the
# catalogue, 733 of the 18,617 style keys that link two shops — 3.9% — join
# products whose brands are both known and different. One of them offered a
# $30 Jordan cap as the cheaper version of a $200 adidas Yeezy.
#
# Dropping the pattern is the wrong cure: 998 of its 1,185 cross-shop keys are
# right. The brands are the cure. A disagreement is only believed when both
# sides actually name a brand — 78% of the catalogue names none, and silence is
# not a contradiction.
#
# This matters beyond the product card. `build_market_index` groups by the same
# keys to decide what the market charges, so a cap in a group of sneakers drags
# the median down and invents a discount below a market price that never
# existed — under `require_real_reference` that invention is now trusted.
_BRANDS_AGREE = """
    (   me.brand_family IS NULL OR TRIM(me.brand_family) = ''
     OR other.brand_family IS NULL OR TRIM(other.brand_family) = ''
     OR LOWER(me.brand_family) = LOWER(other.brand_family))
"""


def _sibling_product_ids(conn: sqlite3.Connection, product_id: int) -> list[int]:
    """Products other rows describe as the same article as this one.

    Keys carried by an implausible number of products are dropped rather than
    followed: a shop that writes its brand into the SKU field turns `DIME` into
    a claim about 413 unrelated products. And a key is not followed across two
    products that name different brands — see `_BRANDS_AGREE`.
    """
    return [
        row[0]
        for row in conn.execute(
            f"""
            WITH mine AS (
                SELECT key_type, key FROM product_keys WHERE product_id = :pid
            ),
            usable AS (
                SELECT m.key_type, m.key FROM mine m
                 WHERE (
                     SELECT COUNT(DISTINCT x.product_id) FROM product_keys x
                      WHERE x.key_type = m.key_type AND x.key = m.key
                 ) <= :fanout
            )
            SELECT DISTINCT pk.product_id
              FROM product_keys pk
              JOIN usable u ON u.key_type = pk.key_type AND u.key = pk.key
              JOIN products other ON other.id = pk.product_id
              JOIN products me    ON me.id = :pid
             WHERE pk.product_id <> :pid
               AND {_BRANDS_AGREE}
            """,
            {"pid": product_id, "fanout": MAX_KEY_FANOUT},
        ).fetchall()
    ]


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
    rows = _cheapest_per_product(conn, _sibling_product_ids(conn, product_id))
    ours = conn.execute(
        "SELECT s.domain FROM products p JOIN stores s ON s.id = p.store_id WHERE p.id = ?",
        (product_id,),
    ).fetchone()
    # Excluding the shop itself is the point: a price confirmed by
    # `bdgastore.com` and `shop.bdgastore.com` is one shop agreeing with itself.
    return _one_row_per_merchant(rows, exclude=same_shop(ours["domain"]) if ours else None)


def _products_by_key(
    conn: sqlite3.Connection, key_type: str, keys: Sequence[str]
) -> tuple[list[int], str | None]:
    """Products carrying any of these keys, and the key that was too common.

    A key on more than `MAX_KEY_FANOUT` products is not an article number and
    following it would answer a different question than the one asked: `DIME`
    is 413 unrelated products because one shop writes its brand into the SKU
    field. Saying so is better than returning them.
    """
    found: list[int] = []
    for key in keys:
        rows = conn.execute(
            "SELECT product_id FROM product_keys WHERE key_type = ? AND key = ?",
            (key_type, key),
        ).fetchall()
        if len(rows) > MAX_KEY_FANOUT:
            return [], key
        found += [row[0] for row in rows]
    return found, None


def _products_by_name(conn: sqlite3.Connection, text: str) -> list[int]:
    """Products whose title or brand contains what was typed."""
    like = f"%{text}%"
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT id FROM products
             WHERE title LIKE ? OR brand_norm LIKE ? OR brand LIKE ?
             LIMIT ?
            """,
            (like, like, like, MAX_LOOKUP_PRODUCTS),
        ).fetchall()
    ]


def lookup_article(conn: sqlite3.Connection, query: str, limit: int = 25) -> dict:
    """What every shop charges for one thing, cheapest first.

    The shelf can only answer this about something already discounted, because
    `same_article` starts from a product id and the page only has ids for what
    is on offer. This starts from what a person typed, so the answer exists for
    the whole catalogue — including the 95% of it that is at its normal price.

    Read in the order the keys deserve, which is the order `MarketIndex.identity`
    already uses: the manufacturer's article number is a number two shops arrive
    at independently; a shop's own SKU is weaker; a name is two shops happening
    to describe something similarly, and is a search rather than a match.

    The honest answer is usually "one shop". 17,936 of 105,707 article numbers
    in the catalogue are carried by more than one product, so for most things
    there is nothing to compare against — and `shops` of length one says exactly
    that rather than implying a comparison was made.

    An article number and a name are not the same question, and the answer says
    which was asked. `same_thing` is true only for an article number: those rows
    are one product priced by several merchants, folded so a chain counts once,
    and the cheapest of them is an answer. A name matches many different
    products — "air force 1 07" is 242 of them — and folding those by merchant
    would quietly turn a search into a comparison and call the cheapest of 242
    unrelated things a saving. So a name returns the products themselves.

    Children's clothing is not filtered out here. Asking for a thing by name is
    a clearer statement of intent than any classifier's reading of a title, the
    same reason a watched article still reaches its reader.
    """
    text = (query or "").strip()
    result: dict = {
        "query": text, "matched_by": None, "key": None, "same_thing": False,
        "products": 0, "shops": [], "found": 0, "too_common": None,
    }
    if len(text) < 3:
        return result

    # A cascade, strongest key first, and every step may come up empty without
    # ending the search: someone who types a code that is nobody's article
    # number still meant something by it, and the words are the last reading
    # left. Only an exhausted cascade is "nothing found".
    ids: list[int] = []
    codes = sorted(reference.style_codes(text))
    if codes:
        ids, blocked = _products_by_key(conn, reference.STYLE, codes)
        result["too_common"] = blocked
        if ids:
            result["matched_by"], result["key"] = reference.STYLE, codes[0]

    if not ids and " " not in text and len(text) >= 4:
        # No recognised article shape, but a single token is still worth trying
        # as a shop's own SKU. This is also where the junk is caught: `DIME` is
        # in the SKU field of 413 unrelated products because one shop puts its
        # brand there, and saying so is more useful than either the 413 or a
        # silent fall-through.
        sku_ids, blocked = _products_by_key(conn, reference.SKU, [text.upper()])
        result["too_common"] = result["too_common"] or blocked
        # A word is only allowed to *be* an article number if it is shaped like
        # one. `nike` sits in exactly one shop's SKU field, and taking that as
        # the answer would hand back a single shop to somebody who plainly asked
        # about a brand. The probe still ran, so the junk guard above still
        # fires — it is only the result that a word may not claim.
        if sku_ids and any(ch.isdigit() for ch in text):
            ids = sku_ids
            result["matched_by"], result["key"] = reference.SKU, text.upper()

    if not ids:
        ids = _products_by_name(conn, text)
        if ids:
            result["matched_by"], result["key"] = reference.TITLE, text

    if not ids:
        return result

    priced = _cheapest_per_product(conn, ids)
    same_thing = result["matched_by"] in (reference.STYLE, reference.SKU)
    result["same_thing"] = same_thing
    rows = _one_row_per_merchant(priced) if same_thing else priced
    result["products"] = len(ids)
    # How many there are, beside how many are being handed back: a caller that
    # only sees the truncated list reports the limit as the answer.
    result["found"] = len(rows)
    result["shops"] = rows[:limit]
    return result


# ---------------------------------------------------------------- favourites

def add_favorite(
    conn: sqlite3.Connection,
    user_id: int,
    product_id: int,
    variant_id: int | None = None,
) -> bool:
    """Start following a product for one reader. False if it was already followed.

    The price it costs right now is written down with it, because that is what
    "it got cheaper" will be measured against later.

    Nothing in stock falls back to the last price the shop was asking, in
    whatever state. Following something sold out is not the odd case, it is one
    of the two ordinary reasons to follow anything — measured on a shoe picked
    at random from the catalogue, every one of its 24 sizes was last seen out of
    stock — and "it was $300 when you starred it" is true and useful whether or
    not anybody could have bought it that day. The rigorous comparison is the
    reference price in the notification; this one is only ever shown when it is
    above what the thing costs now.
    """
    priced = _cheapest_per_product(conn, [product_id])
    if priced:
        was = priced[0]["price_usd"]
    else:
        row = conn.execute(
            """
            SELECT price_usd FROM price_points
             WHERE variant_id IN (SELECT id FROM variants WHERE product_id = ?)
             ORDER BY ts DESC LIMIT 1
            """,
            (product_id,),
        ).fetchone()
        was = row[0] if row else None
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO favorites
            (user_id, product_id, variant_id, added_at, notify, last_price_usd)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (user_id, product_id, variant_id, utcnow(), round(was, 2) if was else None),
    )
    return cur.rowcount > 0


def remove_favorite(conn: sqlite3.Connection, user_id: int, product_id: int) -> bool:
    """Stop following it. False if it was not being followed."""
    cur = conn.execute(
        "DELETE FROM favorites WHERE user_id = ? AND product_id = ?",
        (user_id, product_id),
    )
    return cur.rowcount > 0


def favorite_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """Which products this reader follows. What the page draws its hearts from."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT product_id FROM favorites WHERE user_id = ?", (user_id,)
        )
    }


def following(conn: sqlite3.Connection) -> dict[int, set[int]]:
    """Everything anybody is following, by reader.

    One query for the whole run. The union of these is what has to be scored —
    a followed product must be judged before anyone can be told about it, and
    scoring is the half that is shared — while the per-reader sets decide who
    actually hears.
    """
    out: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT user_id, product_id FROM favorites WHERE notify = 1"
    ):
        out.setdefault(row[0], set()).add(row[1])
    return out


def favorite_prices(conn: sqlite3.Connection, user_id: int) -> dict[int, float | None]:
    """What each of this reader's followed products cost when last reported."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT product_id, last_price_usd FROM favorites"
            " WHERE user_id = ? AND notify = 1",
            (user_id,),
        )
    }


def record_favorite_price(
    conn: sqlite3.Connection, user_id: int, product_id: int, price_usd: float
) -> None:
    """Remember what it cost when this reader was last written to about it."""
    conn.execute(
        "UPDATE favorites SET last_price_usd = ? WHERE user_id = ? AND product_id = ?",
        (round(price_usd, 2), user_id, product_id),
    )


def favorites_for(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """What this reader follows, newest first, priced as it stands now.

    Not a slice of the shelf, and it cannot be one. The shelf holds what is
    discounted; a followed product is usually at its normal price — that is
    ordinarily the whole reason for following it — so narrowing `offers` to
    these ids would show a person most of their own list missing with no
    explanation. So the list is built from the products themselves, and the
    discount is an extra fact about a row rather than the reason it is there.

    A product with nothing in stock keeps its place with `price` None. It is the
    single most useful row in the list — the thing you are waiting for, sold out
    — and dropping it would look like the star had been forgotten.
    """
    rows = conn.execute(
        """
        SELECT f.product_id, f.variant_id, f.added_at, f.notify, f.last_price_usd,
               p.title, p.url, p.image_url, p.brand, p.brand_family, p.brand_norm,
               p.gender, p.kind, p.audience,
               s.domain, s.name AS store_name, s.country, s.last_ok,
               (SELECT MAX(discount_pct) FROM offers WHERE product_id = p.id) AS discount_pct
          FROM favorites f
          JOIN products p ON p.id = f.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE f.user_id = ?
         ORDER BY f.added_at DESC
        """,
        (user_id,),
    ).fetchall()

    ids = [row["product_id"] for row in rows]
    # In batches, because `_cheapest_per_product` names every id in the query
    # and a long list of favourites is a perfectly reasonable thing to have.
    priced: dict[int, sqlite3.Row] = {}
    for start in range(0, len(ids), MAX_LOOKUP_PRODUCTS):
        for row in _cheapest_per_product(conn, ids[start : start + MAX_LOOKUP_PRODUCTS]):
            priced[row["product_id"]] = row

    out: list[dict] = []
    for row in rows:
        live = priced.get(row["product_id"])
        out.append(
            {
                "product_id": row["product_id"],
                "variant_id": row["variant_id"],
                "added_at": row["added_at"],
                "since_usd": row["last_price_usd"],
                "title": row["title"],
                "url": row["url"],
                "image_url": row["image_url"],
                "brand": row["brand_family"] or row["brand_norm"] or row["brand"],
                "store_name": row["store_name"],
                "domain": row["domain"],
                "country": row["country"],
                "checked_at": row["last_ok"],
                "discount_pct": row["discount_pct"],
                "price_usd": round(live["price_usd"], 2) if live else None,
                "currency": live["currency"] if live else None,
                "price_native": live["price_native"] if live else None,
            }
        )
    return out
