"""
Сливает дубли product_variants (один SKU в одном магазине = несколько записей).
Запуск: python -m src.dedup_variants
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def dedup():
    engine = create_engine(DATABASE_URL)
    total = 0
    with engine.begin() as conn:
        groups = conn.execute(text("""
            SELECT o.store_id, pv.sku, MIN(pv.id) AS keep_id,
                   (array_agg(pv.id ORDER BY pv.id)) AS ids
            FROM product_variants pv
            JOIN offers o ON o.variant_id = pv.id
            WHERE pv.sku IS NOT NULL AND pv.sku <> ''
            GROUP BY o.store_id, pv.sku
            HAVING COUNT(DISTINCT pv.id) > 1
        """)).fetchall()

        print(f"Found {len(groups)} duplicate groups")

        for store_id, sku, keep_id, ids in groups:
            for dup_id in ids:
                if dup_id == keep_id:
                    continue

                # 1. history: конфликты по PK удаляем, остальное переносим
                conn.execute(text("""
                    DELETE FROM price_history ph USING price_history kp
                    WHERE ph.variant_id = :dup AND kp.variant_id = :keep
                      AND ph.store_id = kp.store_id AND ph.timestamp = kp.timestamp
                """), {'dup': dup_id, 'keep': keep_id})
                conn.execute(text(
                    "UPDATE price_history SET variant_id = :keep WHERE variant_id = :dup"
                ), {'dup': dup_id, 'keep': keep_id})

                # 2. offers: дубль удаляем (у keep уже есть оффер магазина)
                conn.execute(text(
                    "DELETE FROM offers WHERE variant_id = :dup AND store_id = :store"
                ), {'dup': dup_id, 'store': store_id})

                # 3. product_matches: перенаправляем с удалением конфликтов UNIQUE
                for col, other in (('canonical_variant_id', 'matched_variant_id'),
                                   ('matched_variant_id', 'canonical_variant_id')):
                    conn.execute(text(f"""
                        DELETE FROM product_matches a USING product_matches b
                        WHERE a.{col} = :dup AND b.{col} = :keep
                          AND a.{other} = b.{other}
                    """), {'dup': dup_id, 'keep': keep_id})
                    conn.execute(text(f"""
                        UPDATE product_matches SET {col} = :keep WHERE {col} = :dup
                    """), {'dup': dup_id, 'keep': keep_id})

                # 4. удаляем дубль
                conn.execute(text("DELETE FROM product_variants WHERE id = :dup"),
                             {'dup': dup_id})
                total += 1

        # 5. продукты без вариантов
        conn.execute(text("""
            DELETE FROM products p
            WHERE NOT EXISTS (SELECT 1 FROM product_variants pv WHERE pv.product_id = p.id)
        """))

    print(f"✅ Merged {total} duplicate variants")

if __name__ == "__main__":
    dedup()
