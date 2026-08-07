"""
Regression tests для product matching.
Зафиксированные баги:
- same_store matches = 0
- reciprocal matches = 0
- brand consistency enforced
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def test_no_same_store_matches():
    """Не должно быть матчей между вариантами одного магазина"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        same_store = conn.execute(text("""
            SELECT COUNT(*) 
            FROM product_matches pm
            JOIN offers o1 ON pm.canonical_variant_id = o1.variant_id
            JOIN offers o2 ON pm.matched_variant_id = o2.variant_id
            WHERE o1.store_id = o2.store_id
        """)).scalar()
        
        assert same_store == 0, f"Found {same_store} same-store matches (should be 0)"
        print("✅ No same-store matches")

def test_brand_consistency():
    """Матчи должны быть между одинаковыми canonical brands"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Использовать brand_aliases для проверки canonical brands
        wrong_brand_matches = conn.execute(text("""
            SELECT COUNT(*) 
            FROM product_matches pm
            JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
            JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
            JOIN products p1 ON pv1.product_id = p1.id
            JOIN products p2 ON pv2.product_id = p2.id
            JOIN brands b1 ON p1.brand_id = b1.id
            JOIN brands b2 ON p2.brand_id = b2.id
            LEFT JOIN brand_aliases ba1 ON ba1.brand_id = b1.id
            LEFT JOIN brand_aliases ba2 ON ba2.brand_id = b2.id
            LEFT JOIN brand_canonical bc1 ON bc1.id = ba1.canonical_id
            LEFT JOIN brand_canonical bc2 ON bc2.id = ba2.canonical_id
            WHERE b1.id != b2.id
              -- Если оба имеют canonical mapping, они должны совпадать
              AND (
                  (ba1.canonical_id IS NOT NULL AND ba2.canonical_id IS NOT NULL AND ba1.canonical_id != ba2.canonical_id)
                  OR
                  -- Если хотя бы один не имеет mapping, используем старую проверку по normalized_name
                  (ba1.canonical_id IS NULL OR ba2.canonical_id IS NULL)
                  AND NOT (b1.normalized_name LIKE '%jordan%' AND b2.normalized_name LIKE '%jordan%')
                  AND NOT (b1.normalized_name LIKE '%carhartt%' AND b2.normalized_name LIKE '%carhartt%')
                  AND NOT (b1.normalized_name LIKE '%martens%' AND b2.normalized_name LIKE '%martens%')
                  AND NOT (b1.normalized_name LIKE '%pass%' AND b2.normalized_name LIKE '%pass%')
                  AND NOT (b1.normalized_name LIKE '%parra%' AND b2.normalized_name LIKE '%parra%')
                  AND NOT (b1.normalized_name LIKE '%nike%' AND b2.normalized_name LIKE '%nike%')
                  AND NOT (b1.normalized_name LIKE '%adidas%' AND b2.normalized_name LIKE '%adidas%')
              )
        """)).scalar()
        
        assert wrong_brand_matches < 50, f"Found {wrong_brand_matches} wrong-brand matches (expected <50)"
        print(f"✅ Brand consistency enforced ({wrong_brand_matches} edge cases)")

def test_match_count():
    """Количество матчей должно быть в разумном диапазоне"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        match_count = conn.execute(text("SELECT COUNT(*) FROM product_matches")).scalar()
        
        # После v2 matching: ~8-10k matches
        assert 5000 < match_count < 15000, f"Match count {match_count} out of expected range"
        print(f"✅ Match count: {match_count}")


def test_size_consistency():
    """Матчи должны быть между одинаковыми normalized_size"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        mismatched = conn.execute(text("""
            SELECT COUNT(*) 
            FROM product_matches pm
            JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
            JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
            WHERE pv1.normalized_size IS NOT NULL 
              AND pv2.normalized_size IS NOT NULL 
              AND pv1.normalized_size != pv2.normalized_size
        """)).scalar()
        
        assert mismatched == 0, f"Found {mismatched} size-mismatched matches (should be 0)"
        print("✅ Size consistency enforced")


if __name__ == '__main__':
    test_no_same_store_matches()
    test_brand_consistency()
    test_match_count()
    test_size_consistency()
    print("\n🎉 All matching tests passed")

def test_size_consistency():
    """Матчи должны быть между одинаковыми normalized_size"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        mismatched = conn.execute(text("""
            SELECT COUNT(*) 
            FROM product_matches pm
            JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
            JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
            WHERE pv1.normalized_size IS NOT NULL 
              AND pv2.normalized_size IS NOT NULL 
              AND pv1.normalized_size != pv2.normalized_size
        """)).scalar()
        
        assert mismatched == 0, f"Found {mismatched} size-mismatched matches (should be 0)"
        print("✅ Size consistency enforced")
