"""
Batch importer для всех Shopify сайтов с rate limiting и логированием.
"""
import json
import time
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.adapters.shopify_adapter import ShopifyAdapter
from src.config import DATABASE_URL


def batch_import(batch_size: int = 10, delay: float = 2.0):
    """
    Импортирует Shopify сайты партиями с паузами.
    
    Args:
        batch_size: количество сайтов в одной партии
        delay: пауза между сайтами в секундах
    """
    # Загружаем список Shopify сайтов
    with open('shopify_sites.json', 'r') as f:
        sites = json.load(f)
    
    print(f"🚀 Starting batch import of {len(sites)} Shopify sites")
    print(f"   Batch size: {batch_size}")
    print(f"   Delay: {delay}s between sites\n")
    
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    
    results = []
    
    for i, site in enumerate(sites, 1):
        url = site['url']
        domain = urlparse(url).netloc
        store_name = domain.replace('www.', '').replace('.com', '').title()
        
        print(f"[{i}/{len(sites)}] 📦 {store_name}")
        print(f"   URL: {url}")
        
        try:
            db = Session()
            
            adapter = ShopifyAdapter(
                store_name=store_name,
                base_url=url
            )
            
            # Получаем товары (лимит 500 для скорости)
            products = adapter.fetch_products(limit=250)
            
            if not products:
                print("   ⚠️  No products found")
                results.append({
                    'store': store_name,
                    'status': 'empty',
                    'products': 0
                })
                db.close()
                continue
            
            # Импортируем
            adapter.import_products(db, products)
            
            results.append({
                'store': store_name,
                'status': 'success',
                'products': len(products)
            })
            
            print(f"   ✅ Imported {len(products)} products")
            
            db.close()
            
        except Exception as e:
            print(f"   ❌ Error: {str(e)[:100]}")
            results.append({
                'store': store_name,
                'status': 'error',
                'products': 0,
                'error': str(e)[:100]
            })
        
        # Пауза между сайтами
        if i < len(sites):
            time.sleep(delay)
        
        # Прогресс каждые 10 сайтов
        if i % 10 == 0:
            success = sum(1 for r in results if r['status'] == 'success')
            total_products = sum(r['products'] for r in results)
            print(f"\n📊 Progress: {i}/{len(sites)} | Success: {success} | Products: {total_products}\n")
    
    # Финальная статистика
    print("\n" + "="*80)
    print("📊 FINAL RESULTS")
    print("="*80)
    
    success = [r for r in results if r['status'] == 'success']
    empty = [r for r in results if r['status'] == 'empty']
    errors = [r for r in results if r['status'] == 'error']
    
    print(f"✅ Success: {len(success)}")
    print(f"⚠️  Empty: {len(empty)}")
    print(f"❌ Errors: {len(errors)}")
    print(f"📦 Total products: {sum(r['products'] for r in success)}")
    
    # Сохраняем результаты
    with open('batch_import_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n✅ Results saved to batch_import_results.json")
    
    if errors:
        print("\n⚠️  Failed stores:")
        for r in errors[:10]:
            print(f"   {r['store']}: {r['error'][:50]}")

if __name__ == "__main__":
    batch_import(batch_size=10, delay=2.0)
