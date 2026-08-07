#!/usr/bin/env python3
"""
Рассчитывает reliability score (0-100) для каждого магазина на основе:
- Uptime (% successful syncs за 30 дней)
- Data freshness (часы с последнего обновления)
- Currency error rate
- Price anomaly rate
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
from datetime import datetime, timezone

def calculate_reliability_scores():
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получить все магазины с metadata (без pipeline_runs per-store)
        stores = conn.execute(text("""
            SELECT 
                s.id,
                s.name,
                s.domain,
                s.sync_status,
                s.last_sync,
                s.last_successful_sync,
                s.last_error,
                s.products_count,
                -- Currency errors за 30 дней
                (SELECT COUNT(*) FROM currency_errors ce 
                 WHERE ce.domain = s.domain 
                   AND ce.timestamp > NOW() - INTERVAL '30 days') AS currency_errors_30d,
                -- Часы с последнего успешного обновления
                EXTRACT(EPOCH FROM (NOW() - COALESCE(s.last_successful_sync, s.last_sync))) / 3600 AS hours_since_sync
            FROM stores s
            WHERE s.sync_status != 'dead'
            ORDER BY s.name
        """)).fetchall()
        
        if not stores:
            print("❌ No stores found")
            return
        
        print(f"\n🔢 Calculating reliability for {len(stores)} stores...\n")
        
        updated = 0
        for store in stores:
            store_id, name, domain, status, last_sync, last_success, last_error, products_count, \
                currency_errors, hours_since_sync = store
            
            # === STATUS SCORE (0-40 points) ===
            # Используем текущий sync_status как proxy для uptime
            if status == 'success':
                uptime_score = 40
            elif status == 'empty':
                uptime_score = 20
            elif status == 'error':
                uptime_score = 5
            else:
                uptime_score = 10  # unknown/stale
            
            # === FRESHNESS (0-30 points) ===
            if hours_since_sync is None:
                freshness_score = 0
            elif hours_since_sync < 6:
                freshness_score = 30  # отлично
            elif hours_since_sync < 24:
                freshness_score = 20  # хорошо
            elif hours_since_sync < 72:
                freshness_score = 10  # stale
            else:
                freshness_score = 0   # very stale/dead
            
            # === ERROR RATE (0-30 points) ===
            # Penalty за currency errors и ошибки парсинга
            error_penalty = 0
            if currency_errors > 10:
                error_penalty += 15
            elif currency_errors > 5:
                error_penalty += 10
            elif currency_errors > 0:
                error_penalty += 5
            
            if status == 'error':
                error_penalty += 15
            elif status == 'empty':
                error_penalty += 10
            
            error_score = max(0, 30 - error_penalty)
            
            # === ИТОГОВЫЙ SCORE ===
            reliability_score = int(uptime_score + freshness_score + error_score)
            reliability_score = max(0, min(100, reliability_score))
            
            # Обновить в БД
            conn.execute(text("""
                UPDATE stores 
                SET reliability_score = :score
                WHERE id = :id
            """), {'score': reliability_score, 'id': store_id})
            
            updated += 1
            
            # Показать топ-10 худших
            if reliability_score < 50:
                freshness_str = f"{hours_since_sync:.0f}h" if hours_since_sync is not None else "N/A"
                print(f"  ⚠️  {name}: {reliability_score}/100 (status: {status}, freshness: {freshness_str})")
        
        conn.commit()
        print(f"\n✅ Updated {updated} stores")
        
        # Показать distribution
        distribution = conn.execute(text("""
            SELECT 
                CASE 
                    WHEN reliability_score >= 80 THEN 'Excellent (80-100)'
                    WHEN reliability_score >= 60 THEN 'Good (60-79)'
                    WHEN reliability_score >= 40 THEN 'Fair (40-59)'
                    WHEN reliability_score >= 20 THEN 'Poor (20-39)'
                    ELSE 'Critical (0-19)'
                END AS category,
                COUNT(*) AS count,
                ROUND(AVG(reliability_score), 1) AS avg_score
            FROM stores
            WHERE sync_status != 'dead'
            GROUP BY category
            ORDER BY MIN(reliability_score) DESC
        """)).fetchall()
        
        print(f"\n📊 Reliability Distribution:")
        for category, count, avg in distribution:
            print(f"   {category}: {count} stores (avg {avg})")
        
        # Топ-10 самых надёжных
        top_reliable = conn.execute(text("""
            SELECT name, reliability_score 
            FROM stores 
            WHERE sync_status != 'dead'
            ORDER BY reliability_score DESC 
            LIMIT 10
        """)).fetchall()
        
        print(f"\n🏆 Top 10 Reliable Stores:")
        for name, score in top_reliable:
            print(f"   {name}: {score}/100")

if __name__ == "__main__":
    calculate_reliability_scores()
