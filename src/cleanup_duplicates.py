"""
Скрипт для удаления дубликатов и нормализации брендов.
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def cleanup():
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        print("🧹 Cleaning up duplicates...")
        
        # 1. Удаляем все данные для чистого старта
        print("  Deleting price_history...")
        conn.execute(text("DELETE FROM price_changes; DELETE FROM price_history"))
        
        print("  Deleting offers...")
        conn.execute(text("DELETE FROM offers"))
        
        print("  Deleting product_variants...")
        conn.execute(text("DELETE FROM product_variants"))
        
        print("  Deleting products...")
        conn.execute(text("DELETE FROM products"))
        
        print("  Deleting brands...")
        conn.execute(text("DELETE FROM brands"))
        
        print("  Deleting stores...")
        conn.execute(text("DELETE FROM stores"))
        
        conn.commit()
        
        # Сбрасываем последовательности (auto-increment)
        print("\n🔄 Resetting sequences...")
        conn.execute(text("ALTER SEQUENCE brands_id_seq RESTART WITH 1"))
        conn.execute(text("ALTER SEQUENCE stores_id_seq RESTART WITH 1"))
        conn.execute(text("ALTER SEQUENCE products_id_seq RESTART WITH 1"))
        conn.execute(text("ALTER SEQUENCE product_variants_id_seq RESTART WITH 1"))
        conn.execute(text("ALTER SEQUENCE offers_id_seq RESTART WITH 1"))
        
        conn.commit()
        
        print("\n✅ Cleanup completed!")

if __name__ == "__main__":
    cleanup()
