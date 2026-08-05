"""Проверяем papinistore.com — реальный ли это Magento API."""
import requests, json

url = "https://www.papinistore.com/en/rest/V1/products"
params = {'searchCriteria[pageSize]': 2, 'searchCriteria[currentPage]': 1}
headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}

print(f"🔍 {url}")
r = requests.get(url, params=params, headers=headers, timeout=20)
print(f"Status: {r.status_code} | Content-Type: {r.headers.get('Content-Type')}")

if r.headers.get('Content-Type','').startswith('application/json'):
    try:
        data = r.json()
        print(f"✅ JSON! Items: {len(data.get('items', []))}")
        if data.get('items'):
            item = data['items'][0]
            print(f"First item keys: {list(item.keys())[:10]}")
            print(f"SKU: {item.get('sku')} | Price: {item.get('price')} | Name: {item.get('name')}")
    except Exception as e:
        print(f"❌ JSON parse error: {e}")
else:
    print(f"❌ Not JSON, first 500 chars: {r.text[:500]}")
