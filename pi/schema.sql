-- Price Intelligence schema. Applied by pi.db.connect() via PRAGMA user_version.

CREATE TABLE IF NOT EXISTS stores (
    id            INTEGER PRIMARY KEY,
    domain        TEXT    NOT NULL UNIQUE,
    name          TEXT,
    platform      TEXT    NOT NULL DEFAULT 'unknown',  -- shopify | jsonld | blocked | dead | unknown
    currency      TEXT,                                 -- ISO-4217, detected not guessed
    country       TEXT,
    status        TEXT    NOT NULL DEFAULT 'new',       -- new | ok | error | skipped
    last_error    TEXT,
    last_checked  TEXT,                                 -- ISO-8601 UTC
    last_ok       TEXT,
    product_count INTEGER NOT NULL DEFAULT 0,
    -- How much of this shop's catalogue wears a struck-through price, and how
    -- much of it wears the same one. A shop with half its catalogue at an
    -- identical -40% is running a promotion, not pricing products, and its tag
    -- is worth nothing as a reference. Recomputed each run by pi.reference.
    tag_share     REAL,
    round_share   REAL,
    blanket_pct   REAL,
    blanket_share REAL,
    -- jsonld stores are crawled a page at a time; the cursor walks their sitemap
    -- across runs so a 8,000-product catalogue is covered without hammering it.
    sitemap_cursor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    store_id    INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    external_id TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    brand       TEXT,
    url         TEXT    NOT NULL,
    image_url   TEXT,
    category    TEXT,
    UNIQUE (store_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_products_brand ON products(brand);

CREATE TABLE IF NOT EXISTS variants (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    external_id TEXT    NOT NULL,
    sku         TEXT,
    size        TEXT,
    size_norm   TEXT,
    color       TEXT,
    UNIQUE (product_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_variants_sku       ON variants(sku);
CREATE INDEX IF NOT EXISTS ix_variants_size_norm ON variants(size_norm);

-- Append-only, but a row is written only when the price, the struck-through price
-- or stock actually changed *in the shop's own currency*. A 6-hourly sweep over an
-- unchanged catalogue writes nothing, whatever the exchange rate did.
CREATE TABLE IF NOT EXISTS price_points (
    variant_id      INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
    ts              TEXT    NOT NULL,
    price_usd       REAL    NOT NULL CHECK (price_usd > 0),
    compare_at_usd  REAL    CHECK (compare_at_usd IS NULL OR compare_at_usd > 0),
    in_stock        INTEGER NOT NULL DEFAULT 1,
    currency        TEXT    NOT NULL,
    price_native    REAL    NOT NULL,
    -- The struck-through price as the shop quotes it. Comparisons are made on
    -- the native pair, never on the USD one: an exchange rate that moves while
    -- the shop stands still is not a price change.
    compare_at_native REAL,
    fx_rate         REAL    NOT NULL,
    PRIMARY KEY (variant_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_price_points_variant_ts ON price_points(variant_id, ts DESC);

-- Deduplication is per PRODUCT, not per variant: a hoodie discounted in six
-- sizes is one piece of news, not six notifications. price_bucket is a 5%-wide
-- logarithmic bucket (see pi.deals.price_bucket), and the UNIQUE constraint on
-- it is the race-safe backstop behind pi.deals.already_alerted.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variant_id   INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
    ts           TEXT    NOT NULL,
    price_usd    REAL    NOT NULL,
    price_bucket INTEGER NOT NULL,
    discount_pct REAL    NOT NULL,
    score        INTEGER NOT NULL,
    -- 0 for rows written by `pi seed`, which suppress a notification rather than
    -- being one. Without this the summary reports tens of thousands of "alerts"
    -- that nobody ever received.
    sent         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (product_id, price_bucket)
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts DESC);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    stores_ok      INTEGER NOT NULL DEFAULT 0,
    stores_failed  INTEGER NOT NULL DEFAULT 0,
    products_seen  INTEGER NOT NULL DEFAULT 0,
    points_written INTEGER NOT NULL DEFAULT 0,
    alerts_sent    INTEGER NOT NULL DEFAULT 0,
    -- Two independent facts about a run, which shared one `note` column until
    -- they started overwriting each other: a run can hit its alert cap *and* be
    -- cut off by Shopify, and the capped flag is what makes the next run
    -- reconsider the deals that did not fit.
    capped         INTEGER NOT NULL DEFAULT 0,
    blocked        INTEGER NOT NULL DEFAULT 0,
    note           TEXT
);

-- How a product might be recognised in another shop: the manufacturer's article
-- number above all (CW2288-111 is stocked by fourteen of the shops on the list),
-- with the shop's own SKU and a normalised title as weaker handles. This is what
-- makes a market price possible on a database where almost every variant has
-- been seen exactly once and has no history to compare against.
CREATE TABLE IF NOT EXISTS product_keys (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    key_type   TEXT    NOT NULL,   -- sku | style | title
    key        TEXT    NOT NULL,
    PRIMARY KEY (product_id, key_type, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_product_keys_key ON product_keys(key_type, key);
