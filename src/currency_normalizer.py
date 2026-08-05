"""
Currency Normalizer: конвертирует цены в USD.
Все курсы хранятся в формате: 1 USD = X currency
"""
import requests
from decimal import Decimal
from urllib.parse import urlparse
import json
from datetime import datetime, timedelta

FIXER_API_KEY = "YOUR_API_KEY_HERE"
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

def detect_currency(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    for store_domain, currency in STORE_CURRENCY_MAP.items():
        if store_domain in domain:
            return currency
    for suffix, currency in DOMAIN_CURRENCY_MAP.items():
        if domain.endswith(suffix):
            return currency
    return 'USD'

def load_cached_rates() -> dict:
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            cached_time = datetime.fromisoformat(cache['timestamp'])
            if datetime.now() - cached_time < timedelta(hours=CACHE_TTL_HOURS):
                return cache['rates']
    except:
        pass
    return None

def save_rates_to_cache(rates: dict):
    cache = {'timestamp': datetime.now().isoformat(), 'rates': rates}
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def get_fallback_rates() -> dict:
    """Курсы в формате: 1 USD = X currency"""
    return {
        'EUR': 0.92,   # 1 USD = 0.92 EUR
        'GBP': 0.79,   # 1 USD = 0.79 GBP
        'JPY': 149.5,  # 1 USD = 149.5 JPY
        'KRW': 1320.0, # 1 USD = 1320 KRW
        'CAD': 1.36,
        'AUD': 1.53,
        'CNY': 7.24,
        'CHF': 0.88,
        'SEK': 10.5,
        'NOK': 10.8,
        'DKK': 6.87,
        'PLN': 4.02,
        'CZK': 23.1,
        'HUF': 355.0,
        'BRL': 4.97,
        'MXN': 17.1,
        'INR': 83.2,
        'RUB': 92.5,
    }

def get_exchange_rates() -> dict:
    """Возвращает курсы в формате: 1 USD = X currency"""
    cached = load_cached_rates()
    if cached:
        return cached
    
    if FIXER_API_KEY == "YOUR_API_KEY_HERE":
        return get_fallback_rates()
    
    try:
        # Fixer.io бесплатный план: base=EUR
        url = f"http://data.fixer.io/api/latest?access_key={FIXER_API_KEY}&base=EUR"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('success'):
            eur_rates = data['rates']  # 1 EUR = X currency
            usd_in_eur = eur_rates.get('USD', 1.0)
            
            # Конвертируем в формат: 1 USD = X currency
            usd_rates = {}
            for currency, rate in eur_rates.items():
                if currency == 'USD':
                    continue
                # 1 EUR = rate currency, 1 USD = usd_in_eur EUR
                # Значит: 1 USD = usd_in_eur * rate currency
                usd_rates[currency] = usd_in_eur * rate
            
            save_rates_to_cache(usd_rates)
            return usd_rates
    except Exception as e:
        print(f"⚠️  Fixer.io failed: {e}")
    
    return get_fallback_rates()

def convert_to_usd(price: Decimal, currency: str, rates: dict = None) -> Decimal:
    """Конвертирует цену в USD. Курсы: 1 USD = X currency"""
    if currency == 'USD':
        return price
    
    if rates is None:
        rates = get_exchange_rates()
    
    rate = rates.get(currency)
    if not rate:
        print(f"⚠️  No rate for {currency}")
        return price
    
    # price в currency, rate = сколько currency в 1 USD
    # Значит: price_in_usd = price / rate
    return price / Decimal(str(rate))

def normalize_price(price: Decimal, url: str, api_currency: str = None) -> tuple:
    """Нормализует цену. Возвращает (цена в USD, оригинальная валюта)."""
    if api_currency and api_currency.upper() != 'USD':
        currency = api_currency.upper()
    else:
        currency = detect_currency(url)
    
    rates = get_exchange_rates()
    usd_price = convert_to_usd(price, currency, rates)
    
    return usd_price, currency

if __name__ == "__main__":
    print("🧪 Currency Normalizer Test\n")
    
    test_cases = [
        (Decimal('590000'), 'https://www.dope-factory.com/p', 'KRW', 'Dope Factory (KRW)'),
        (Decimal('430'), 'https://www.laferramenta.org/p', 'EUR', 'LaFerramenta (EUR)'),
        (Decimal('350'), 'https://slamcity.com/p', 'GBP', 'Slam City (GBP)'),
        (Decimal('150'), 'https://www.footlocker.com/p', 'USD', 'Foot Locker (USD)'),
        (Decimal('50000'), 'https://unknown-shop.com/p', 'JPY', 'Unknown (JPY via API)'),
    ]
    
    for price, url, api_cur, label in test_cases:
        usd, orig = normalize_price(price, url, api_cur)
        print(f"{label}:")
        print(f"  {price} {api_cur} → ${usd:.2f} USD (orig: {orig})\n")
