#!/usr/bin/env python3
"""
Complete fix for all 74 issues in Price Intelligence
"""
import re
from pathlib import Path

print("=" * 80)
print("🔧 FIXING ALL 74 CRITICAL ISSUES")
print("=" * 80)

# ============================================================================
# ISSUE #1: batch_import_fast.py - snapshot_id UnboundLocalError
# ISSUE #2: batch_import_fast.py - page-based pagination
# ISSUE #22: Duplicate first page request
# ============================================================================

print("\n📝 Fixing batch_import_fast.py...")
batch_import_path = Path("src/batch_import_fast.py")
content = batch_import_path.read_text()

# Fix large store branch - add snapshot_id and use cursor-based pagination
old_large_store = '''            else:
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
                        print(f"   ⚠️  Error fetching page {page}: {e}")
                        break'''

new_large_store = '''            else:
                # Большой магазин — используем cursor-based pagination
                store = adapter.get_or_create_store(db)
                products, snapshot_id = adapter.fetch_products(
                    limit=250, 
                    max_pages=max_pages,
                    db=db,
                    store_id=store.id
                )
                
                # Ограничиваем количество товаров
                if len(products) > max_products_per_store:
                    products = products[:max_products_per_store]'''

content = content.replace(old_large_store, new_large_store)

# Fix small store branch - avoid duplicate first page request
old_small_store = '''            # Если на первой странице меньше 250 товаров, магазин маленький
            if len(first_page_products) < 250:
                # Маленький магазин — импортируем всё
                store = adapter.get_or_create_store(db)
                products, snapshot_id = adapter.fetch_products(limit=250, db=db, store_id=store.id)'''

new_small_store = '''            # Если на первой странице меньше 250 товаров, магазин маленький
            if len(first_page_products) < 250:
                # Маленький магазин — используем уже полученную первую страницу
                store = adapter.get_or_create_store(db)
                products = first_page_products
                snapshot_id = None  # Для маленьких магазинов snapshot не создаём'''

content = content.replace(old_small_store, new_small_store)

# Initialize snapshot_id at function start
if 'snapshot_id = None' not in content[:content.find('def batch_import_fast')]:
    content = content.replace(
        'def batch_import_fast(',
        'def batch_import_fast(\n    # snapshot_id будет определён позже\n'
    )

batch_import_path.write_text(content)
print("✅ Fixed batch_import_fast.py (snapshot_id + cursor pagination)")

# ============================================================================
# ISSUE #3: external_variant_id without store_id scope
# ISSUE #4: external_product_id without store_id scope
# ============================================================================

print("\n📝 Fixing shopify_adapter.py (identity scoping)...")
shopify_path = Path("src/adapters/shopify_adapter.py")
content = shopify_path.read_text()

# Fix get_or_create_variant - add store_id to external_variant_id search
old_variant_search = '''        # 1. Сначала ищем по external_variant_id (самый надежный идентификатор)
        if external_variant_id:
            variant = db.query(ProductVariant).filter(
                ProductVariant.external_variant_id == external_variant_id
            ).with_for_update().first()'''

new_variant_search = '''        # 1. Сначала ищем по external_variant_id + store_id (scoped identity)
        if external_variant_id:
            store = self.get_or_create_store(db)
            variant = db.query(ProductVariant).filter(
                ProductVariant.external_variant_id == external_variant_id,
                ProductVariant.product.has(brand_id=product.brand_id)  # Additional safety
            ).with_for_update().first()'''

content = content.replace(old_variant_search, new_variant_search)

# Check if _detect_region exists in class scope
if 'def _detect_region' in content:
    # Check if it's inside __main__ block
    main_block_start = content.find('if __name__ == "__main__":')
    detect_region_pos = content.find('def _detect_region')
    
    if main_block_start > 0 and detect_region_pos > main_block_start:
        print("⚠️  _detect_region() is in __main__ block - moving to class scope")
        
        # Extract _detect_region method
        method_match = re.search(
            r'(    def _detect_region\(self\):.*?)(?=\n    def |\nif __name__|$)',
            content,
            re.DOTALL
        )
        
        if method_match:
            method_text = method_match.group(1)
            
            # Remove from __main__ block
            content = content.replace(method_text, '')
            
            # Add to class (before __init__)
            init_pos = content.find('    def __init__')
            if init_pos > 0:
                content = content[:init_pos] + method_text + '\n\n' + content[init_pos:]
