"""
Удаляет product_matches с конфликтами по gender/age
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

engine = create_engine(DATABASE_URL)

with engine.begin() as conn:
    print("🗑️  Удаление гендерных конфликтов из product_matches...")
    print("=" * 70)
    
    # Подсчёт конфликтов перед удалением
    count_before = conn.execute(text("""
        SELECT COUNT(*)
        FROM product_matches pm
        JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
        JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
        WHERE pv1.normalized_gender_age IS NOT NULL
          AND pv2.normalized_gender_age IS NOT NULL
          AND pv1.normalized_gender_age != 'UNKNOWN'
          AND pv2.normalized_gender_age != 'UNKNOWN'
          AND pv1.normalized_gender_age != pv2.normalized_gender_age
    """)).scalar()
    
    print(f"📊 Найдено конфликтов для удаления: {count_before}")
    
    # Удаляем конфликты
    result = conn.execute(text("""
        DELETE FROM product_matches
        WHERE (canonical_variant_id, matched_variant_id) IN (
            SELECT pm.canonical_variant_id, pm.matched_variant_id
            FROM product_matches pm
            JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
            JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
            WHERE pv1.normalized_gender_age IS NOT NULL
              AND pv2.normalized_gender_age IS NOT NULL
              AND pv1.normalized_gender_age != 'UNKNOWN'
              AND pv2.normalized_gender_age != 'UNKNOWN'
              AND pv1.normalized_gender_age != pv2.normalized_gender_age
        )
    """))
    
    print(f"✅ Удалено {result.rowcount} конфликтных матчей")
    
    # Проверяем оставшиеся матчи
    total_after = conn.execute(text("SELECT COUNT(*) FROM product_matches")).scalar()
    print(f"📈 Осталось матчей: {total_after:,}")

print("\n🎉 Cleanup завершён!")
