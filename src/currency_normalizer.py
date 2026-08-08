import os
"""
Currency Normalizer v2: конвертирует цены в USD с полным аудитом.
- Reject при неизвестной валюте (вместо auto-USD)
- Сохранение использованного курса в БД
- Логирование ошибок в currency_errors
"""
import requests
from decimal import Decimal
from urllib.parse import urlparse
import json
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

FIXER_API_KEY = os.getenv("FIXER_API_KEY", "YOUR_API_KEY_HERE")
CACHE_FILE = "exchange_rates.json"
CACHE_TTL_HOURS = 24

STORE_CURRENCY_MAP = {
    'dope-factory.com': 'KRW',
    'laferramenta.org': 'EUR',
    'oneblockdown.it': 'EUR',
    'starcowparis.com': 'EUR',
    'slamcity.com': 'GBP',
    'noteshop.co.uk': 'GBP',
}

DOMAIN_CURRENCY_MAP = {
    '.kr': 'KRW', '.jp': 'JPY', '.cn': 'CNY',
    '.eu': 'EUR', '.de': 'EUR', '.fr': 'EUR', '.it': 'EUR',
    '.es': 'EUR', '.nl': 'EUR', '.be': 'EUR', '.at': 'EUR',
    '.uk': 'GBP', '.co.uk': 'GBP',
    '.ca': 'CAD', '.au': 'AUD', '.com.au': 'AUD',
    '.ch': 'CHF', '.se': 'SEK', '.no': 'NOK', '.dk': 'DKK',
    '.pl': 'PLN', '.cz': 'CZK', '.hu': 'HUF',
}

SUPPORTED_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'KRW', 'CAD', 'AUD', 'CHF', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'HUF'}

def detect_currency(url: str) -> str:
    """Определяет валюту по домену. Возвращает None если неизвестна."""
    domain = urlparse(url).netloc.lower()
    for store_domain, currency in STORE_CURRENCY_MAP.items():
        if store_domain in domain:
            return currency
    for suffix, currency in DOMAIN_CURRENCY_MAP.items():
        if domain.endswith(suffix):
            return currency
    return None  # Раньше возвращал 'USD' — это было опасно

def load_cached_rates() -> dict:
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            cached_time = datetime.fromisoformat(cache['timestamp'])
            if datetime.now() - cached_time < timedelta(hours=CACHE_TTL_HOURS):
                return cache['rates'], cached_time
    except:
        pass
    return None, None

def save_rates_to_cache(rates: dict):
    cache = {'timestamp': datetime.now().isoformat(), 'rates': rates}
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def get_fallback_rates() -> dict:
    """Fallback курсы (только для известных валют). Формат: 1 USD = X currency"""
    return {
        'EUR': Decimal('0.92'),
        'GBP': Decimal('0.79'),
        'JPY': Decimal('149.5'),
        'KRW': Decimal('1320.0'),
        'CAD': Decimal('1.36'),
        'AUD': Decimal('1.53'),
        'CHF': Decimal('0.88'),
        'SEK': Decimal('10.5'),
        'NOK': Decimal('10.8'),
        'DKK': Decimal('6.87'),
        'PLN': Decimal('4.02'),
        'CZK': Decimal('23.1'),
        'HUF': Decimal('355.0'),
    }

def get_exchange_rates() -> tuple:
    """Возвращает (rates: dict, timestamp: datetime). Формат: 1 USD = X currency"""
    cached, cached_time = load_cached_rates()
    if cached:
        return cached, cached_time
    
    if FIXER_API_KEY == "YOUR_API_KEY_HERE":
        rates = get_fallback_rates()
        save_rates_to_cache({k: float(v) for k, v in rates.items()})
        return rates, datetime.now()
    
    try:
        url = f"https://data.fixer.io/api/latest?access_key={FIXER_API_KEY}&base=EUR"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('success'):
            eur_rates = data['rates']
            usd_in_eur = eur_rates.get('USD', 1.0)
            
            usd_rates = {}
            for currency, rate in eur_rates.items():
                if currency == 'USD':
                    continue
                usd_rates[currency] = Decimal(str(rate)) / Decimal(str(usd_in_eur))  # 1 USD = rate/usd_in_eur foreign
            
            save_rates_to_cache({k: float(v) for k, v in usd_rates.items()})
            return usd_rates, datetime.now()
    except Exception as e:
        print(f"⚠️  Fixer.io failed: {e}")
    
    rates = get_fallback_rates()
    return rates, datetime.now()

def log_currency_error(url: str, detected_currency: str, error_type: str, raw_price: Decimal):
    """Логирует ошибку валюты в БД."""
    domain = urlparse(url).netloc.lower()
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO currency_errors (url, domain, detected_currency, error_type, raw_price)
                VALUES (:url, :domain, :currency, :error, :price)
            """), {
                'url': url, 'domain': domain, 'currency': detected_currency or 'UNKNOWN',
                'error': error_type, 'price': raw_price
            })
            conn.commit()
    except Exception as e:
        print(f"⚠️  Failed to log currency error: {e}")

def convert_to_usd(price: Decimal, currency: str, rates: dict = None) -> tuple:
    """
    Конвертирует цену в USD.
    Возвращает (usd_price, exchange_rate, currency) или (None, None, currency) при ошибке.
    """
    if currency == 'USD':
        return price, Decimal('1.0'), currency
    
    if rates is None:
        rates, _ = get_exchange_rates()
    
    rate = rates.get(currency)
    if not rate:
        return None, None, currency
    
    usd_price = price / Decimal(str(rate))
    return usd_price, rate, currency

def normalize_price(price: Decimal, url: str, api_currency: str = None) -> tuple:
    """
    Нормализует цену. Возвращает (usd_price, original_currency, exchange_rate) или (None, currency, None) при ошибке.
    """
    if api_currency and api_currency.upper() in SUPPORTED_CURRENCIES:
        currency = api_currency.upper()
    else:
        currency = detect_currency(url)
    
    if currency is None:
        log_currency_error(url, 'UNKNOWN', 'unknown_currency', price)
        return None, 'UNKNOWN', None
    
    if currency not in SUPPORTED_CURRENCIES:
        log_currency_error(url, currency, 'unsupported_currency', price)
        return None, currency, None
    
    rates, _ = get_exchange_rates()
    usd_price, rate, _ = convert_to_usd(price, currency, rates)
    
    if usd_price is None:
        log_currency_error(url, currency, 'missing_rate', price)
        return None, currency, None
    
    return usd_price, currency, rate

if __name__ == "__main__":
    print("🧪 Currency Normalizer v2 Test\n")
    
    test_cases = [
        (Decimal('590000'), 'https://www.dope-factory.com/p', 'KRW', 'Dope Factory (KRW)'),
        (Decimal('430'), 'https://www.laferramenta.org/p', 'EUR', 'LaFerramenta (EUR)'),
        (Decimal('350'), 'https://slamcity.com/p', 'GBP', 'Slam City (GBP)'),
        (Decimal('150'), 'https://www.footlocker.com/p', 'USD', 'Foot Locker (USD)'),
        (Decimal('50000'), 'https://unknown-shop.com/p', None, 'Unknown (should reject)'),
    ]
    
    for price, url, api_cur, label in test_cases:
        usd, orig, rate = normalize_price(price, url, api_cur)
        if usd is None:
            print(f"{label}:")
            print(f"  ❌ REJECTED: {price} {api_cur} (orig: {orig})\n")
        else:
            print(f"{label}:")
            print(f"  {price} {api_cur} → ${usd:.2f} USD (rate: {rate}, orig: {orig})\n")