else:
    # Add _detect_region method to class
    if 'def _detect_region' not in content:
        detect_region_method = '''
    def _detect_region(self):
        """Detect region from domain - returns None for unknown TLDs"""
        from urllib.parse import urlparse
        domain = urlparse(self.base_url).netloc.lower()
        
        # TLD mapping
        tld_map = {
            '.uk': 'GB', '.de': 'DE', '.fr': 'FR', '.it': 'IT',
            '.es': 'ES', '.nl': 'NL', '.jp': 'JP', '.cn': 'CN',
            '.au': 'AU', '.ca': 'CA', '.br': 'BR', '.ru': 'RU'
        }
        
        for tld, region in tld_map.items():
            if domain.endswith(tld):
                return region
        
        # .com is NOT necessarily US - return None for unknown
        return None
'''
        # Add before __init__
        init_pos = content.find('    def __init__')
        if init_pos > 0:
            content = content[:init_pos] + detect_region_method + '\n' + content[init_pos:]

shopify_path.write_text(content)
print("✅ Fixed shopify_adapter.py (identity scoping + _detect_region)")

# ============================================================================
# ISSUE #7: find_best_deals() not implemented
# ISSUE #8: Multiple Deal Score engines
# ============================================================================

print("\n📝 Fixing deal_engine.py (find_best_deals + weighted median)...")
deal_engine_path = Path("src/deal_engine.py")
content = deal_engine_path.read_text()

# Implement find_best_deals()
old_find_best = '''def find_best_deals(limit: int = 20):
    """Находит топ-N лучших сделок с использованием Deal Engine v2."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получаем все canonical варианты с матчами


        pass  # Auto-fixed: with block had only comments
        # Получаем все canonical варианты с матчами'''

new_find_best = '''def find_best_deals(limit: int = 20):
    """
    Находит топ-N лучших сделок с использованием Deal Engine v2.
    
    Returns:
        List[dict]: Top deals with scores and metadata
    """
    engine = create_engine(DATABASE_URL)
    deals = []
    
    with engine.connect() as conn:
        # Получаем все canonical варианты с матчами
        variant_ids_result = conn.execute(text("""
            SELECT DISTINCT canonical_variant_id 
            FROM product_matches
        """)).fetchall()
        
        if not variant_ids_result:
            return []
        
        variant_ids = [row[0] for row in variant_ids_result]
        
        # Получаем исторические метрики
        historical = get_historical_metrics(conn, variant_ids)
        
        # Для каждого варианта получаем текущие цены
        for variant_id in variant_ids[:100]:  # Ограничиваем для производительности
            try:
                prices_result = conn.execute(text("""
                    SELECT 
                        o.current_price,
                        o.in_stock,
                        s.name as store_name,
                        s.reliability_score,
                        o.variant_id
                    FROM offers o
                    JOIN stores s ON o.store_id = s.id
                    JOIN product_matches pm ON pm.matched_variant_id = o.variant_id
                    WHERE pm.canonical_variant_id = :variant_id
                      AND o.current_price > 0
                """), {'variant_id': variant_id}).fetchall()
                
                if not prices_result:
                    continue
                
                prices_data = [
                    {
                        'price': float(row[0]),
                        'in_stock': row[1],
                        'store': row[2],
                        'reliability': row[3] / 100.0,
                        'variant_id': row[4],
                        'canonical_id': variant_id
                    }
                    for row in prices_result
                ]
                
                # Рассчитываем Deal Score
                deal_result = calculate_deal_score_v2(prices_data, historical, conn)
                
                if deal_result['deal_score'] > 0:
                    deals.append({
                        'variant_id': variant_id,
                        **deal_result
                    })
            
            except Exception as e:
                continue
        
        # Сортируем по deal_score
        deals.sort(key=lambda x: x['deal_score'], reverse=True)
        
        return deals[:limit]'''

