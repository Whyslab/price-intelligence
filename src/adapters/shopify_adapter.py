import requests
from sqlalchemy.orm import Session
from src.models import Brand, Store, Product, ProductVariant, Offer, PriceHistory, PriceChange
from src.config import DATABASE_URL
from src.pricing import MAX_PRICE
from src.currency_normalizer import normalize_price, detect_currency
from sqlalchemy import create_engine
from datetime import datetime, timezone
from decimal import Decimal

class ShopifyAdapter:
    def __init__(self, store_name: str, base_url: str):
        self.store_name = store_name
        self.base_url = base_url.rstrip('/')
        self.products_url = f"{self.base_url}/products.json"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        })
        # Определяем валюту магазина ОДИН РАЗ (v2: None если неизвестна)
        self.store_currency = detect_currency(self.base_url) or 'USD'
    
    def fetch_products(self, limit: int = 250, max_pages: int = 5) -> list:
        all_products, page = [], 1
        while page <= max_pages:
            url = f"{self.products_url}?limit={limit}&page={page}"
            response = self.session.get(url, timeout=30)
            if response.status_code != 200:
                break
            products = response.json().get('products', [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < limit:
                break
            page += 1
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
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc
        store = db.query(Store).filter(Store.domain == domain).first()
        if not store:
            store = Store(name=self.store_name, domain=domain, 
                         currency=self.store_currency, region="US")
            db.add(store)
            db.flush()
        return store
    
    def extract_brand_name(self, product_data: dict) -> str:
        return product_data.get('vendor', '') or (product_data.get('title', '').split() or ['Unknown'])[0]
    
    def is_sane_price(self, price: Decimal) -> bool:
        return price is not None and Decimal(0) < price <= Decimal(str(MAX_PRICE))
    
    def is_plausible_change(self, old: Decimal, new: Decimal) -> bool:
        if old is None or old <= 0:
            return True
        ratio = new / old
        return Decimal('0.25') <= ratio <= Decimal('4')
    
    def get_or_create_variant(self, db: Session, product: Product, variant_data: dict) -> ProductVariant:
        sku = variant_data.get('sku', '')
        variant = None
        if sku:
            variant = db.query(ProductVariant).filter(
                ProductVariant.sku == sku,
                ProductVariant.product_id == product.id
            ).first()
        if not variant:
            variant = ProductVariant(
                product_id=product.id, sku=sku, ean=None,
                size=variant_data.get('option1', ''),
                color=variant_data.get('option2', ''),
                attributes={'option3': variant_data.get('option3')}
            )
            db.add(variant)
            db.flush()
        return variant
    
    def import_products(self, db: Session, products: list):
        store = self.get_or_create_store(db)
        imported = updated = skipped = currency_converted = currency_rejected = 0
        
        for product_data in products:
            try:
                brand = self.get_or_create_brand(db, self.extract_brand_name(product_data))
                canonical_name = product_data.get('title', 'Unknown')
                
                product = db.query(Product).filter(
                    Product.brand_id == brand.id,
                    Product.canonical_name == canonical_name
                ).first()
                if not product:
                    product = Product(brand_id=brand.id, canonical_name=canonical_name,
                                      category=product_data.get('product_type', ''))
                    db.add(product)
                    db.flush()
                    imported += 1
                else:
                    updated += 1
                
                # Используем валюту магазина (определена один раз)
                api_currency = product_data.get('currency') or self.store_currency
                
                for variant_data in product_data.get('variants', []):
                    variant = self.get_or_create_variant(db, product, variant_data)
                    
                    price_str = variant_data.get('price')
                    if not price_str:
                        continue
                    price = Decimal(price_str)
                    old_price = Decimal(variant_data['compare_at_price']) if variant_data.get('compare_at_price') else None
                    
                    product_url = f"{self.base_url}/products/{product_data.get('handle', '')}"
                    
                    # v2: возвращает (usd_price, currency, rate) или (None, currency, None)
                    usd_price, original_currency, exchange_rate = normalize_price(price, product_url, api_currency)
                    
                    if usd_price is None:
                        # Reject: неизвестная валюта или нет курса
                        currency_rejected += 1
                        continue
                    
                    if original_currency != 'USD':
                        currency_converted += 1
                    
                    if not self.is_sane_price(usd_price):
                        skipped += 1
                        continue
                    
                    existing_offer = db.query(Offer).filter(
                        Offer.store_id == store.id,
                        Offer.variant_id == variant.id
                    ).first()
                    
                    if existing_offer and existing_offer.current_price:
                        if not self.is_plausible_change(existing_offer.current_price, usd_price):
                            skipped += 1
                            continue
                    
                    now_utc = datetime.now(timezone.utc)
                    
                    if existing_offer:
                        existing_offer.current_price = usd_price
                        existing_offer.old_price = old_price
                        existing_offer.in_stock = variant_data.get('available', True)
                        existing_offer.url = product_url
                        existing_offer.original_currency = original_currency
                        existing_offer.exchange_rate = exchange_rate
                        existing_offer.exchange_rate_timestamp = now_utc
                    else:
                        db.add(Offer(
                            store_id=store.id, variant_id=variant.id,
                            url=product_url,
                            current_price=usd_price, old_price=old_price,
                            in_stock=variant_data.get('available', True),
                            original_currency=original_currency,
                            exchange_rate=exchange_rate,
                            exchange_rate_timestamp=now_utc
                        ))
                    
                    open_interval = db.query(PriceChange).filter(
                        PriceChange.variant_id == variant.id,
                        PriceChange.store_id == store.id,
                        PriceChange.ended_at.is_(None)
                    ).first()
                    if open_interval is None or open_interval.price != usd_price:
                        if open_interval:
                            open_interval.ended_at = now_utc
                        db.add(PriceChange(
                            variant_id=variant.id, store_id=store.id,
                            started_at=now_utc,
                            price=usd_price, old_price=old_price,
                            original_currency=original_currency,
                            exchange_rate=exchange_rate
                        ))
            except Exception as e:
                continue
        
        db.commit()
        print(f"✅ Imported: {imported}, Updated: {updated}, Skipped: {skipped}, Currency converted: {currency_converted}, Currency rejected: {currency_rejected}")
        if self.store_currency != 'USD':
            print(f"   💱 Store currency: {self.store_currency}")

if __name__ == "__main__":
    engine = create_engine(DATABASE_URL)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        adapter = ShopifyAdapter("A Ma Maniere", "https://www.a-ma-maniere.com")
        products = adapter.fetch_products(limit=50)
        adapter.import_products(db, products)
    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
    finally:
        db.close()
