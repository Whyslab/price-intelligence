"""
Deal Engine: находит лучшие сделки по всей базе с учётом наличия и confidence.
"""
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
import statistics


def weighted_median(values, weights):
    """Рассчитывает взвешенную медиану."""
    if not values or not weights or len(values) != len(weights):
        return statistics.median(values) if values else 0
    sorted_data = sorted(zip(values, weights), key=lambda x: x[0])
    total_weight = sum(w for _, w in sorted_data)
    if total_weight == 0:
        return statistics.median(values)
    cum = 0
    for val, w in sorted_data:
        cum += w
        if cum >= total_weight / 2:
            return val
    return sorted_data[-1][0]


def get_time_at_price(conn, variant_id, store_id, current_price):
    """
    P1-25: Time-at-price. Возвращает сколько часов держится текущая цена.
    Использует price_changes: запись с ended_at IS NULL = текущая цена.
    """
    from datetime import datetime, timezone
    try:
        row = conn.execute(text("""
            SELECT started_at
            FROM price_changes
            WHERE variant_id = :variant_id
              AND store_id = :store_id
              AND price = :price
              AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
        """), {'variant_id': variant_id, 'store_id': store_id, 'price': current_price}).fetchone()
        
        if not row or not row[0]:
            return None
        
        started_at = row[0]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        hours = (now - started_at).total_seconds() / 3600
        return round(hours, 1)
    except Exception:
        return None


def analyze_discount_duration_v2(conn, variant_id, store_id, current_price):
    """
    P1-26 v2: Улучшенная детекция фейковых скидок.
    Анализирует:
    1. Сколько дней держалась old_price
    2. Частоту изменений цены
    3. Сравнение с рыночной медианой
    
    Возвращает dict: {
        'is_fake': bool,
        'discount_tag': str,
        'old_price_duration_days': float,
        'price_change_frequency': int
    }
    """
    from datetime import datetime, timezone
    
    try:
        # 1. Находим текущую цену (ended_at IS NULL)
        current_row = conn.execute(text("""
            SELECT started_at, price, old_price
            FROM price_changes
            WHERE variant_id = :variant_id
              AND store_id = :store_id
              AND price = :price
              AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
        """), {'variant_id': variant_id, 'store_id': store_id, 'price': current_price}).fetchone()
        
        if not current_row:
            return {'is_fake': False, 'discount_tag': '💰 DEAL', 'old_price_duration_days': None, 'price_change_frequency': 0}
        
        current_started, current_price_val, old_price_val = current_row
        
        # 2. Если есть old_price, находим, сколько дней он держался
        old_price_duration_days = None
        if old_price_val:
            old_row = conn.execute(text("""
                SELECT started_at, ended_at
                FROM price_changes
                WHERE variant_id = :variant_id
                  AND store_id = :store_id
                  AND price = :old_price
                  AND ended_at = :current_started
                ORDER BY started_at DESC
                LIMIT 1
            """), {
                'variant_id': variant_id, 
                'store_id': store_id, 
                'old_price': old_price_val,
                'current_started': current_started
            }).fetchone()
            
            if old_row and old_row[0] and old_row[1]:
                old_started, old_ended = old_row
                if old_started.tzinfo is None:
                    old_started = old_started.replace(tzinfo=timezone.utc)
                if old_ended.tzinfo is None:
                    old_ended = old_ended.replace(tzinfo=timezone.utc)
                old_price_duration_days = (old_ended - old_started).total_seconds() / 86400
        
        # 3. Считаем частоту изменений цены за последние 30 дней
        now = datetime.now(timezone.utc)
        thirty_days_ago = now.timestamp() - (30 * 86400)
        
        freq_result = conn.execute(text("""
            SELECT COUNT(*)
            FROM price_changes
            WHERE variant_id = :variant_id
              AND store_id = :store_id
              AND EXTRACT(EPOCH FROM started_at) > :threshold
        """), {'variant_id': variant_id, 'store_id': store_id, 'threshold': thirty_days_ago}).fetchone()
        
        price_change_frequency = freq_result[0] if freq_result else 0
        
        # 4. Применяем логику детекции
        is_fake = False
        discount_tag = '💰 DEAL'
        
        if old_price_duration_days is not None:
            if old_price_duration_days < 7:
                # Реальная скидка - old_price держался менее 7 дней
                discount_tag = '🔥 REAL SALE'
                is_fake = False
            elif old_price_duration_days > 14:
                # Фейковая скидка - old_price держался более 14 дней (это обычная цена)
                discount_tag = '⚠️ PERMANENT SALE'
                is_fake = True
            else:
                # Неопределенность (7-14 дней)
                discount_tag = '📊 UNCERTAIN'
                is_fake = False
        
        # Если цена часто меняется (> 3 раз за 30 дней) - это подозрительно
        if price_change_frequency > 3:
            discount_tag = '📊 PRICE VOLATILE'
            is_fake = True
        
        return {
            'is_fake': is_fake,
            'discount_tag': discount_tag,
            'old_price_duration_days': old_price_duration_days,
            'price_change_frequency': price_change_frequency
        }
        
    except Exception as e:
        return {'is_fake': False, 'discount_tag': '💰 DEAL', 'old_price_duration_days': None, 'price_change_frequency': 0}