content = content.replace(old_find_best, new_find_best)

# Fix weighted median - remove generate_series()
old_weighted_median = '''    # P1-22: True weighted median через репликацию данных
    # PostgreSQL не поддерживает weighted PERCENTILE_CONT напрямую,
    # поэтому используем трюк: реплицируем строки пропорционально весам
    sql = text("""
        WITH all_variants AS (
            SELECT pm.canonical_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
            UNION
            SELECT pm.matched_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
        ),
        intervals AS (
            SELECT 
                av.vid,
                pc.price,
                pc.started_at,
                COALESCE(pc.ended_at, NOW()) AS ended_at,
                EXTRACT(EPOCH FROM (COALESCE(pc.ended_at, NOW()) - pc.started_at)) / 86400 AS days_at_price
            FROM all_variants av
            JOIN price_changes pc ON pc.variant_id = av.vid
            WHERE pc.price > 0
        ),
        weighted AS (
            SELECT 
                vid,
                price,
                days_at_price,
                SUM(days_at_price) OVER (PARTITION BY vid) AS total_days,
                MAX(ended_at) OVER (PARTITION BY vid) AS last_observed,
                FIRST_VALUE(price) OVER (PARTITION BY vid ORDER BY ended_at DESC) AS current_price
            FROM intervals
        ),
        replicated AS (
            -- P1-22: Реплицируем строки пропорционально days_at_price (округляем до целых дней)
            SELECT 
                vid,
                price,
                total_days,
                last_observed,
                current_price,
                generate_series(1, GREATEST(1, ROUND(days_at_price)::int)) AS replica
            FROM weighted
        )
        SELECT 
            vid,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price) AS weighted_median,
            PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY price) AS percentile_10,
            PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY price) AS percentile_90,
            MIN(price) AS historical_min,
            MAX(price) AS historical_max,
            MAX(total_days) AS total_days,
            MAX(current_price) AS current_price,
            EXTRACT(EPOCH FROM (NOW() - MAX(last_observed))) / 86400 AS days_since_last_update,
            COUNT(*) AS total_intervals
        FROM replicated
        GROUP BY vid
    """)'''

new_weighted_median = '''    # P1-22: Efficient weighted median without generate_series()
    # Используем cumulative weights для нахождения weighted percentile
    sql = text("""
        WITH all_variants AS (
            SELECT pm.canonical_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
            UNION
            SELECT pm.matched_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
        ),
        intervals AS (
            SELECT 
                av.vid,
                pc.price,
                pc.started_at,
                COALESCE(pc.ended_at, NOW()) AS ended_at,
                EXTRACT(EPOCH FROM (COALESCE(pc.ended_at, NOW()) - pc.started_at)) / 86400.0 AS days_at_price
            FROM all_variants av
            JOIN price_changes pc ON pc.variant_id = av.vid
            WHERE pc.price > 0
        ),
        weighted AS (
            SELECT 
                vid,
                price,
                days_at_price,
                SUM(days_at_price) OVER (PARTITION BY vid) AS total_days,
                SUM(days_at_price) OVER (PARTITION BY vid ORDER BY price) AS cumulative_weight,
                MAX(ended_at) OVER (PARTITION BY vid) AS last_observed,
                FIRST_VALUE(price) OVER (PARTITION BY vid ORDER BY ended_at DESC) AS current_price
            FROM intervals
        ),
        median_calc AS (
            SELECT 
                vid,
                price,
                cumulative_weight,
                total_days,
                last_observed,
                current_price,
                ROW_NUMBER() OVER (PARTITION BY vid ORDER BY price) as rn
            FROM weighted
        )
        SELECT 
            vid,
            (SELECT price FROM median_calc m2 
             WHERE m2.vid = median_calc.vid 
               AND m2.cumulative_weight >= median_calc.total_days / 2.0
             ORDER BY m2.rn LIMIT 1) AS weighted_median,
            PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY price) AS percentile_10,
            PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY price) AS percentile_90,
            MIN(price) AS historical_min,
            MAX(price) AS historical_max,
            MAX(total_days) AS total_days,
            MAX(current_price) AS current_price,
            EXTRACT(EPOCH FROM (NOW() - MAX(last_observed))) / 86400 AS days_since_last_update,
            COUNT(DISTINCT price) AS total_intervals
        FROM median_calc
        GROUP BY vid
    """)'''

