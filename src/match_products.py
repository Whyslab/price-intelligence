"""
Product Matching: определяет одинаковые товары в разных магазинах.
Создаёт связи в таблице product_matches.
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def create_matches_table():
    """Создаёт таблицу для хранения связей между вариантами."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
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
            CREATE INDEX IF NOT EXISTS idx_matches_canonical 
            ON product_matches(canonical_variant_id)
        """))
        
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_matches_matched 
            ON product_matches(matched_variant_id)
        """))
        
        conn.commit()
    print("✅ product_matches table ready")

def match_by_sku():
    """
    Уровень 1: Матчинг по SKU.
    Группирует варианты с одинаковым SKU из разных магазинов.
    """
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        print("\n🔍 Level 1: Matching by SKU...")
        
        # Находим SKU, которые встречаются в 2+ магазинах
        result = conn.execute(text("""
            WITH sku_groups AS (
                SELECT 
                    pv.sku,
                    pv.id as variant_id,
                    pv.product_id,
                    p.canonical_name,
                    o.store_id,
                    s.name as store_name
                FROM product_variants pv
                JOIN products p ON pv.product_id = p.id
                JOIN offers o ON pv.id = o.variant_id
                JOIN stores s ON o.store_id = s.id
                WHERE pv.sku IS NOT NULL AND pv.sku != ''
            ),
            multi_store_skus AS (
                SELECT sku
                FROM sku_groups
                GROUP BY sku
                HAVING COUNT(DISTINCT store_id) >= 2
            )
            SELECT 
                sg.sku,
                sg.variant_id,
                sg.canonical_name,
                sg.store_name
            FROM sku_groups sg
            JOIN multi_store_skus ms ON sg.sku = ms.sku
            ORDER BY sg.sku, sg.store_name
        """))
        
        rows = result.fetchall()
        
        # Группируем по SKU
        sku_groups = {}
        for row in rows:
            sku = row[0]
            if sku not in sku_groups:
                sku_groups[sku] = []
            sku_groups[sku].append({
                'variant_id': row[1],
                'name': row[2],
                'store': row[3]
            })
        
        print(f"  Found {len(sku_groups)} SKUs across multiple stores")
        
        # Создаём связи: первый вариант = canonical, остальные = matched
        matches_created = 0
        for sku, variants in sku_groups.items():
            if len(variants) < 2:
                continue
            
            canonical_id = variants[0]['variant_id']
            canonical_name = variants[0]['name']
            
            print(f"\n  📦 SKU: {sku}")
            print(f"     Canonical: {canonical_name} ({variants[0]['store']})")
            
            for v in variants[1:]:
                print(f"     Matched:   {v['name']} ({v['store']})")
                
                # Вставляем связь
                conn.execute(text("""
                    INSERT INTO product_matches 
                    (canonical_variant_id, matched_variant_id, match_method, confidence_score)
                    VALUES (:canon, :matched, 'sku_exact', 0.99)
                    ON CONFLICT (canonical_variant_id, matched_variant_id) DO NOTHING
                """), {
                    'canon': canonical_id,
                    'matched': v['variant_id']
                })
                matches_created += 1
        
        conn.commit()
        print(f"\n✅ Created {matches_created} SKU-based matches")

def show_match_summary():
    """Показывает статистику матчинга."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        print("\n" + "="*60)
        print("📊 Match Summary:")
        print("="*60)
        
        # Общее количество матчей
        result = conn.execute(text("""
            SELECT COUNT(*) FROM product_matches
        """))
        total = result.fetchone()[0]
        print(f"  Total matches: {total}")
        
        # Примеры сматченных товаров
        print("\n🔥 Top matched products:")
        result = conn.execute(text("""
            SELECT 
                pv.sku,
                p.canonical_name,
                COUNT(pm.matched_variant_id) + 1 as stores_count,
                STRING_AGG(DISTINCT s.name, ', ') as stores
            FROM product_matches pm
            JOIN product_variants pv ON pm.canonical_variant_id = pv.id
            JOIN products p ON pv.product_id = p.id
            JOIN offers o ON pm.canonical_variant_id = o.variant_id
            JOIN stores s ON o.store_id = s.id
            GROUP BY pv.sku, p.canonical_name
            ORDER BY stores_count DESC
            LIMIT 10
        """))
        
        for row in result.fetchall():
            print(f"  [{row[2]} stores] {row[1]}")
            print(f"     SKU: {row[0]} | In: {row[3]}")

if __name__ == "__main__":
    create_matches_table()
    match_by_sku()
    show_match_summary()
