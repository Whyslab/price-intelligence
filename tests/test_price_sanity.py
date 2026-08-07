"""
Regression tests для price sanity layer.
Зафиксированные баги:
- $20000 placeholder не должен искажать медиану
- Outlier ratio 3.0 (не 5.0)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pricing import sanitize_prices

def test_sanitize_outliers():
    """Outliers должны быть отфильтрованы"""
    prices = [100, 110, 105, 20000, 95]  # 20000 — outlier
    clean = sanitize_prices(prices)
    
    assert 20000 not in clean, "Outlier 20000 should be removed"
    assert len(clean) == 4, f"Expected 4 prices, got {len(clean)}"
    assert 100 in clean and 110 in clean
    print("✅ Outliers filtered")

def test_sanitize_all_same():
    """Если все цены одинаковые, sanitize должен вернуть их"""
    prices = [100, 100, 100]
    clean = sanitize_prices(prices)
    
    assert len(clean) == 3
    assert all(p == 100 for p in clean)
    print("✅ Same prices preserved")

def test_sanitize_ratio_3():
    """Outlier ratio 3.0: цена ×3 от медианы должна пройти, ×7 — нет"""
    # Медиана = 100 (5 раз 100 + одно отклонение), допустимый диапазон 33-300
    prices = [100, 100, 100, 100, 100, 300, 700]
    clean = sanitize_prices(prices)
    
    assert 300 in clean, "3× median should pass"
    assert 700 not in clean, "7× median should fail (outlier)"
    print("✅ Ratio 3.0 enforced")

def test_sanitize_empty():
    """Пустой список должен вернуть пустой"""
    assert sanitize_prices([]) == []
    print("✅ Empty list handled")

def test_sanitize_single():
    """Одна цена должна пройти"""
    assert sanitize_prices([100]) == [100]
    print("✅ Single price preserved")

if __name__ == '__main__':
    test_sanitize_outliers()
    test_sanitize_all_same()
    test_sanitize_ratio_3()
    test_sanitize_empty()
    test_sanitize_single()
    print("\n🎉 All sanity tests passed")
