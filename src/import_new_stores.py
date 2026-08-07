"""Импортирует только новые Shopify магазины"""
import json
import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.config import DATABASE_URL
from src.adapters.shopify_adapter import ShopifyAdapter
from urllib.parse import urlparse

# Загружаем список новых магазинов
with open('new_shopify_stores.json', 'r') as f:
    new_stores = json.load(f)

print(f"🚀 Importing {len(new_stores)} new Shopify stores\n")

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

results = []

for i, site in enumerate(new_stores, 1):
    url = site['url']
    domain = urlparse(url).netloc
    store_name = domain.replace('www.', '').replace('.com', '').title()
    
    print(f"[{i}/{len(new_stores)}] 📦 {store_name}")
    
    try:
        db = Session()
        adapter = ShopifyAdapter(store_name=store_name, base_url=url)
        
        products = adapter.fetch_products(limit=250)
        
        if not products:
            print(f"   ⚠️  No products")
            results.append({'store': store_name, 'status': 'empty', 'products': 0})
            db.close()
            continue
        
        adapter.import_products(db, products)
        
        results.append({
            'store': store_name,
            'status': 'success',
            'products': len(products)
        })
        
        db.close()
        
    except Exception as e:
        print(f"   ❌ Error: {str(e)[:80]}")
        results.append({
            'store': store_name,
            'status': 'error',
            'products': 0,
            'error': str(e)[:80]
        })
    
    if i < len(new_stores):
        time.sleep(1.0)

# Статистика
print("\n" + "="*80)
print("📊 RESULTS")
print("="*80)

success = [r for r in results if r['status'] == 'success']
empty = [r for r in results if r['status'] == 'empty']
errors = [r for r in results if r['status'] == 'error']

print(f"✅ Success: {len(success)}")
print(f"⚠️  Empty: {len(empty)}")
print(f"❌ Errors: {len(errors)}")
print(f"📦 Total products: {sum(r['products'] for r in success)}")

with open('new_stores_import_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print(f"\n✅ Results saved to new_stores_import_results.json")
