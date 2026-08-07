"""
Regression test для color-aware matching.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def test_color_consistency():
    """Матчи должны быть между одинаковыми normalized_color (если оба есть)"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        mismatched = conn.execute(text("""
            SELECT COUNT(*) 
            FROM product_matches pm
            JOIN product_variants pv1 ON pm.canonical_variant_id = pv1.id
            JOIN product_variants pv2 ON pm.matched_variant_id = pv2.id
            WHERE pv1.normalized_color IS NOT NULL 
              AND pv2.normalized_color IS NOT NULL 
              AND pv1.normalized_color != pv2.normalized_color
        """)).scalar()
        
        assert mismatched == 0, f"Found {mismatched} color-mismatched matches (should be 0)"
        print("✅ Color consistency enforced")

def test_color_normalization_coverage():
    """Достаточное количество вариантов имеют normalized_color"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        coverage = conn.execute(text("""
            SELECT 
                COUNT(*) AS total,
                COUNT(normalized_color) AS normalized
            FROM product_variants
        """)).fetchone()
        
        total, normalized = coverage
        # Минимум 10% variants с color должны быть нормализованы
        color_variants = conn.execute(text("SELECT COUNT(*) FROM product_variants WHERE color IS NOT NULL")).scalar()
        assert normalized > color_variants * 0.5, f"Color coverage {normalized}/{color_variants} too low"
        print(f"✅ Color coverage: {normalized:,}/{color_variants:,} ({100*normalized/color_variants:.1f}%)")

if __name__ == "__main__":
    test_color_consistency()
    test_color_normalization_coverage()
    print("\n🎉 All color tests passed")