def calculate_enhanced_deal_score(prices_data: list, conn=None) -> dict:
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
    # P1-23: Weighted Market Median (W = Reliability * Freshness * Stock)
    import math
    from datetime import datetime, timezone
    current_time = datetime.now(timezone.utc)
    
    valid_prices, valid_weights = [], []
    for p in prices_data:
        R = max(0.1, min(1.0, p.get('reliability', 0.5)))
        
        # F: Freshness (экспоненциальное затухание)
        updated_at = p.get('updated_at')
        if updated_at:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            days_old = (current_time - updated_at).total_seconds() / 86400
        else:
            days_old = 30  # Штраф если нет данных о времени
        F = math.exp(-0.1 * max(0, days_old))
        
        # S: Stock (1.0 в наличии, 0.1 нет в наличии)
        S = 1.0 if p.get('in_stock', False) else 0.1
        
        weight = R * F * S
        valid_prices.append(float(p['price']))
        valid_weights.append(weight)
    
    market_median = weighted_median(valid_prices, valid_weights) if valid_prices else statistics.median(all_prices)
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
    
    # P1-26 v2: Fake Discount Detection
    fake_discount_analysis = None
    discount_tag = '💰 DEAL'
    if conn and best_store:
        fake_discount_analysis = analyze_discount_duration_v2(
            conn, best_store['variant_id'], best_store['store_id'], best_available_price
        )
        discount_tag = fake_discount_analysis['discount_tag']
        
        # Штраф за фейковую скидку (только на deal_score, confidence позже)
        if fake_discount_analysis['is_fake']:
            deal_score = max(0, deal_score - 15)
    
    # === CONFIDENCE SCORE ===
    # Зависит от количества магазинов и разброса цен
    confidence = min(100, store_count * 25)  # 4+ магазина = 100% confidence
    
    # Штраф если цены слишком разные (возможно плохой матчинг)
    if len(all_prices) > 1:
        price_variance = max(all_prices) - min(all_prices)
        if price_variance > market_median * 0.5:  # Разброс > 50%
            confidence *= 0.7
    
    confidence = int(confidence)
    
    # P1-26 v2: Штраф на confidence за фейковую скидку
    if fake_discount_analysis and fake_discount_analysis.get('is_fake'):
        confidence = max(0, int(confidence * 0.8))
    
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
        'reason': reason,
        'discount_tag': discount_tag,
        'fake_discount_analysis': fake_discount_analysis
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
                    b.name as brand,
                    s.reliability_score, o.updated_at,
                    pv.id AS variant_id, o.store_id AS store_id
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
                    b.name as brand,
                    s.reliability_score, o.updated_at,
                    pv.id AS variant_id, o.store_id AS store_id
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
                    'brand': row[6],
                    'reliability': float(row[7]) if row[7] else 0.5,
                    'updated_at': row[8],
                    'variant_id': row[9],
                    'store_id': row[10]
                }
                for row in prices_result.fetchall()
            ]
            
            if not prices_data:
                continue
            
            # Рассчитываем метрики
            metrics = calculate_enhanced_deal_score(prices_data, conn)
            
            # P1-25: Time-at-price для лучшей цены
            if metrics.get('deal_score', 0) > 0:
                best = next((p for p in prices_data if p['store'] == metrics.get('best_store')), None)
                if best:
                    metrics['time_at_price_hours'] = get_time_at_price(
                        conn, best['variant_id'], best['store_id'], metrics['best_price']
                    )
            
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
            print(f"   🏷️  {deal.get('discount_tag', '💰 DEAL')}")
            tap = deal.get('time_at_price_hours')
            if tap is not None:
                if tap < 24:
                    print(f"   ⚡ Цена держится всего {tap:.0f} ч — свежее снижение!")
                elif tap < 72:
                    print(f"   🕐 Цена держится {tap/24:.1f} дн")
                else:
                    print(f"   🕰 Цена держится {tap/24:.0f} дн — стабильная")
            print(f"   💡 {deal['reason']}")
            print("-"*80)
        
        # Статистика
        very_good = sum(1 for d in deals if 'VERY GOOD' in d['classification'])
        good = sum(1 for d in deals if 'GOOD DEAL' in d['classification'])
        normal = sum(1 for d in deals if 'NORMAL' in d['classification'])
        
        print(f"\n📈 Summary: {very_good} VERY GOOD | {good} GOOD | {normal} NORMAL")

if __name__ == "__main__":
    find_best_deals(limit=20)