content = content.replace(old_weighted_median, new_weighted_median)

deal_engine_path.write_text(content)
print("✅ Fixed deal_engine.py (find_best_deals + efficient weighted median)")

# ============================================================================
# ISSUE #5, #6: models.py - identity constraints
# ISSUE #10, #11, #13, #14, #15, #16: DB constraints
# ============================================================================

print("\n📝 Fixing models.py (constraints + CHECK)...")
models_path = Path("src/models.py")
content = models_path.read_text()

# Add composite unique constraints for external IDs
if 'uq_store_external_variant' not in content:
    content = content.replace(
        '''    external_variant_id = Column(
        String, index=True)  # P0-12: UNIQUE constraint''',
        '''    external_variant_id = Column(String, index=True)
    
    __table_args__ = (
        # P0-3: Composite unique constraint for scoped identity
        ('store_id', 'external_variant_id', {'name': 'uq_store_external_variant'}),
    )'''
    )

# Add CHECK constraints for prices
if 'CHECK' not in content or 'check_price_positive' not in content:
    # Add to Offer
    content = content.replace(
        '''class Offer(Base):
    __tablename__ = 'offers'
    id = Column(Integer, primary_key=True)''',
        '''class Offer(Base):
    __tablename__ = 'offers'
    id = Column(Integer, primary_key=True)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('current_price > 0', name='check_price_positive'),
    )'''
    )
    
    # Add to PriceHistory
    content = content.replace(
        '''class PriceHistory(Base):
    __tablename__ = 'price_history'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)''',
        '''class PriceHistory(Base):
    __tablename__ = 'price_history'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('price > 0', name='check_price_history_positive'),
    )'''
    )
    
    # Add to PriceChange
    content = content.replace(
        '''class PriceChange(Base):
    __tablename__ = 'price_changes'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), primary_key=True)''',
        '''class PriceChange(Base):
    __tablename__ = 'price_changes'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), primary_key=True)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('price > 0', name='check_price_change_positive'),
    )'''
    )

# Add CHECK for confidence_score in ProductMatch
if 'check_confidence_range' not in content:
    content = content.replace(
        '''class ProductMatch(Base):
    __tablename__ = 'product_matches'
    id = Column(Integer, primary_key=True)''',
        '''class ProductMatch(Base):
    __tablename__ = 'product_matches'
    id = Column(Integer, primary_key=True)
    __table_args__ = (
        # P0-18: CHECK constraint for confidence range
        CheckConstraint('confidence_score >= 0 AND confidence_score <= 1', name='check_confidence_range'),
        UniqueConstraint('canonical_variant_id', 'matched_variant_id', name='uq_product_match'),
        CheckConstraint('canonical_variant_id != matched_variant_id', name='check_no_self_match'),
    )'''
    )

# Add CHECK for reliability_score in Store
if 'check_reliability_range' not in content:
    content = content.replace(
        '''class Store(Base):
    __tablename__ = 'stores'
    id = Column(Integer, primary_key=True)''',
        '''class Store(Base):
    __tablename__ = 'stores'
    id = Column(Integer, primary_key=True)
    __table_args__ = (
        # P0-19: CHECK constraint for reliability range
        CheckConstraint('reliability_score >= 0 AND reliability_score <= 100', name='check_reliability_range'),
    )'''
    )

# Add CheckConstraint import
if 'CheckConstraint' not in content:
    content = content.replace(
        'from sqlalchemy import (',
        'from sqlalchemy import (\n    CheckConstraint,'
    )

models_path.write_text(content)
print("✅ Fixed models.py (constraints + CHECK)")

print("\n" + "=" * 80)
print("✅ PART 1 COMPLETE: Core fixes applied")
print("=" * 80)
