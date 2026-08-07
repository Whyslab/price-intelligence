"""
P1-21/22: Gender + Adult/Kids normalization
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import re
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

# УЛУЧШЕННЫЕ PATTERNS с более строгими границами
PATTERNS = {
    'KIDS': re.compile(
        r'\b(GS|PS|TD|Grade[\s-]?School|Preschool|Toddler|Infant|Kids?|Pre[\s-]?School)\b'
        r'|\b(Little|Big)[\s-]?Kids\b'
        r'|[\s(]GS[\s)]'
        r'|[\s(]PS[\s)]'
        r'|[\s(]TD[\s)]',
        re.IGNORECASE
    ),
    'WOMEN': re.compile(
        r"\b(WMNS|WMN|Women's|Womens|Women|Wmn)\b"
        r"|[\s(]W[\s)]",
        re.IGNORECASE
    ),
    'MEN': re.compile(
        r"\b(Men's|Mens)\b"
        r"|[\s(]Men[\s)]"
        r"|\bMen(?=\s+['’]?s\b)",
        re.IGNORECASE
    ),
    'UNISEX': re.compile(
        r'\b(Unisex)\b',
        re.IGNORECASE
    ),
}

# Исключения
FALSE_POSITIVES = re.compile(
    r'\b(PSA|PS[0-9]|PS-|PS\.|DMP|MNT|GMNT|NM)\b',
    re.IGNORECASE
)

# Специальные исключения для конкретных фраз
PHRASE_EXCEPTIONS = [
    re.compile(r'\bmen\s+in\s+black\b', re.IGNORECASE),  # "Men in Black" (movie)
]

def normalize_gender_age(name: str) -> str:
    if not name:
        return 'UNKNOWN'
    
    # Проверяем исключения для фраз
    for ex in PHRASE_EXCEPTIONS:
        if ex.search(name):
            return 'UNKNOWN'
    
    # Проверяем общие false positives (PSA grading и т.д.)
    has_fp = FALSE_POSITIVES.search(name) is not None
    
    for category, pattern in PATTERNS.items():
        if pattern.search(name):
            # Для KIDS проверяем PSA
            if has_fp and category == 'KIDS':
                # Проверяем, действительно ли это PS/GS или это PSA grading
                if re.search(r'\bPSA\b', name, re.IGNORECASE):
                    continue
            return category
    
    return 'UNKNOWN'

def main():
    engine = create_engine(DATABASE_URL)
    
    print("🔄 Запуск нормализации gender/age...")
    print("=" * 60)
    
    with engine.begin() as conn:
        # Проверяем наличие колонки
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'product_variants' AND column_name = 'normalized_gender_age'
        """))
        if not result.fetchone():
            print("➕ Добавляем колонку normalized_gender_age...")
            conn.execute(text("ALTER TABLE product_variants ADD COLUMN normalized_gender_age VARCHAR(20)"))
            print("✅ Колонка добавлена")
        
        # Создаём индекс
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_product_variants_gender_age 
            ON product_variants(normalized_gender_age)
        """))
        print("✅ Индекс готов")
        
        # Получаем все products
        products = conn.execute(text("""
            SELECT id, canonical_name FROM products WHERE canonical_name IS NOT NULL
        """)).fetchall()
        
        total_products = len(products)
        print(f"\n📊 Найдено продуктов: {total_products}")
        
        product_categories = {}
        category_counts = {'KIDS': 0, 'WOMEN': 0, 'MEN': 0, 'UNISEX': 0, 'UNKNOWN': 0}
        
        for row in products:
            prod_id, name = row
            cat = normalize_gender_age(name)
            product_categories[prod_id] = cat
            category_counts[cat] += 1
        
        print(f"\n📈 Распределение по продуктам:")
        for cat, count in category_counts.items():
            pct = (count / total_products * 100) if total_products > 0 else 0
            print(f"  {cat:10s}: {count:6d} ({pct:5.1f}%)")
        
        print("\n🔍 Примеры по категориям:")
        for cat in ['KIDS', 'WOMEN', 'MEN', 'UNISEX', 'UNKNOWN']:
            examples = [p[1] for p in products if product_categories[p[0]] == cat][:3]
            print(f"\n  [{cat}]")
            for ex in examples:
                print(f"    • {ex[:90]}")
        
        print("\n💾 Обновляем product_variants через executemany...")
        variants = conn.execute(text("SELECT id, product_id FROM product_variants")).fetchall()
        total_variants = len(variants)
        print(f"📊 Всего variants: {total_variants}")
        
        # Готовим batch updates
        batch_size = 5000
        updated_total = 0
        
        update_sql = text("UPDATE product_variants SET normalized_gender_age = :cat WHERE id = :id")
        
        for i in range(0, total_variants, batch_size):
            batch = variants[i:i + batch_size]
            params = [{'cat': product_categories.get(pid, 'UNKNOWN'), 'id': vid} 
                     for vid, pid in batch]
            
            if params:
                conn.execute(update_sql, params)
                updated_total += len(params)
                pct = (updated_total / total_variants * 100)
                print(f"  📊 Progress: {updated_total:,} / {total_variants:,} ({pct:.1f}%)")
        
        print(f"\n✅ Обновлено {updated_total:,} variants")
        
        print("\n📊 Итоговая статистика по variants:")
        result = conn.execute(text("""
            SELECT normalized_gender_age, COUNT(*) 
            FROM product_variants 
            WHERE normalized_gender_age IS NOT NULL
            GROUP BY normalized_gender_age 
            ORDER BY COUNT(*) DESC
        """))
        for row in result.fetchall():
            print(f"  {row[0]:10s}: {row[1]:8d}")

if __name__ == "__main__":
    main()
