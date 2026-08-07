"""
Интеграционный тест P1-23: Weighted Market Median
Проверяет calculate_deal_score_v2 на реальных данных из БД
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.deal_engine import get_weighted_market_median, calculate_deal_score_v2
from src.db import db_session, init_db

def test_weighted_median_integration():
    print("🔄 Инициализация БД...")
    init_db()
    
    # Находим SKU с ценами для теста
    query = """
        SELECT sku_id, COUNT(*) as price_count, AVG(price) as avg_price
        FROM prices
        WHERE price > 0 AND is_active = true
        GROUP BY sku_id
        HAVING COUNT(*) >= 2
        ORDER BY price_count DESC
        LIMIT 1
    """
    
    result = db_session.execute(query).fetchone()
    
    if not result:
        print("⚠️ Нет SKU с множественными ценами для теста")
        return
    
    sku_id = result.sku_id
    avg_price = float(result.avg_price)
    price_count = result.price_count
    
    print(f"📊 Тестовый SKU: {sku_id}")
    print(f"   Найдено цен: {price_count}, средняя: {avg_price:.2f}")
    
    # Тест 1: get_weighted_market_median
    print("\n🧪 Тест get_weighted_market_median...")
    median = get_weighted_market_median(sku_id)
    print(f"   Взвешенная медиана: {median:.2f}")
    
    assert median > 0, "Медиана должна быть > 0"
    assert abs(median - avg_price) / avg_price < 2.0, "Медиана не должна сильно отличаться от средней"
    print("   ✅ get_weighted_market_median работает корректно")
    
    # Тест 2: calculate_deal_score_v2
    print("\n🧪 Тест calculate_deal_score_v2...")
    current_price = avg_price * 0.85  # Цена на 15% ниже средней
    discount_percent = 15.0
    store_reliability = 0.8
    
    deal_result = calculate_deal_score_v2(sku_id, current_price, discount_percent, store_reliability)
    
    print(f"   Score: {deal_result['score']}/100")
    print(f"   Level: {deal_result['level']}")
    print(f"   Market Median: {deal_result['market_median']:.2f}")
    print(f"   Price Ratio: {deal_result['price_ratio']:.2f}")
    print(f"   Discount Tag: {deal_result['discount_tag']}")
    
    assert "score" in deal_result, "Должен быть score"
    assert "market_median" in deal_result, "Должна быть market_median"
    assert "price_ratio" in deal_result, "Должен быть price_ratio"
    assert "discount_tag" in deal_result, "Должен быть discount_tag"
    assert deal_result["market_median"] > 0, "Market median должна быть > 0"
    assert deal_result["score"] >= 0 and deal_result["score"] <= 100, "Score должен быть 0-100"
    
    print("   ✅ calculate_deal_score_v2 работает корректно")
    
    print("\n🎉 Все интеграционные тесты P1-23 пройдены!")

if __name__ == "__main__":
    try:
        test_weighted_median_integration()
    except Exception as e:
        print(f"❌ Ошибка интеграционного теста: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
