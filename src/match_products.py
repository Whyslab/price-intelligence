"""
Product Matching v2: связи одинаковых товаров в разных магазинах.

Правила v2 (вместо v1):
- только cross-store пары;
- SKU: btrim + upper;
- rejected: len<=4, generic (ONE SIZE/DEFAULT/...), числовые 1-6,
  числовые кроме 12-13 (GTIN UPC/EAN);
- не-GTIN группы требуют brand consistency (normalized brand_key);
- canonical = MIN(variant_id) — детерминированно, без циклов;
- confidence: 0.99 gtin_exact / 0.95 sku_exact.
Ребилд идемпотентен и атомен (одна транзакция).
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

INSERT_V2_SQL = """
INSERT INTO product_matches (canonical_variant_id, matched_variant_id, match_method, confidence_score)
WITH vo AS (
  SELECT DISTINCT ON (variant_id) variant_id, store_id
  FROM offers ORDER BY variant_id, store_id
),
norm AS (
  SELECT pv.id AS variant_id,
         upper(btrim(pv.sku)) AS sku,
         vo.store_id,
         -- canonical brand (brand_canonical) если есть mapping, иначе normalized_name
         regexp_replace(regexp_replace(lower(coalesce(bc.name, b.normalized_name, b.name, '')), '[^a-z0-9]', '', 'g'), '^by', '') AS brand_key,
         pv.normalized_size AS nsize,
         pv.normalized_color AS ncolor,
         pv.normalized_gender_age AS ngender,
         (btrim(pv.sku) ~ '^[0-9]{12,13}$') AS is_gtin
  FROM product_variants pv
  JOIN vo ON vo.variant_id = pv.id
  JOIN products p ON p.id = pv.product_id
  LEFT JOIN brands b ON b.id = p.brand_id
  LEFT JOIN brand_aliases ba ON ba.brand_id = b.id
  LEFT JOIN brand_canonical bc ON bc.id = ba.canonical_id
  WHERE btrim(coalesce(pv.sku,'')) <> ''
    AND length(btrim(pv.sku)) > 4
    AND upper(btrim(pv.sku)) NOT IN ('ONE SIZE','ONESIZE','DEFAULT','N/A','NA','STD','STANDARD','TEST','UNIVERSAL','GENERIC','OS')
    AND NOT (btrim(pv.sku) ~ '^[0-9]{1,6}$')
    AND NOT (btrim(pv.sku) ~ '^[0-9]+$' AND length(btrim(pv.sku)) NOT IN (12,13))
),
agg AS (
  SELECT sku,
         COUNT(DISTINCT store_id) AS stores,
         MIN(variant_id) AS canonical,
         COUNT(DISTINCT brand_key) AS brands
  FROM norm GROUP BY sku
)
SELECT a.canonical, n.variant_id,
       CASE WHEN n.is_gtin THEN 'gtin_exact' ELSE 'sku_exact' END,
       CASE WHEN n.is_gtin THEN 0.99 ELSE 0.95 END
FROM norm n
JOIN agg a ON a.sku = n.sku
JOIN norm cn ON cn.variant_id = a.canonical
WHERE a.stores >= 2
  AND n.variant_id <> a.canonical
  -- cross-store: canonical и matched не должны иметь общих stores
  AND NOT EXISTS (
      SELECT 1 FROM offers o1 
      JOIN offers o2 ON o1.store_id = o2.store_id
      WHERE o1.variant_id = a.canonical 
        AND o2.variant_id = n.variant_id
  )
  AND (n.is_gtin OR a.brands = 1)
  -- size-aware: матч только при совпадении normalized_size (NULL = unknown, разрешён)
  AND (n.nsize IS NULL OR cn.nsize IS NULL OR n.nsize = cn.nsize)
  -- color-aware: матч только при совпадении normalized_color (NULL = unknown, разрешён)
  AND (n.ncolor IS NULL OR cn.ncolor IS NULL OR n.ncolor = cn.ncolor)
  -- gender-aware: матч только при совместимости normalized_gender_age
  AND (n.ngender IS NULL OR cn.ngender IS NULL OR n.ngender = cn.ngender
       OR n.ngender = 'UNKNOWN' OR cn.ngender = 'UNKNOWN')
"""


def create_matches_table():
    """Таблица + FK + индексы (идемпотентно)."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS product_matches (
                id SERIAL PRIMARY KEY,
                canonical_variant_id INTEGER NOT NULL,
                matched_variant_id INTEGER NOT NULL,
                match_method VARCHAR(50) NOT NULL,
                confidence_score DECIMAL(3,2) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(canonical_variant_id, matched_variant_id)
            )
        """))
        conn.execute(text("""
            DO $$ BEGIN
                ALTER TABLE product_matches
                  ADD CONSTRAINT fk_pm_canonical FOREIGN KEY (canonical_variant_id)
                      REFERENCES product_variants(id) ON DELETE CASCADE;
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                ALTER TABLE product_matches
                  ADD CONSTRAINT fk_pm_matched FOREIGN KEY (matched_variant_id)
                      REFERENCES product_variants(id) ON DELETE CASCADE;
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_matches_canonical
            ON product_matches(canonical_variant_id)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_matches_matched
            ON product_matches(matched_variant_id)
        """))
    print("✅ product_matches table ready (v2)")


def match_by_sku():
    """Полный ребилд матчей по правилам v2. Идемпотентно, атомарно."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE product_matches"))
        res = conn.execute(text(INSERT_V2_SQL))
        print(f"✅ v2 matches rebuilt: {res.rowcount}")


def show_match_summary():
    """Статистика матчей v2."""
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM product_matches")).fetchone()[0]
        by_method = conn.execute(text(
            "SELECT match_method, COUNT(*) FROM product_matches GROUP BY 1 ORDER BY 1"
        )).fetchall()
        print(f"\n📊 Total matches: {total}")
        for m, c in by_method:
            print(f"   {m}: {c}")


if __name__ == "__main__":
    create_matches_table()
    match_by_sku()
    show_match_summary()
