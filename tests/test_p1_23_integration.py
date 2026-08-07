"""
Интеграционный тест P1-23: Weighted Market Median
Проверяет, что новые колонки существуют и find_best_deals работает
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def test_db_schema():
    """Проверяем, что новые колонки существуют в БД"""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Проверяем stores.reliability_score
        result = conn.execute(text("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'stores' AND column_name = 'reliability_score'
        """)).fetchone()
        
        if result:
            print(f"✅ stores.reliability_score существует ({result[1]})")
        else:
            print("⚠️ stores.reliability_score НЕ найдена")
            # Показываем доступные колонки
            cols = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'stores' ORDER BY ordinal_position
            """)).fetchall()
            print(f"   Доступные колонки в stores: {[c[0] for c in cols]}")
        
        # Проверяем offers.updated_at
        result = conn.execute(text("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'offers' AND column_name = 'updated_at'
        """)).fetchone()
        
        if result:
            print(f"✅ offers.updated_at существует ({result[1]})")
        else:
            print("⚠️ offers.updated_at НЕ найдена")
            # Показываем доступные колонки
            cols = conn.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'offers' ORDER BY ordinal_position
            """)).fetchall()
            print(f"   Доступные колонки в offers: {[c[0] for c in cols]}")

def test_find_best_deals():
    """Запускаем find_best_deals с limit=3 для проверки"""
    from src.deal_engine import find_best_deals
    print("\n🧪 Запуск find_best_deals(limit=3)...")
    find_best_deals(limit=3)

if __name__ == "__main__":
    try:
        print("🔄 Проверка схемы БД...")
        test_db_schema()
        print("\n" + "="*60)
        test_find_best_deals()
        print("\n🎉 Интеграционный тест P1-23 завершён!")
    except Exception as e:
        print(f"❌ Ошибка интеграционного теста: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
