"""
Product Matching v3: связи одинаковых товаров в разных магазинах.

Правила v3 (улучшения v2):
- P1-14: EAN/GTIN используется как основной идентификатор (приоритет над SKU)
- P1-17: Strict size matching - только если оба значения известны и равны
- P1-18: Strict gender matching - убираем UNKNOWN wildcard
- P1-20: Canonical выбирается по quality_score (ean > size > color > sku length)
- только cross-store пары;
- не-GTIN группы требуют brand consistency (normalized brand_key);
- confidence: 0.99 ean_gtin / 0.97 sku_gtin / 0.95 sku_exact
- Ребилд идемпотентен и атомен (одна транзакция).
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
         regexp_replace(regexp_replace(lower(NULLIF(coalesce(bc.name, b.normalized_name, b.name, ''), '')), '[^a-z0-9]', '', 'g'), '^by', '') AS brand_key,
         pv.normalized_size AS nsize,
         pv.normalized_color AS ncolor,
         pv.normalized_gender_age AS ngender,
         -- P1-14: Проверяем оба поля: ean и sku на GTIN
         (btrim(coalesce(pv.ean, '')) ~ '^[0-9]{12,14}$') AS is_gtin_ean,
         (btrim(pv.sku) ~ '^[0-9]{12,13}$') AS is_gtin_sku,
         (btrim(coalesce(pv.ean, '')) ~ '^[0-9]{12,14}$') OR (btrim(pv.sku) ~ '^[0-9]{12,13}$') AS is_gtin,
         -- P1-20: Quality score для выбора canonical variant
         (
           CASE WHEN btrim(coalesce(pv.ean, '')) ~ '^[0-9]{12,14}$' THEN 100 ELSE 0 END +
           CASE WHEN pv.normalized_size IS NOT NULL AND pv.normalized_size != '' THEN 10 ELSE 0 END +
           CASE WHEN pv.normalized_color IS NOT NULL AND pv.normalized_color != '' THEN 10 ELSE 0 END +
           CASE WHEN pv.normalized_gender_age IS NOT NULL AND pv.normalized_gender_age != 'UNKNOWN' THEN 5 ELSE 0 END +
           CASE WHEN btrim(pv.sku) ~ '^[0-9]{12,13}$' THEN 50 ELSE 0 END +
           CASE WHEN length(btrim(coalesce(pv.sku, ''))) > 6 THEN 5 ELSE 0 END
         ) AS quality_score
  FROM product_variants pv
  JOIN vo ON vo.variant_id = pv.id
  JOIN products p ON p.id = pv.product_id
  LEFT JOIN brands b ON b.id = p.brand_id
  LEFT JOIN (SELECT DISTINCT brand_id, canonical_id FROM brand_aliases) ba ON ba.brand_id = b.id
  LEFT JOIN brand_canonical bc ON bc.id = ba.canonical_id
  WHERE btrim(coalesce(pv.sku,'')) <> ''
    AND length(btrim(pv.sku)) > 4
    AND upper(btrim(pv.sku)) NOT IN ('ONE SIZE','ONESIZE','DEFAULT','N/A','NA','STD','STANDARD','TEST','UNIVERSAL','GENERIC','OS')
    AND NOT (btrim(pv.sku) ~ '^[0-9]{1,6}$')
    AND NOT (btrim(pv.sku) ~ '^[0-9]+$' AND length(btrim(pv.sku)) NOT IN (12,13))
),
-- P1-20: Canonical = вариант с максимальным quality_score (а не MIN(id))
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY sku 
           ORDER BY quality_score DESC, variant_id ASC
         ) AS rank
  FROM norm
),
canonical_selection AS (
  SELECT sku, variant_id AS canonical
  FROM ranked
  WHERE rank = 1
),
agg AS (
  SELECT n.sku,
         COUNT(DISTINCT n.store_id) AS stores,
         cs.canonical,
         COUNT(DISTINCT n.brand_key) AS brands
  FROM norm n
  JOIN canonical_selection cs ON cs.sku = n.sku
  GROUP BY n.sku, cs.canonical
)
SELECT a.canonical, n.variant_id,
       -- P1-14: Приоритет метода матчинга: EAN > SKU
       CASE 
         WHEN n.is_gtin_ean THEN 'ean_gtin'
         WHEN n.is_gtin_sku THEN 'sku_gtin'
         ELSE 'sku_exact'
       END AS match_method,
       CASE 
         WHEN n.is_gtin_ean THEN 0.99
         WHEN n.is_gtin_sku THEN 0.97
         ELSE 0.95
       END AS confidence_score
FROM norm n
JOIN agg a ON a.sku = n.sku
JOIN norm cn ON cn.variant_id = a.canonical
WHERE a.stores >= 2
      AND NOT EXISTS (
          SELECT 1 FROM norm n2 WHERE n2.sku = n.sku 
            AND n2.variant_id != n.variant_id
            AND n2.ean IS NOT NULL AND n.ean IS NOT NULL AND n2.ean != n.ean
      )
  AND n.variant_id <> a.canonical
  -- cross-store: canonical и matched не должны иметь общих stores
  AND NOT EXISTS (
      SELECT 1 FROM offers o1 
      JOIN offers o2 ON o1.store_id = o2.store_id
      WHERE o1.variant_id = a.canonical 
        AND o2.variant_id = n.variant_id
  )
  AND (n.is_gtin OR a.brands = 1)
  -- P1-17: Strict size matching - только если оба известны и равны
  AND (
    (n.nsize IS NOT NULL AND cn.nsize IS NOT NULL AND n.nsize = cn.nsize)
    OR (n.nsize IS NULL AND cn.nsize IS NULL AND n.is_gtin)  -- только для GTIN разрешаем оба NULL
  )
  -- P1-17: Strict color matching
  AND (
    (n.ncolor IS NOT NULL AND cn.ncolor IS NOT NULL AND n.ncolor = cn.ncolor)
    OR (n.ncolor IS NULL AND cn.ncolor IS NULL AND n.is_gtin)
  )
  -- P1-18: Strict gender matching - убираем UNKNOWN wildcard
  AND (
    (n.ngender IS NOT NULL AND cn.ngender IS NOT NULL 
     AND n.ngender != 'UNKNOWN' AND cn.ngender != 'UNKNOWN'
     AND n.ngender = cn.ngender)
    OR n.is_gtin  -- для GTIN разрешаем любые комбинации
  )
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
