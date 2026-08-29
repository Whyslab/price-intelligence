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
    sitemap_cursor INTEGER NOT NULL DEFAULT 0,
    -- Set when a shop answers only to a client presenting a browser's TLS
    -- fingerprint. Measured on the 25 shops that reply 403 to us: nine answer
    -- 200 that way and three of those go on to yield products. See
    -- pi.sources.impersonate.
    impersonate   INTEGER NOT NULL DEFAULT 0
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
    -- What the shop wrote (brand, category) versus what we made of it. The raw
    -- columns stay untouched so a better classifier can be re-run against them:
    -- `pi reclassify` rewrites everything below and nothing above.
    brand_norm   TEXT,   -- canonical spelling, NULL when no other shop corroborates it
    brand_family TEXT,   -- Jordan's family is Nike; what a brand filter matches on
    gender       TEXT,   -- 'men' | 'women' | NULL, and NULL means the shop did not say
    kind         TEXT,   -- 'shoes' | 'clothing' | 'accessories' | NULL
    UNIQUE (store_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_products_brand ON products(brand);
CREATE INDEX IF NOT EXISTS ix_products_brand_family ON products(brand_family);
CREATE INDEX IF NOT EXISTS ix_products_kind ON products(kind, gender);

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
    -- Who was told. Telegram's user id, or 0 meaning everybody.
    --
    -- Deduplication has to be per reader or the second subscriber is silently
    -- robbed: with one row per product the first person to be told closes the
    -- news for everyone, and the more subscribers there are the less each of
    -- them hears. So the key carries the reader.
    --
    -- 0 is not a user, it is a claim about all of them, and only two things
    -- write it. `pi seed` records the discounts that were already running when
    -- the bot arrived — those are what things cost, not news, and they are not
    -- news for somebody who subscribes tomorrow either. And every row written
    -- before this column existed, which was genuinely sent to the only reader
    -- there was. Both mean "nobody needs to hear this", which is what
    -- already_alerted asks.
    user_id      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, product_id, price_bucket)
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts DESC);

-- What is on offer right now, kept as it is found rather than searched for.
--
-- A run already scores every variant whose price moved and then throws away
-- everything past the notification cap. Searching the whole database instead
-- takes a minute or two, which is fine for `pi find` at a terminal and not fine
-- for someone pressing a button in the bot. So the run writes down what it
-- found: one row per variant that qualifies, replaced when that variant is
-- scored again and deleted when it stops qualifying.
--
-- This is a cache with a clear rule for going stale: a deal is only true until
-- its price moves, and a price moving is exactly when the row is rewritten.
CREATE TABLE IF NOT EXISTS offers (
    variant_id       INTEGER PRIMARY KEY REFERENCES variants(id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    found_at         TEXT    NOT NULL,   -- when this price was first seen, not when scored
    checked_at       TEXT    NOT NULL,   -- when the shop last confirmed it
    price_usd        REAL    NOT NULL,
    reference_usd    REAL    NOT NULL,
    reference_source TEXT    NOT NULL,
    discount_pct     REAL    NOT NULL,
    saving_usd       REAL    NOT NULL,
    score            INTEGER NOT NULL,
    all_time_low     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_offers_score ON offers(score DESC);
CREATE INDEX IF NOT EXISTS ix_offers_product ON offers(product_id);

-- Who is being shown things, and what they want shown. The split this table
-- exists for: filters.toml says what counts as a discount — a fact about the
-- product, the same for everyone — and this says what is worth putting in front
-- of one person. Keeping them apart is what lets a second person be added
-- without recomputing the first person's discounts.
CREATE TABLE IF NOT EXISTS bot_users (
    id          INTEGER PRIMARY KEY,       -- Telegram's user id
    chat_id     TEXT    NOT NULL,
    username    TEXT,
    created_at  TEXT    NOT NULL,
    -- Every filter below is NULL for "no opinion", which is not the same as
    -- empty. A user who skipped the sizes question wants every size; a user who
    -- answered it and then cleared the list is saying something else, and the
    -- wizard never leaves them in that state.
    genders     TEXT,                      -- 'men' and/or 'women', comma separated
    kinds       TEXT,                      -- shoes / clothing / accessories
    sizes       TEXT,                      -- normalised: EU44, US10.5, XL
    brands      TEXT,                      -- brand families, comma separated
    -- The wizard is resumable, because it runs over several messages and the
    -- process holding it in memory can be restarted between two of them.
    wizard_step TEXT,
    onboarded   INTEGER NOT NULL DEFAULT 0,
    -- Cleared when Telegram says this chat cannot be written to any more —
    -- the reader blocked the bot, or deleted the chat. Without it every run
    -- spends a request and a second of its notification budget on somebody who
    -- left, and the log fills with a failure nobody can act on. Talking to the
    -- bot again sets it back.
    active      INTEGER NOT NULL DEFAULT 1
);

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
    -- How many Shopify shops this run allowed itself. Recorded because the next
    -- run reads it: the budget adapts to how the last one went, and a number
    -- that only lived in a log cannot be adapted from.
    shopify_budget INTEGER,
    -- 'sweep' for a run that took its own turn from the queue, 'stores' for one
    -- given a list by hand. Only sweeps are comparable to each other, and the
    -- degradation check compares runs: a hand-run `--stores one.com` collecting
    -- one shop is not a collector that has stopped working.
    scope          TEXT NOT NULL DEFAULT 'sweep',
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
