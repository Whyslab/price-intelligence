"""
Скрипт для импорта товаров со всех магазинов.
Запуск: python -m src.import_all_stores
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.adapters.shopify_adapter import ShopifyAdapter
from src.config import DATABASE_URL

# Список магазинов для импорта
STORES = [
    {"name": "A Ma Maniere", "url": "https://www.a-ma-maniere.com"},
    {"name": "Social Status", "url": "https://www.socialstatuspgh.com"},
    {"name": "APB Store", "url": "https://www.apbstore.com"},
    {"name": "CNCPTS", "url": "https://cncpts.com"},
    {"name": "Feature", "url": "https://feature.com"},
    {"name": "Likelihood", "url": "https://www.likelihood.us"},
]

def import_all_stores():
    """Импортирует товары со всех магазинов."""
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    db = Session()
    
    print(f"🚀 Starting import of {len(STORES)} stores...")
    print("=" * 60)
    
    results = []
    
    for store_info in STORES:
        store_name = store_info["name"]
        store_url = store_info["url"]
        
        print(f"\n📦 [{store_name}] Starting import...")
        print(f"   URL: {store_url}")
        
        try:
            adapter = ShopifyAdapter(store_name=store_name, base_url=store_url)
            
            # Получаем товары (лимит 1000 для скорости на MVP)
            products = adapter.fetch_products(limit=250)
            
            # Импортируем в БД
            adapter.import_products(db, products)
            
            results.append({
                "store": store_name,
                "status": "✅ Success",
                "products": len(products)
            })
            
        except Exception as e:
            print(f"❌ Error importing {store_name}: {e}")
            results.append({
                "store": store_name,
                "status": f"❌ Failed: {str(e)[:50]}",
                "products": 0
            })
            db.rollback()
    
    # Итоговая статистика
    print("\n" + "=" * 60)
    print("📊 Import Summary:")
    print("=" * 60)
    
    total_products = 0
    for result in results:
        print(f"  {result['store']}: {result['status']}")
        total_products += result['products']
    
    print(f"\n✅ Total products imported: {total_products}")
    
    db.close()

if __name__ == "__main__":
    import_all_stores()
