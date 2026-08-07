"""
Regression tests для currency_normalizer v2.
Зафиксированные баги:
- KRW 590000 → ~447 USD (не 590000 USD)
- Unknown currency → reject (не silent USD fallback)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal
from src.currency_normalizer import normalize_price, detect_currency

def test_krw_conversion():
    """KRW 590000 должен конвертироваться в ~447 USD"""
    price = Decimal('590000')
    url = 'https://dope-factory.com/products/test'
    currency = 'KRW'
    
    usd_price, original_currency, rate = normalize_price(price, url, currency)
    
    assert usd_price is not None, "KRW should not be rejected"
    assert original_currency == 'KRW'
    assert rate is not None
    # 590000 KRW / 1320 (примерный курс) ≈ 447 USD
    assert 400 < usd_price < 500, f"KRW 590000 should be ~447 USD, got {usd_price}"
    print("✅ KRW conversion correct")

def test_eur_conversion():
    """EUR 100 должен конвертироваться в ~108 USD"""
    price = Decimal('100')
    url = 'https://oneblockdown.it/products/test'
    currency = 'EUR'
    
    usd_price, original_currency, rate = normalize_price(price, url, currency)
    
    assert usd_price is not None
    assert original_currency == 'EUR'
    # 100 EUR / 0.92 (примерный курс) ≈ 108 USD
    assert 100 < usd_price < 120, f"EUR 100 should be ~108 USD, got {usd_price}"
    print("✅ EUR conversion correct")

def test_unknown_currency_reject():
    """Неизвестная валюта должна возвращать None (reject)"""
    price = Decimal('100')
    url = 'https://unknown-store.xyz/products/test'
    currency = 'XYZ'  # не в SUPPORTED_CURRENCIES
    
    usd_price, original_currency, rate = normalize_price(price, url, currency)
    
    assert usd_price is None, "Unknown currency should be rejected (None)"
    assert original_currency == 'UNKNOWN', f"Expected 'UNKNOWN', got '{original_currency}'"
    assert rate is None
    print("✅ Unknown currency rejected")

def test_usd_no_conversion():
    """USD должен оставаться USD без конвертации"""
    price = Decimal('100')
    url = 'https://kith.com/products/test'
    currency = 'USD'
    
    usd_price, original_currency, rate = normalize_price(price, url, currency)
    
    assert usd_price == Decimal('100')
    assert original_currency == 'USD'
    assert rate == Decimal('1.0')
    print("✅ USD no conversion")

def test_detect_currency_domain():
    """detect_currency должен определять валюту по домену"""
    # oneblockdown.it → EUR (суффикс .it)
    assert detect_currency('https://oneblockdown.it') == 'EUR'
    # dope-factory.com → KRW (в STORE_CURRENCY_MAP)
    assert detect_currency('https://dope-factory.com') == 'KRW'
    # Домены без mapping возвращают None (безопасное поведение)
    unknown = detect_currency('https://unknown-store-12345.com')
    assert unknown is None, f"Unknown domain should return None, got {unknown}"
    print("✅ Domain detection correct")

if __name__ == '__main__':
    test_krw_conversion()
    test_eur_conversion()
    test_unknown_currency_reject()
    test_usd_no_conversion()
    test_detect_currency_domain()
    print("\n🎉 All currency tests passed")
