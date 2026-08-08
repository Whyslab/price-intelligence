import re
from datetime import datetime, timezone
from decimal import Decimal

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config import DATABASE_URL
from src.currency_normalizer import detect_currency, normalize_price
from src.data_provenance import save_raw_snapshot
from src.models import (
    Brand,
    Offer,
    PriceChange,
    Product,
    ProductVariant,
    Store,
)
from src.pricing import MAX_PRICE


class ShopifyAdapter:
    def _detect_region(self):
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc.lower()
        tld_map = {'.uk':'GB','.de':'DE','.fr':'FR','.it':'IT','.jp':'JP','.cn':'CN'}
        for tld, reg in tld_map.items():
            if domain.endswith(tld): return reg
        return None  # Unknown TLD

    def __init__(self, store_name: str, base_url: str):
        self.store_name = store_name
        self.base_url = base_url.rstrip('/')
        self.products_url = f"{self.base_url}/products.json"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        })
        # Определяем валюту магазина ОДИН РАЗ (v2: None если неизвестна)
        self.store_currency = detect_currency(self.base_url)
        if not self.store_currency:
            print(f"⚠️ Unknown currency: {self.base_url}")
            self.store_currency = None
    
    def fetch_products(self, limit: int = 250, max_pages: int = 50, db: Session = None, store_id: int = None) -> tuple:
        """
        P0-8: Cursor-based pagination через Link header.
        P0-69: Возвращает (products, snapshot_id) для provenance.
        
        Returns:
            tuple: (list of products, snapshot_id or None)
        """
        all_products = []
        all_responses = []  # Собираем raw responses для snapshot
        url = f"{self.products_url}?limit={limit}"
        page_count = 0
        
        while url and page_count < max_pages:
            try:
                response = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                print(f"   ⚠️  Request failed at page {page_count}: {e}")
                break
            
            if response.status_code != 200:
                print(f"   ⚠️  HTTP {response.status_code} at page {page_count}")
                break
            
            try:
                data = response.json()
            except ValueError:
                print(f"   ⚠️  Invalid JSON at page {page_count}")
                break
            
            # P0-69: сохраняем raw response
            all_responses.append({
                'url': url,
                'status': response.status_code,
                'headers': dict(response.headers),
                'data': data
            })
            
            products = data.get('products', [])
            if not products:
                break
            
            all_products.extend(products)
            page_count += 1
            
            # Parse Link header для cursor-based pagination
            link_header = response.headers.get('Link', '')
            next_url = None
            for part in link_header.split(','):
                if 'rel="next"' in part:
                    match = re.search(r'<([^>]+)>', part.strip())
                    if match:
                        next_url = match.group(1)
                        break
            
            if not next_url:
                break
            url = next_url
        
        # P0-69: сохраняем aggregated snapshot в БД
        snapshot_id = None
        if db and store_id and all_responses:
            try:
                aggregated_payload = {
                    'pages': all_responses,
                    'total_products': len(all_products),
                    'fetched_at': datetime.now(timezone.utc).isoformat()
                }
                snapshot_id = save_raw_snapshot(
                    db=db,
                    store_id=store_id,
                    adapter_name='shopify',
                    url=self.products_url,
                    http_status=all_responses[-1]['status'],
                    payload=aggregated_payload,
                    response_headers=all_responses[-1]['headers'],
                    pipeline_run_id=None  # будет добавлено через env var
                )
            except Exception as e:
                print(f"   ⚠️  Failed to save raw snapshot: {e}")
        
        return all_products, snapshot_id
    
    def normalize_brand_name(self, brand_name: str) -> str:
        return (brand_name or "unknown").lower().strip()
    
    def get_or_create_brand(self, db: Session, brand_name: str) -> Brand:
        """P0-12: Использует with_for_update() для предотвращения race conditions."""
        normalized = self.normalize_brand_name(brand_name)
        brand = db.query(Brand).filter(Brand.normalized_name == normalized).with_for_update().first()
        if not brand:
            brand = Brand(name=brand_name or "Unknown", normalized_name=normalized)
            db.add(brand)
            db.flush()
        return brand
    
    def get_or_create_store(self, db: Session) -> Store:
        """P0-12: Использует with_for_update() для предотвращения race conditions."""
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc
        store = db.query(Store).filter(Store.domain == domain).with_for_update().first()
        if not store:
            store = Store(name=self.store_name, domain=domain, 
                         currency=self.store_currency, region=self._detect_region())
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
        return Decimal('0.25') <= ratio <= Decimal(4)
    
    def get_or_create_variant(self, db: Session, product: Product, variant_data: dict) -> ProductVariant:
        """P0-10/P0-12: Использует external_variant_id и with_for_update() для предотвращения race conditions."""
        sku = variant_data.get('sku', '')
        external_variant_id = str(variant_data.get('id', ''))
        external_product_id = str(variant_data.get('product_id', ''))
        
        variant = None
        
        # 1. Сначала ищем по external_variant_id + store_id (scoped identity)
        store = self.get_or_create_store(db)
        if external_variant_id:
            variant = db.query(ProductVariant).filter(
                ProductVariant.external_variant_id == external_variant_id,
                ProductVariant.store_id == store.id  # Additional safety
            ).with_for_update().first()
        
        # 2. Если не найден, ищем по SKU (в рамках продукта)
        if not variant and sku:
            variant = db.query(ProductVariant).filter(
                ProductVariant.sku == sku,
                ProductVariant.product_id == product.id
            ).with_for_update().first()
        
        # 3. Если все еще не найден, создаем новый
        if not variant:
            variant = ProductVariant(
                product_id=product.id,
                sku=sku,
                ean=variant_data.get('barcode'),
                external_product_id=external_product_id,
                external_variant_id=external_variant_id,
                size=variant_data.get('option1', ''),
                color=variant_data.get('option2', ''),
                attributes={'option3': variant_data.get('option3')}
            )
            db.add(variant)
            db.flush()
        
        return variant
    
    def import_products(self, db: Session, products: list, snapshot_id: int = None):
        """P0-69: принимает snapshot_id для provenance."""
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
                    old_price_str = variant_data.get('compare_at_price')
                    
                    product_url = f"{self.base_url}/products/{product_data.get('handle', '')}"
                    
                    # v2: возвращает (usd_price, currency, rate) или (None, currency, None)
                    usd_price, original_currency, exchange_rate = normalize_price(price, product_url, api_currency)
                    
                    if usd_price is None:
                        # Reject: неизвестная валюта или нет курса
                        currency_rejected += 1
                        continue
                    
                    # P0-6: Конвертируем compare_at_price в USD (а не сохраняем в исходной валюте)
                    old_price = None
                    if old_price_str:
                        usd_old, _, _ = normalize_price(Decimal(old_price_str), product_url, api_currency)
                        old_price = usd_old
                    
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
                        existing_offer.parser_version = '1.0'
                        existing_offer.raw_snapshot_id = snapshot_id
                        existing_offer.exchange_rate_source = 'fixer_io' if original_currency != 'USD' else None
                        existing_offer.currency_source = 'api' if original_currency else None
                    else:
                        db.add(Offer(
                            store_id=store.id, variant_id=variant.id,
                            url=product_url,
                            current_price=usd_price, old_price=old_price,
                            in_stock=variant_data.get('available', True),
                            original_currency=original_currency,
                            exchange_rate=exchange_rate,
                            exchange_rate_timestamp=now_utc,
                            parser_version='1.0',
                            raw_snapshot_id=snapshot_id,
                            exchange_rate_source='fixer_io' if original_currency != 'USD' else None,
                            currency_source='api'
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
                            exchange_rate=exchange_rate,
                            parser_version='1.0',
                            raw_snapshot_id=snapshot_id,
                            exchange_rate_source='fixer_io' if original_currency != 'USD' else None
                        ,
                            normalized_size=variant.normalized_size,
                            in_stock=variant_data.get('available', True),
                            region=store.region))
            except Exception:
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
        store = adapter.get_or_create_store(db)
        products, snapshot_id = adapter.fetch_products(limit=50, db=db, store_id=store.id)
        adapter.import_products(db, products, snapshot_id=snapshot_id)
    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
    finally:
        db.close()

    

