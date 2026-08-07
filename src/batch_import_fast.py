"""
Быстрый batch importer с лимитами и умной стратегией.
"""
import json
import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.config import DATABASE_URL
from src.adapters.shopify_adapter import ShopifyAdapter
from src.models import Store
from urllib.parse import urlparse
from datetime import datetime, timezone
from sqlalchemy import text

def update_store_sync_metadata(db, domain: str, status: str, error: str = None, products_count: int = 0):
    """
    Обновляет sync metadata для магазина.
    
    Args:
        db: SQLAlchemy session
        domain: домен магазина
        status: 'success', 'error', 'empty'
        error: текст ошибки (для status='error')
        products_count: количество импортированных товаров
    """
    try:
        now = datetime.now(timezone.utc)
        
        # Найти магазин по домену
        store = db.query(Store).filter(Store.domain == domain).first()
        if not store:
            return
        
        store.last_sync = now
        store.sync_status = status
        store.last_error = error
        
        if status == 'success':
            store.last_successful_sync = now
            store.products_count = products_count
        
        db.commit()
    except Exception as e:
        print(f"   ⚠️  Failed to update sync metadata: {e}")
        db.rollback()



def batch_import_fast(
    max_products_per_store: int = 500,
    max_pages: int = 10,
    delay: float = 1.0,
    skip_large_stores: bool = False
):
    """
    Быстрый импорт с ограничениями.
    
    Args:
        max_products_per_store: максимум товаров с одного магазина
        max_pages: максимум страниц пагинации
        delay: пауза между сайтами
        skip_large_stores: пропускать магазины с >5000 товаров
    """
    with open('shopify_sites.json', 'r') as f:
        sites = json.load(f)
    
    print(f"🚀 Fast batch import of {len(sites)} Shopify sites")
    print(f"   Max products per store: {max_products_per_store}")
    print(f"   Max pages: {max_pages}")
    print(f"   Delay: {delay}s\n")
    
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    
    results = []
    
    for i, site in enumerate(sites, 1):
        url = site['url']
        domain = urlparse(url).netloc
        store_name = domain.replace('www.', '').replace('.com', '').title()
        
        print(f"[{i}/{len(sites)}] 📦 {store_name}")
        
        try:
            db = Session()
            
            adapter = ShopifyAdapter(
                store_name=store_name,
                base_url=url
            )
            
            # Быстрая проверка размера магазина (только первая страница)
            adapter.products_url = f"{adapter.base_url}/products.json"
            first_page = adapter.session.get(
                f"{adapter.products_url}?limit=250&page=1",
                timeout=10
            )
            
            if first_page.status_code != 200:
                print(f"   ⚠️  Cannot access products.json")
                update_store_sync_metadata(db, domain, 'error', 'Cannot access products.json')
                results.append({
                    'store': store_name,
                    'status': 'error',
                    'products': 0
                })
                db.close()
                continue
            
            first_page_data = first_page.json()
            first_page_products = first_page_data.get('products', [])
            
            # Если на первой странице меньше 250 товаров, магазин маленький
            if len(first_page_products) < 250:
                # Маленький магазин — импортируем всё
                products = adapter.fetch_products(limit=250)
            else:
                # Большой магазин — импортируем только первые N страниц
                products = []
                for page in range(1, max_pages + 1):
                    page_url = f"{adapter.products_url}?limit=250&page={page}"
                    try:
                        resp = adapter.session.get(page_url, timeout=10)
                        if resp.status_code != 200:
                            break
                        
                        page_data = resp.json()
                        page_products = page_data.get('products', [])
                        
                        if not page_products:
                            break
                        
                        products.extend(page_products)
                        
                        if len(products) >= max_products_per_store:
                            products = products[:max_products_per_store]
                            break
                        
                    except Exception as e:
                        print(f"   ⚠️  Page {page} error: {e}")
                        break
            
            if not products:
                print(f"   ⚠️  No products found")
                update_store_sync_metadata(db, domain, 'empty')
                results.append({
                    'store': store_name,
                    'status': 'empty',
                    'products': 0
                })
                db.close()
                continue
            
            # Импортируем
            adapter.import_products(db, products)
            
            # Обновляем sync metadata
            update_store_sync_metadata(db, domain, 'success', products_count=len(products))
            
            results.append({
                'store': store_name,
                'status': 'success',
                'products': len(products)
            })
            
            print(f"   ✅ {len(products)} products")
            
            db.close()
            
        except Exception as e:
            error_msg = str(e)[:200]
            print(f"   ❌ Error: {error_msg[:80]}")
            try:
                update_store_sync_metadata(db, domain, 'error', error_msg)
            except:
                pass
            results.append({
                'store': store_name,
                'status': 'error',
                'products': 0,
                'error': error_msg
            })
        
        # Пауза
        if i < len(sites):
            time.sleep(delay)
        
        # Прогресс
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
    
    with open('batch_import_fast_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\n✅ Results saved to batch_import_fast_results.json")

if __name__ == "__main__":
    batch_import_fast(
        max_products_per_store=500,  # Максимум 500 товаров с магазина
        max_pages=5,                  # Максимум 5 страниц (1250 товаров)
        delay=1.0,                    # 1 секунда между сайтами
        skip_large_stores=False
    )
