"""
Диагностика: смотрим сырой ответ от Magento API.
"""
import json

import requests

url = "https://www.footlocker.com/rest/V1/products"
params = {
    'searchCriteria[pageSize]': 10,
    'searchCriteria[currentPage]': 1
}

headers = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'Accept': 'application/json'
}

print(f"🔍 Fetching: {url}")
print(f"   Params: {params}\n")

response = requests.get(url, params=params, headers=headers, timeout=30)

print(f"Status: {response.status_code if response else 'N/A'}")
print(f"Headers: {dict(response.headers)}\n")
print(f"Content-Type: {response.headers.get('Content-Type')}\n")

print("Raw response (first 2000 chars):")
print("-" * 80)
print(response.text[:2000])
print("-" * 80)

# Пробуем распарсить как JSON
try:
    data = response.json()
    print("\n✅ Parsed as JSON!")
    print(f"Keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
    if 'items' in data:
        print(f"Items count: {len(data['items'])}")
        if data['items']:
            print(f"\nFirst item keys: {list(data['items'][0].keys())}")
            print("\nFirst item (truncated):")
            print(json.dumps(data['items'][0], indent=2)[:1000])
except Exception as e:
    print(f"\n❌ Not JSON: {e}")
    
    # Проверяем, может это HTML
    if '<html' in response.text.lower():
        print("   Это HTML страница, не API")
        
        # Ищем признаки Magento
        if 'magento' in response.text.lower():
            print("   ✅ Magento detected в HTML")
        if 'REST API' in response.text or 'api' in response.text.lower():
            print("   ⚠️  Возможно, API отключен или требует авторизации")
