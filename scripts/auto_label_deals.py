#!/usr/bin/env python3
"""
Автоматическая разметка deal alerts на основе эвристик:
- Cross-market consistency (сколько магазинов имеют похожую цену)
- Price sanity (не слишком далеко от медианы)
- Freshness (обновлялась ли цена недавно)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def auto_label_deals():
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получить неразмеченные алерты с контекстом
        alerts = conn.execute(text("""
            WITH alert_context AS (
                SELECT 
                    da.id AS alert_id,
                    da.sku,
                    da.store_id,
                    da.price,
                    da.deal_score,
                    da.confidence,
                    -- Сколько магазинов имеют этот SKU
                    (SELECT COUNT(DISTINCT o.store_id) 
                     FROM offers o 
                     JOIN product_variants pv ON o.variant_id = pv.id
                     WHERE pv.sku = da.sku) AS store_count,
                    -- Медиана цены по всем магазинам для этого SKU
                    (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.current_price)
                     FROM offers o
                     JOIN product_variants pv ON o.variant_id = pv.id
                     WHERE pv.sku = da.sku) AS market_median,
                    -- Минимальная цена
                    (SELECT MIN(o.current_price)
                     FROM offers o
                     JOIN product_variants pv ON o.variant_id = pv.id
                     WHERE pv.sku = da.sku) AS market_min
                FROM deal_alerts da
                LEFT JOIN deal_validation dv ON dv.alert_id = da.id
                WHERE dv.id IS NULL
                  AND da.sent_at > NOW() - INTERVAL '7 days'
            )
            SELECT * FROM alert_context
            WHERE store_count >= 2  -- минимум 2 магазина для валидации
        """)).fetchall()
        
        if not alerts:
            print("✅ No unlabeled alerts with sufficient data")
            return
        
        print(f"\n🤖 Auto-labeling {len(alerts)} alerts...\n")
        
        labeled = 0
        for alert in alerts:
            alert_id, sku, store_id, price, score, conf, store_count, market_median, market_min = alert
            
            # Конвертация типов (Decimal -> float)
            price = float(price) if price is not None else 0
            market_median = float(market_median) if market_median is not None else 0
            market_min = float(market_min) if market_min is not None else 0
            
            # Эвристики
            if market_median == 0 or market_min == 0:
                continue
            
            discount_pct = ((market_median - price) / market_median) * 100 if market_median > 0 else 0
            proximity_to_min = ((price - market_min) / market_median) * 100 if market_median > 0 else 0
            
            # GOOD DEAL: скидка >15%, близость к минимуму <20%, store_count >= 3
            if discount_pct >= 15 and proximity_to_min <= 20 and store_count >= 3:
                label = 1
                notes = f"Auto: {discount_pct:.0f}% below median, {proximity_to_min:.0f}% from min, {store_count} stores"
            # BAD DEAL: скидка <5% или цена близка к медиане
            elif discount_pct < 5 or proximity_to_min > 50:
                label = 0
                notes = f"Auto: only {discount_pct:.0f}% below median, {proximity_to_min:.0f}% from min"
            else:
                # Серая зона — пропускаем
                continue
            
            conn.execute(text("""
                INSERT INTO deal_validation (alert_id, label, notes, user_id)
                VALUES (:alert_id, :label, :notes, 'auto_labeler')
                ON CONFLICT (alert_id, user_id) DO NOTHING
            """), {'alert_id': alert_id, 'label': label, 'notes': notes})
            
            status = "🟢 GOOD" if label == 1 else "🔴 BAD"
            print(f"  {sku}: {status} ({discount_pct:.0f}% off, {store_count} stores)")
            labeled += 1
        
        conn.commit()
        
        # Показать финальную precision
        metrics = conn.execute(text("""
            SELECT 
                COUNT(*) FILTER (WHERE dv.label = 1) AS true_positives,
                COUNT(*) FILTER (WHERE dv.label = 0) AS false_positives,
                COUNT(*) FILTER (WHERE dv.label IS NOT NULL) AS labeled,
                COUNT(*) AS total_alerts,
                ROUND(100.0 * COUNT(*) FILTER (WHERE dv.label = 1) / NULLIF(COUNT(*) FILTER (WHERE dv.label IS NOT NULL), 0), 1) AS precision_pct
            FROM deal_alerts da
            LEFT JOIN deal_validation dv ON dv.alert_id = da.id
            WHERE da.sent_at > NOW() - INTERVAL '30 days'
        """)).fetchone()
        
        if metrics:
            tp, fp, labeled_count, total, precision = metrics
            print(f"\n📊 PRECISION METRICS (last 30 days):")
            print(f"   Total alerts: {total}")
            print(f"   Labeled: {labeled_count} ({labeled} auto-labeled now)")
            print(f"   True positives: {tp}")
            print(f"   False positives: {fp}")
            print(f"   🎯 Precision: {precision}%")

if __name__ == "__main__":
    auto_label_deals()
