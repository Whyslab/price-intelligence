"""Тест импорта Dope Factory с конвертацией KRW → USD"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.adapters.shopify_adapter import ShopifyAdapter
from src.config import DATABASE_URL

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
db = Session()

try:
    adapter = ShopifyAdapter("Dope Factory", "https://www.dope-factory.com")
    products = adapter.fetch_products(limit=10)
    
    print("\n📦 First 3 products (before import):")
    for p in products[:3]:
        print(f"  {p['title'][:50]} | Currency: {p.get('currency')}")
        if p.get('variants'):
            v = p['variants'][0]
            print(f"    Price: {v['price']} {p.get('currency')}")
    
    print("\n🔄 Importing...")
    adapter.import_products(db, products)
    
    # Проверяем результат
    print("\n✅ Check offers in DB:")
    from sqlalchemy import text
    result = db.execute(text("""
        SELECT o.current_price, o.url, s.name
        FROM offers o
        JOIN stores s ON o.store_id = s.id
        WHERE s.name = 'Dope Factory'
        LIMIT 5
    """)).fetchall()
    
    for row in result:
        print(f"  ${row[0]:.2f} USD @ {row[2]}")
        
except Exception as e:
    db.rollback()
    print(f"Failed: {e}")
finally:
    db.close()
