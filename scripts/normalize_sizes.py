#!/usr/bin/env python3
"""
Нормализация размеров: извлекает число + gender marker.
Примеры:
  "04.5B" -> "4.5W"   (B = Women)
  "06.5M" -> "6.5M"   (M = Men)
  "Sz 11" -> "11"
  "0-12MO" -> "0K"    (kids)
  "$100", "*" -> NULL (мусор/цены)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def normalize_size(size: str) -> str:
    if not size:
        return None
    s = size.strip()
    if s in ('', '*', '-', '--') or s.startswith('$'):
        return None
    
    upper = s.upper()
    
    # Gender markers
    gender = None
    if 'GS' in upper or 'PS' in upper or 'TD' in upper or 'MO' in upper:
        gender = 'K'  # kids
    elif upper.endswith('B') or re.search(r'\bW\b', upper):
        gender = 'W'  # women
    elif upper.endswith('M') or re.search(r'\bM\b', upper):
        gender = 'M'  # men
    
    # Извлечь первое число с десятичной частью
    m = re.search(r'(\d+(?:[.,]\d+)?)', s)
    if not m:
        return None
    
    num = m.group(1).replace(',', '.')
    # Убрать leading zeros: 04.5 -> 4.5
    num = str(float(num)) if '.' in num else str(int(num))
    
    return num + (gender or '')

def main():
    engine = create_engine(DATABASE_URL)
    BATCH = 50000
    
    with engine.connect() as conn:
        max_id = conn.execute(text("SELECT MAX(id) FROM product_variants")).scalar() or 0
        print(f"Normalizing sizes for {max_id} variants...")
        
        updated = 0
        for start in range(1, max_id + 1, BATCH):
            end = start + BATCH
            rows = conn.execute(text("""
                SELECT id, size FROM product_variants
                WHERE id >= :start AND id < :end AND normalized_size IS NULL
            """), {'start': start, 'end': end}).fetchall()
            
            updates = [(normalize_size(r[1]), r[0]) for r in rows]
            
            for norm, vid in updates:
                conn.execute(text("""
                    UPDATE product_variants SET normalized_size = :norm WHERE id = :id
                """), {'norm': norm, 'id': vid})
            
            conn.commit()
            updated += len(rows)
            print(f"  Processed {updated}/{max_id} ({100*updated/max_id:.0f}%)")
        
        # Статистика
        stats = conn.execute(text("""
            SELECT 
                COUNT(*) AS total,
                COUNT(normalized_size) AS normalized,
                COUNT(*) - COUNT(normalized_size) AS null_count
            FROM product_variants
        """)).fetchone()
        
        print(f"\n✅ Total: {stats[0]:,} | Normalized: {stats[1]:,} | NULL: {stats[2]:,}")
        
        # Примеры нормализации
        examples = conn.execute(text("""
            SELECT size, normalized_size, COUNT(*) 
            FROM product_variants 
            WHERE normalized_size IS NOT NULL
            GROUP BY size, normalized_size
            ORDER BY COUNT(*) DESC
            LIMIT 15
        """)).fetchall()
        
        print(f"\n📊 Top normalizations:")
        for size, norm, count in examples:
            print(f"   '{size}' -> '{norm}' ({count:,}x)")

if __name__ == "__main__":
    main()
