import logging
logger = logging.getLogger(__name__)
"""
Magento Adapter: импорт товаров через REST API (Magento 2.x).
Поддерживает сайты с открытым API (без авторизации).
"""
from decimal import Decimal
from urllib.parse import urlparse

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config import DATABASE_URL
from src.models import (
    Brand,
    Offer,
    PriceChange,
    PriceHistory,
    Product,
    ProductVariant,
    Store,
)
from src.pricing import MAX_PRICE


class MagentoAdapter:
    def __init__(self, store_name: str, base_url: str):
        self.store_name = store_name
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/json'
        })
    
    def fetch_products(self, page_size: int = 100, max_pages: int = 10) -> list:
        """Получает товары через Magento REST API."""
        all_products = []
        
        for page in range(1, max_pages + 1):
            url = f"{self.base_url}/rest/V1/products"
            params = {
                'searchCriteria[pageSize]': page_size,
                'searchCriteria[currentPage]': page
            }
            
            try:
                response = self.session.get(url, params=params, timeout=30)
                
                if response.status_code != 200:
                    print(f"   ⚠️  API returned {response.status_code}")
                    break
                
                data = response.json()
                items = data.get('items', [])
                
                if not items:
                    break
                
                all_products.extend(items)
                print(f"   Page {page}: {len(items)} products")
                
                # Проверяем, есть ли ещё страницы
                total_count = data.get('total_count', 0)
                if len(all_products) >= total_count:
                    break
                
            except Exception as e:
                print(f"   ❌ Error on page {page}: {e}")
                break
        
        print(f"   Total fetched: {len(all_products)} products")
        return all_products
    
    def normalize_brand_name(self, brand_name: str) -> str:
        return (brand_name or "unknown").lower().strip()
    
    def get_or_create_brand(self, db: Session, brand_name: str) -> Brand:
        normalized = self.normalize_brand_name(brand_name)
        brand = db.query(Brand).filter(Brand.normalized_name == normalized).first()
        if not brand:
            brand = Brand(name=brand_name or "Unknown", normalized_name=normalized)
            db.add(brand)
            db.flush()
        return brand
    
    def get_or_create_store(self, db: Session) -> Store:
        domain = urlparse(self.base_url).netloc
        store = db.query(Store).filter(Store.domain == domain).first()
        if not store:
            store = Store(name=self.store_name, domain=domain, currency="USD", region="US")
            db.add(store)
            db.flush()
        return store
    
    def is_sane_price(self, price: Decimal) -> bool:
        return price is not None and Decimal(0) < price <= Decimal(str(MAX_PRICE))
    
    def import_products(self, db: Session, products: list):
        """Импортирует товары Magento в БД."""
        store = self.get_or_create_store(db)
        imported = updated = skipped = 0
        
        for product_data in products:
            try:
                sku = product_data.get('sku', '')
                name = product_data.get('name', 'Unknown')
                
                # Magento не всегда даёт бренд отдельно — извлекаем из имени
                brand_name = product_data.get('custom_attributes', {})
                if isinstance(brand_name, list):
                    for attr in brand_name:
                        if attr.get('attribute_code') == 'brand':
                            brand_name = attr.get('value', '')
                            break
                else:
                    brand_name = ''
                
                if not brand_name:
                    # Берём первое слово из названия
                    brand_name = name.split()[0] if name else 'Unknown'
                
                brand = self.get_or_create_brand(db, brand_name)
                
                # Ищем или создаём продукт
                product = db.query(Product).filter(
                    Product.brand_id == brand.id,
                    Product.canonical_name == name
                ).first()
                
                if not product:
                    product = Product(
                        brand_id=brand.id,
                        canonical_name=name,
                        category=product_data.get('type_id', '')
                    )
                    db.add(product)
                    db.flush()
                    imported += 1
                else:
                    updated += 1
                
                # Создаём вариант (Magento использует SKU как уникальный идентификатор)
                variant = db.query(ProductVariant).filter(
                    ProductVariant.sku == sku,
                    ProductVariant.product_id == product.id,
                    ProductVariant.store_id == store.id
                ).first()
                
                if not variant:
                    variant = ProductVariant(
                        product_id=product.id,
                        store_id=store.id,
                        sku=sku,
                        ean=None,
                        size='',
                        color='',
                        attributes={}
                    )
                    db.add(variant)
                    db.flush()
                
                # Извлекаем цену
                price_str = product_data.get('price')
                if not price_str:
                    skipped += 1
                    continue
                
                regular_price = Decimal(str(price_str))
                
                # P0-7: Correct special_price interpretation
                # special_price = скидочная цена (current), price = обычная цена (old)
                current_price = regular_price
                old_price = None
                
                special_price = product_data.get('special_price')
                if special_price:
                    special_decimal = Decimal(str(special_price))
                    # Проверяем, что special_price меньше regular_price
                    if special_decimal < regular_price:
                        # Проверяем даты действия special_price (если есть)
                        from datetime import datetime, timezone
                        special_from = product_data.get('special_from_date')
                        special_to = product_data.get('special_to_date')
                        
                        now = datetime.now(timezone.utc)
                        is_active = True
                        
                        if special_from:
                            try:
                                from_date = datetime.fromisoformat(special_from.replace('Z', '+00:00'))
                                if now < from_date:
                                    is_active = False
                            except:
                                pass
                        
                        if special_to and is_active:
                            try:
                                to_date = datetime.fromisoformat(special_to.replace('Z', '+00:00'))
                                if now > to_date:
                                    is_active = False
                            except:
                                pass
                        
                        if is_active:
                            current_price = special_decimal
                            old_price = regular_price
                
                if not self.is_sane_price(current_price):
                    skipped += 1
                    continue
                
                # Upsert для оффера
                existing_offer = db.query(Offer).filter(
                    Offer.store_id == store.id,
                    Offer.variant_id == variant.id
                ).first()
                
                product_url = f"{self.base_url}/catalog/product/view/id/{product_data.get('id', '')}"
                
                if existing_offer:
                    existing_offer.current_price = current_price
                    existing_offer.old_price = old_price
                    existing_offer.in_stock = product_data.get('status', 1) == 1
                    existing_offer.url = product_url
                else:
                    db.add(Offer(
                        store_id=store.id,
                        variant_id=variant.id,
                        url=product_url,
                        current_price=current_price,
                        old_price=old_price,
                        in_stock=product_data.get('status', 1) == 1
                    ))
                
                # История цен
                db.add(PriceHistory(
                    variant_id=variant.id,
                    store_id=store.id,
                    timestamp=datetime.now(timezone.utc),
                    price=current_price,
                    old_price=old_price
                ))
                
                # P1-24/25/26: PriceChange с контекстом
                now_utc = datetime.now(timezone.utc)
                open_interval = db.query(PriceChange).filter(
                    PriceChange.variant_id == variant.id,
                    PriceChange.store_id == store.id,
                    PriceChange.ended_at.is_(None)
                ).first()
                if open_interval is None or open_interval.price != current_price:
                    if open_interval:
                        open_interval.ended_at = now_utc
                    db.add(PriceChange(
                        variant_id=variant.id, store_id=store.id,
                        started_at=now_utc,
                        price=current_price, old_price=old_price,
                        normalized_size=variant.normalized_size,
                        in_stock=product_data.get('status', 1) == 1,
                        region=store.region
                    ))
                
            except Exception as e:
                print(f"   Error: {e}")
                continue
        
        db.commit()
        print(f"   ✅ Imported: {imported}, Updated: {updated}, Skipped: {skipped}")

if __name__ == "__main__":
    # Тест на Foot Locker
    engine = create_engine(DATABASE_URL)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    db = Session()
    
    try:
        adapter = MagentoAdapter("Foot Locker", "https://www.footlocker.com")
        products = adapter.fetch_products(page_size=50, max_pages=2)
        adapter.import_products(db, products)
    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
    finally:
        db.close()

    
    def _detect_currency(self):
        from src.currency_normalizer import detect_currency
        return detect_currency(self.base_url) or None
    
    def _detect_region(self):
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc.lower()
        tld_map = {'.uk':'GB','.de':'DE','.fr':'FR','.it':'IT','.es':'ES','.jp':'JP'}
        for tld, reg in tld_map.items():
            if domain.endswith(tld): return reg
        return 'US'
