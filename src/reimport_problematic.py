"""Перезапуск импорта для магазинов с мусорными ценами"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.config import DATABASE_URL
from src.adapters.shopify_adapter import ShopifyAdapter

PROBLEMATIC_STORES = [
    ("Dope-Factory", "https://www.dope-factory.com"),
    ("It.Oneblockdown.It", "https://it.oneblockdown.it"),
]

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

for store_name, url in PROBLEMATIC_STORES:
    print(f"\n🔄 Reimporting {store_name}...")
    db = Session()
    try:
        adapter = ShopifyAdapter(store_name, url)
        products = adapter.fetch_products(limit=500)
        adapter.import_products(db, products)
    except Exception as e:
        db.rollback()
        print(f"   ❌ Error: {e}")
    finally:
        db.close()

print("\n✅ Reimport complete")
