#!/usr/bin/env python3
"""
Нормализация цветов: извлекает основной цвет из строки.
Примеры:
  "Black" -> BLACK
  "00000 Black" -> BLACK
  "200 WHITE/BLACK" -> MULTI (2 цвета)
  "Nero" -> BLACK (итальянский)
  "Men", "XL", "8" -> NULL (не цвет)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

# Цветовые слова для поиска (англ + итал + исп)
COLOR_WORDS = {
    'black': 'BLACK', 'nero': 'BLACK', 'negro': 'BLACK', 'noir': 'BLACK',
    'white': 'WHITE', 'bianco': 'WHITE', 'blanco': 'WHITE', 'blanc': 'WHITE',
    'grey': 'GREY', 'gray': 'GREY', 'gris': 'GREY',
    'blue': 'BLUE', 'navy': 'BLUE', 'azul': 'BLUE', 'blu': 'BLUE',
    'red': 'RED', 'crimson': 'RED', 'scarlet': 'RED', 'rojo': 'RED', 'rosso': 'RED',
    'green': 'GREEN', 'olive': 'GREEN', 'verde': 'GREEN',
    'brown': 'BROWN', 'chocolate': 'BROWN', 'marron': 'BROWN', 'marrone': 'BROWN',
    'beige': 'BEIGE', 'tan': 'BEIGE', 'cream': 'BEIGE',
    'purple': 'PURPLE', 'violet': 'PURPLE', 'viola': 'PURPLE',
    'orange': 'ORANGE', 'arancione': 'ORANGE', 'naranja': 'ORANGE',
    'yellow': 'YELLOW', 'gold': 'YELLOW', 'giallo': 'YELLOW', 'amarillo': 'YELLOW',
    'pink': 'PINK', 'rose': 'PINK', 'rosa': 'PINK',
    'multi': 'MULTI', 'multicolor': 'MULTI',
}

def normalize_color(color: str) -> str:
    if not color:
        return None
    s = color.strip()
    
    # Отфильтровать размеры/gender markers (не цвета)
    if re.match(r'^\d+(\.\d+)?\s*[MW]?$', s):  # "8", "10.5M"
        return None
    if s.lower() in ('men', 'women', 'unisex', 'kids', 'one size', 'new', 'xl', 'l', 'm', 's', 'xs', 'xxl'):
        return None
    
    # Найти все цветовые слова в строке
    found = []
    lower = s.lower()
    for word, canonical in COLOR_WORDS.items():
        if re.search(r'\b' + word + r'\b', lower):
            if canonical not in found:
                found.append(canonical)
    
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return 'MULTI'  # 2+ цвета

def main():
    engine = create_engine(DATABASE_URL)
    BATCH = 50000
    
    with engine.connect() as conn:
        max_id = conn.execute(text("SELECT MAX(id) FROM product_variants")).scalar() or 0
        print(f"Normalizing colors for {max_id} variants...")
        
        updated = 0
        for start in range(1, max_id + 1, BATCH):
            end = start + BATCH
            rows = conn.execute(text("""
                SELECT id, color FROM product_variants
                WHERE id >= :start AND id < :end AND normalized_color IS NULL
            """), {'start': start, 'end': end}).fetchall()
            
            for vid, color in rows:
                norm = normalize_color(color)
                conn.execute(text("""
                    UPDATE product_variants SET normalized_color = :norm WHERE id = :id
                """), {'norm': norm, 'id': vid})
            
            conn.commit()
            updated += len(rows)
            print(f"  Processed {updated} new variants")
        
        # Статистика
        stats = conn.execute(text("""
            SELECT 
                COUNT(*) AS total,
                COUNT(normalized_color) AS normalized,
                COUNT(*) - COUNT(normalized_color) AS null_count
            FROM product_variants
        """)).fetchone()
        
        print(f"\n✅ Total: {stats[0]:,} | Normalized: {stats[1]:,} | NULL: {stats[2]:,}")
        
        # Distribution по canonical colors
        distribution = conn.execute(text("""
            SELECT normalized_color, COUNT(*) AS count
            FROM product_variants
            WHERE normalized_color IS NOT NULL
            GROUP BY normalized_color
            ORDER BY count DESC
        """)).fetchall()
        
        print(f"\n📊 Distribution:")
        for color, count in distribution:
            print(f"   {color}: {count:,}")

if __name__ == "__main__":
    main()
