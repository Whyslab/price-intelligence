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

-- Append-only, but a row is written only when price, compare_at or stock actually
-- changed. A 6-hourly sweep over an unchanged catalogue writes nothing.
CREATE TABLE IF NOT EXISTS price_points (
    variant_id      INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
    ts              TEXT    NOT NULL,
    price_usd       REAL    NOT NULL CHECK (price_usd > 0),
    compare_at_usd  REAL    CHECK (compare_at_usd IS NULL OR compare_at_usd > 0),
    in_stock        INTEGER NOT NULL DEFAULT 1,
    currency        TEXT    NOT NULL,
    price_native    REAL    NOT NULL,
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
    note           TEXT
);
