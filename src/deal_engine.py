"""
Deal Engine v2: находит лучшие сделки с учётом истории цен и weighted statistics.

Изменения vs v1:
- Historical component (50%): weighted median, percentile, time-at-price
- Enhanced Confidence: freshness, store count, price variance
- Weighted statistics по интервалам (не observations)
"""
import statistics

from sqlalchemy import create_engine, text

from src.config import DATABASE_URL
from src.pricing import sanitize_prices


def get_historical_metrics(conn, variant_ids: list) -> dict:
    """
    Получает weighted historical metrics для списка variant_ids.
    Возвращает dict: {variant_id: {median, percentile_10, days_at_current, total_intervals}}
    """
    if not variant_ids:
        return {}
    
    # P1-22: Efficient weighted median without ()
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
        interval_counts AS (
            SELECT vid, COUNT(*) AS total_intervals
            FROM intervals
            GROUP BY vid
        ),
        weighted AS (
            SELECT 
                vid,
                price,
                days_at_price,
                SUM(days_at_price) OVER (PARTITION BY vid) AS total_days,
                SUM(days_at_price) OVER (PARTITION BY vid ORDER BY price) AS cumulative_weight,
                MAX(ended_at) OVER (PARTITION BY vid) AS last_observed,
                FIRST_VALUE(price) OVER (PARTITION BY vid ORDER BY CASE WHEN ended_at IS NULL THEN 0 ELSE 1 END, ended_at DESC) AS current_price
            FROM intervals
        ),
        medians AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_median
            FROM weighted
            WHERE cumulative_weight >= total_days / 2.0
            ORDER BY vid, cumulative_weight
        ),
        p10 AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_p10
            FROM weighted
            WHERE cumulative_weight >= total_days * 0.1
            ORDER BY vid, cumulative_weight
        ),
        p90 AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_p90
            FROM weighted
            WHERE cumulative_weight >= total_days * 0.9
            ORDER BY vid, cumulative_weight
        )
        SELECT 
            w.vid,
            m.weighted_median,
            p1.weighted_p10,
            p9.weighted_p90,
            MIN(w.price) AS historical_min,
            MAX(w.price) AS historical_max,
            MAX(w.total_days) AS total_days,
            MAX(w.current_price) AS current_price,
            EXTRACT(EPOCH FROM (NOW() - MAX(w.last_observed))) / 86400 AS days_since_last_update,
            ic.total_intervals
        FROM weighted w
        JOIN medians m ON m.vid = w.vid
        JOIN p10 p1 ON p1.vid = w.vid
        JOIN p90 p9 ON p9.vid = w.vid
        JOIN interval_counts ic ON ic.vid = w.vid
        GROUP BY w.vid, m.weighted_median, p1.weighted_p10, p9.weighted_p90, ic.total_intervals
    """)
    
    result = conn.execute(sql, {'ids': variant_ids}).fetchall()
    return {
        row[0]: {
            'median': float(row[1]) if row[1] else 0,
            'percentile_10': float(row[2]) if row[2] else 0,
            'percentile_90': float(row[3]) if row[3] else 0,
            'min': float(row[4]) if row[4] else 0,
            'max': float(row[5]) if row[5] else 0,
            'total_days': float(row[6]) if row[6] else 0,
            'current_price': float(row[7]) if row[7] else 0,
            'days_since_update': float(row[8]) if row[8] else 0,
            'total_intervals': int(row[9]) if row[9] else 0
        }
        for row in result
    }


def weighted_median(values, weights):
    import statistics
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

def analyze_discount_duration(variant_id: int, store_id: int, current_price: float, conn) -> dict:
    if not conn:
        return {'duration_days': None, 'is_fake': False, 'is_real': False, 'discount_pct': 0.0}
    try:
        result = conn.execute(text("""
            SELECT
                pc_old.price AS old_price,
                EXTRACT(EPOCH FROM (pc.started_at - pc_old.started_at)) / 86400 AS old_price_days,
                pc.started_at AS discount_started
            FROM price_changes pc
            JOIN price_changes pc_old ON pc_old.variant_id = pc.variant_id
                AND pc_old.store_id = pc.store_id
                AND pc_old.ended_at = pc.started_at
            WHERE pc.variant_id = :variant_id
                AND pc.store_id = :store_id
                AND pc.price = :current_price
                AND pc.ended_at IS NULL
                AND pc_old.price > pc.price
            ORDER BY pc.started_at DESC
            LIMIT 1
        """), {'variant_id': variant_id, 'store_id': store_id, 'current_price': current_price}).fetchone()
    except Exception:
        return {'duration_days': None, 'is_fake': False, 'is_real': False, 'discount_pct': 0.0}
        
    if not result:
        return {'duration_days': None, 'is_fake': False, 'is_real': False, 'discount_pct': 0.0}
        
    old_price, duration_days, discount_started = result
    discount_pct = ((float(old_price) - float(current_price)) / float(old_price)) * 100 if old_price else 0
    is_fake = duration_days > 14 if duration_days else False
    is_real = duration_days < 7 if duration_days else False
    
    return {
        'duration_days': duration_days,
        'is_fake': is_fake,
        'is_real': is_real,
        'discount_pct': discount_pct,
        'old_price': old_price
    }

def calculate_deal_score_v2(prices_data: list, historical: dict, conn=None) -> dict:
    """
    Deal Score v2: 50% cross-market + 50% historical.
    Uses weighted median based on store reliability.
    """
    import statistics
    in_stock_prices = [p for p in prices_data if p['in_stock']]
    if not in_stock_prices:
        return {'deal_score': 0, 'confidence': 0, 'classification': '⚫ OUT OF STOCK', 'reason': 'No stock available', 'discount_analysis': None, 'best_price': 0, 'best_store': '', 'market_median': 0, 'historical_median': 0, 'discount_pct': 0, 'store_count': 0, 'in_stock_count': 0, 'reliability_factor': 0}

    all_prices = sanitize_prices([p['price'] for p in prices_data])
    in_stock_values = sanitize_prices([p['price'] for p in in_stock_prices])
    if not all_prices or not in_stock_values:
        return {'deal_score': 0, 'confidence': 0, 'classification': '⚫ SANITY REJECT', 'reason': 'Prices failed sanity check', 'discount_analysis': None, 'best_price': 0, 'best_store': '', 'market_median': 0, 'historical_median': 0, 'discount_pct': 0, 'store_count': 0, 'in_stock_count': 0, 'reliability_factor': 0}

    reliability_map = {}
    if conn:
        store_names = list(set([p['store'] for p in prices_data]))
        try:
            rows = conn.execute(text("SELECT name, reliability_score FROM stores WHERE name = ANY(:store_names)"), {'store_names': store_names}).fetchall()
            reliability_map = {r[0]: float(r[1]) for r in rows}
        except Exception as e: import sys; print(f'[DealEngine] error: {e}', file=sys.stderr)

    valid_prices, valid_weights = [], []
    all_prices_float = [float(x) for x in all_prices]
    for p in prices_data:
        p_price = float(p['price'])
        if p_price in all_prices_float:
            valid_prices.append(p_price)
            valid_weights.append(max(1.0, reliability_map.get(p['store'], 50.0)))

    market_median = weighted_median(valid_prices, valid_weights) if valid_prices else statistics.median(all_prices)
    market_min = min(all_prices)
    best_available_price = min(in_stock_values)
    best_store = next(p for p in in_stock_prices if p['price'] == best_available_price)
    store_count = len(set(p['store'] for p in prices_data))

    if market_median == 0:
        cross_market_score, discount_pct = 0, 0
    else:
        discount_pct = ((market_median - best_available_price) / market_median) * 100
        base_score = min(80, max(0, discount_pct * 2))
        proximity_bonus = max(0, 20 - ((best_available_price - market_min) / market_median) * 100) if market_min > 0 else 0
        cross_market_score = min(100, base_score + proximity_bonus)

    canonical_id = prices_data[0].get('canonical_id')
    hist = historical.get(canonical_id, {})
    if not hist or hist.get('total_intervals', 0) < 2:
        historical_score = cross_market_score * 0.5
        deal_score = cross_market_score
    else:
        hist_median = hist['median']
        hist_percentile_10 = hist.get('percentile_10', 0)
        if best_available_price <= hist_percentile_10: historical_score = 80
        elif hist_median > 0: historical_score = min(70, max(0, ((hist_median - best_available_price) / hist_median) * 200))
        else: historical_score = 0
        if hist.get('days_since_update', 0) > 7: historical_score *= 0.7
        if hist.get('total_days', 0) > 30: historical_score *= 0.8
        deal_score = min(100, cross_market_score * 0.5 + historical_score * 0.5)

    base_confidence = min(100, store_count * 25)
    freshness_factor = 1.0 if (hist and hist.get('days_since_update', 8) < 1) else (0.8 if (hist and hist.get('days_since_update', 8) < 7) else 0.5)
    
    if reliability_map:
        in_scores = [reliability_map[p['store']] for p in in_stock_prices if p['store'] in reliability_map]
        reliability_factor = (sum(in_scores) / len(in_scores)) / 100.0 if in_scores else 0.5
    else:
        reliability_factor = 0.7

    if len(all_prices) > 1 and market_median > 0 and (max(all_prices) - min(all_prices)) > market_median * 0.5:
        base_confidence *= 0.7
    confidence = int(base_confidence * freshness_factor * reliability_factor)

    if deal_score >= 80 and confidence >= 70: classification = "🔥 VERY GOOD DEAL"
    elif deal_score >= 60 and confidence >= 50: classification = "🟢 GOOD DEAL"
    elif deal_score >= 40: classification = "🟡 NORMAL PRICE"
    else: classification = "🔴 BAD DEAL"

    if market_median > 0 and best_available_price < market_median * 0.8: reason = f"{discount_pct:.0f}% below market, available at {best_store['store']}"
    elif hist and hist.get('total_intervals', 0) >= 2 and best_available_price <= hist.get('percentile_10', 0): reason = f"Rare price (below 10th percentile), available at {best_store['store']}"
    else: reason = f"Price close to market median (${market_median:.0f})"

    discount_info = None
    if conn and best_store and canonical_id:
        try:
            store_info = conn.execute(text("SELECT id FROM stores WHERE name = :name"), {'name': best_store['store']}).fetchone()
            if store_info:
                discount_info = analyze_discount_duration(canonical_id, store_info[0], float(best_available_price), conn)
                if discount_info['is_fake']:
                    confidence = max(0, int(confidence * 0.7))
                    classification = classification.replace("🔥 VERY GOOD", "⚠️ FAKE DISCOUNT")
                    reason += f" (old_price held for {discount_info['duration_days']:.0f} days)"
                elif discount_info['is_real']:
                    confidence = min(100, int(confidence * 1.1))
                    reason += f" 🔥 REAL SALE ({discount_info['duration_days']:.0f} days)"
        except Exception as e: import sys; print(f'[DealEngine] error: {e}', file=sys.stderr)

    return {
        'discount_analysis': discount_info, 'deal_score': int(deal_score), 'confidence': confidence,
        'classification': classification, 'best_price': best_available_price, 'best_store': best_store['store'],
        'market_median': market_median, 'historical_median': hist.get('median', 0) if hist else 0,
        'discount_pct': discount_pct if market_median > 0 else 0, 'store_count': store_count,
        'in_stock_count': len(in_stock_prices), 'reliability_factor': round(reliability_factor, 2), 'reason': reason
    }

def find_best_deals(limit: int = 20):
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
        for variant_id in variant_ids:  # Ограничиваем для производительности
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
        
        return deals[:limit]


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
        
    except Exception:
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
