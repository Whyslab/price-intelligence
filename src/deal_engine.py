"""
Deal Engine: находит лучшие сделки по всей базе с учётом наличия и confidence.
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
import statistics

def calculate_enhanced_deal_score(prices_data: list) -> dict:
    """
    Улучшенный расчёт Deal Score с учётом stock availability и confidence.
    """
    # Фильтруем только товары в наличии
    in_stock_prices = [p for p in prices_data if p['in_stock']]
    
    if not in_stock_prices:
        return {
            'deal_score': 0,
            'confidence': 0,
            'classification': '⚫ OUT OF STOCK',
            'reason': 'No stock available in any store'
        }
    
    # Все цены (для расчёта рынка)
    all_prices = [p['price'] for p in prices_data]
    in_stock_values = [p['price'] for p in in_stock_prices]
    
    # Рыночные метрики (по всем ценам)
    market_median = statistics.median(all_prices)
    market_min = min(all_prices)
    best_available_price = min(in_stock_values)
    
    # Находим магазин с лучшей ценой В НАЛИЧИИ
    best_store = next(p for p in in_stock_prices if p['price'] == best_available_price)
    
    # Количество магазинов с данными
    store_count = len(set(p['store'] for p in prices_data))
    
    # === DEAL SCORE ===
    if market_median == 0:
        deal_score = 0
    else:
        # Скидка от медианы (на основе ЛУЧШЕЙ ДОСТУПНОЙ цены)
        discount_pct = ((market_median - best_available_price) / market_median) * 100
        
        # Базовый скор (0-80)
        base_score = min(80, max(0, discount_pct * 2))
        
        # Бонус за близость к абсолютному минимуму (0-20)
        if market_min > 0:
            proximity_bonus = max(0, 20 - ((best_available_price - market_min) / market_median) * 100)
        else:
            proximity_bonus = 0
        
        deal_score = min(100, base_score + proximity_bonus)
    
    # === CONFIDENCE SCORE ===
    # Зависит от количества магазинов и разброса цен
    confidence = min(100, store_count * 25)  # 4+ магазина = 100% confidence
    
    # Штраф если цены слишком разные (возможно плохой матчинг)
    if len(all_prices) > 1:
        price_variance = max(all_prices) - min(all_prices)
        if price_variance > market_median * 0.5:  # Разброс > 50%
            confidence *= 0.7
    
    confidence = int(confidence)
    
    # === КЛАССИФИКАЦИЯ ===
    if deal_score >= 80 and confidence >= 70:
        classification = "🔥 VERY GOOD DEAL"
    elif deal_score >= 60 and confidence >= 50:
        classification = "🟢 GOOD DEAL"
    elif deal_score >= 40:
        classification = "🟡 NORMAL PRICE"
    else:
        classification = "🔴 BAD DEAL"
    
    # Причина
    if best_available_price < market_median * 0.8:
        reason = f"{discount_pct:.0f}% below market, available at {best_store['store']}"
    else:
        reason = f"Price close to market median (${market_median:.0f})"
    
    return {
        'deal_score': int(deal_score),
        'confidence': confidence,
        'classification': classification,
        'best_price': best_available_price,
        'best_store': best_store['store'],
        'market_median': market_median,
        'discount_pct': discount_pct if market_median > 0 else 0,
        'store_count': store_count,
        'in_stock_count': len(in_stock_prices),
        'reason': reason
    }

def find_best_deals(limit: int = 20):
    """Находит топ-N лучших сделок по всей базе."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получаем все canonical варианты с матчами
        result = conn.execute(text("""
            SELECT DISTINCT pm.canonical_variant_id
            FROM product_matches pm
        """))
        
        canonical_ids = [row[0] for row in result.fetchall()]
        
        print(f"🔍 Analyzing {len(canonical_ids)} matched products...\n")
        
        deals = []
        
        for canon_id in canonical_ids:
            # Получаем цены
            prices_result = conn.execute(text("""
                SELECT 
                    o.current_price, o.old_price, o.in_stock,
                    s.name, p.canonical_name, pv.sku,
                    b.name as brand
                FROM offers o
                JOIN stores s ON o.store_id = s.id
                JOIN product_variants pv ON o.variant_id = pv.id
                JOIN products p ON pv.product_id = p.id
                JOIN brands b ON p.brand_id = b.id
                WHERE o.variant_id = :id
                
                UNION
                
                SELECT 
                    o.current_price, o.old_price, o.in_stock,
                    s.name, p.canonical_name, pv.sku,
                    b.name as brand
                FROM offers o
                JOIN stores s ON o.store_id = s.id
                JOIN product_variants pv ON o.variant_id = pv.id
                JOIN products p ON pv.product_id = p.id
                JOIN brands b ON p.brand_id = b.id
                JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
                WHERE pm.canonical_variant_id = :id
            """), {'id': canon_id})
            
            prices_data = [
                {
                    'price': float(row[0]),
                    'old_price': float(row[1]) if row[1] else None,
                    'in_stock': row[2],
                    'store': row[3],
                    'name': row[4],
                    'sku': row[5],
                    'brand': row[6]
                }
                for row in prices_result.fetchall()
            ]
            
            if not prices_data:
                continue
            
            # Рассчитываем метрики
            metrics = calculate_enhanced_deal_score(prices_data)
            
            # Сохраняем если есть смысл
            if metrics['deal_score'] > 0 and metrics['confidence'] > 0:
                deals.append({
                    'name': prices_data[0]['name'],
                    'brand': prices_data[0]['brand'],
                    'sku': prices_data[0]['sku'],
                    **metrics,
                    'all_prices': prices_data
                })
        
        # Сортируем по deal_score + confidence
        deals.sort(key=lambda x: (x['deal_score'] + x['confidence'] * 0.5), reverse=True)
        
        print("="*80)
        print(f"🏆 TOP {limit} BEST DEALS (sorted by Deal Score + Confidence)")
        print("="*80)
        
        for i, deal in enumerate(deals[:limit], 1):
            print(f"\n#{i}  {deal['classification']}")
            print(f"   🏷️  {deal['brand']} — {deal['name']}")
            print(f"   📦 SKU: {deal['sku']}")
            print(f"   💰 Best Price: ${deal['best_price']:.2f} @ {deal['best_store']}")
            print(f"   📊 Market Median: ${deal['market_median']:.2f} ({deal['discount_pct']:.0f}% off)")
            print(f"   🎯 Deal Score: {deal['deal_score']}/100 | Confidence: {deal['confidence']}%")
            print(f"   🏪 Stores: {deal['store_count']} ({deal['in_stock_count']} in stock)")
            print(f"   💡 {deal['reason']}")
            print("-"*80)
        
        # Статистика
        very_good = sum(1 for d in deals if 'VERY GOOD' in d['classification'])
        good = sum(1 for d in deals if 'GOOD DEAL' in d['classification'])
        normal = sum(1 for d in deals if 'NORMAL' in d['classification'])
        
        print(f"\n📈 Summary: {very_good} VERY GOOD | {good} GOOD | {normal} NORMAL")

if __name__ == "__main__":
    find_best_deals(limit=20)
