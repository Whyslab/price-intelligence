"""
Regression tests для price_changes таблицы.
Зафиксированные баги:
- price_history раздувался дубликатами (53% waste)
- price_changes хранит только изменения (интервалы)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def test_price_changes_schema():
    """price_changes должна иметь правильную структуру"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        columns = conn.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'price_changes'
            ORDER BY ordinal_position
        """)).fetchall()
        
        column_names = [c[0] for c in columns]
        assert 'variant_id' in column_names
        assert 'store_id' in column_names
        assert 'price' in column_names
        assert 'started_at' in column_names
        assert 'ended_at' in column_names
        assert 'original_currency' in column_names
        assert 'exchange_rate' in column_names
        print("✅ price_changes schema correct")

def test_price_changes_intervals():
    """Каждый (variant, store) должен иметь непересекающиеся интервалы"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Найти пересечения интервалов
        overlaps = conn.execute(text("""
            SELECT pc1.variant_id, pc1.store_id
            FROM price_changes pc1
            JOIN price_changes pc2 ON pc1.variant_id = pc2.variant_id 
                                   AND pc1.store_id = pc2.store_id
                                   AND pc1.started_at < pc2.started_at
            WHERE pc2.started_at < COALESCE(pc1.ended_at, NOW())
            LIMIT 1
        """)).fetchall()
        
        assert len(overlaps) == 0, f"Found overlapping intervals: {overlaps}"
        print("✅ No interval overlaps")

def test_price_changes_compression():
    """price_changes должен быть значительно меньше price_history"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        history_count = conn.execute(text("SELECT COUNT(*) FROM price_history")).scalar()
        changes_count = conn.execute(text("SELECT COUNT(*) FROM price_changes")).scalar()
        
        ratio = history_count / changes_count if changes_count > 0 else 0
        assert ratio > 1.5, f"Compression ratio {ratio:.1f}x too low (expected >1.5x)"
        print(f"✅ Compression ratio: {ratio:.1f}x ({history_count} → {changes_count})")

if __name__ == '__main__':
    test_price_changes_schema()
    test_price_changes_intervals()
    test_price_changes_compression()
    print("\n🎉 All price_changes tests passed")
