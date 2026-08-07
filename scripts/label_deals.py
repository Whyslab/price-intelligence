#!/usr/bin/env python3
"""
Интерактивная разметка deal alerts для precision measurement. 
Usage: python scripts/label_deals.py [count]
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

def label_deals(count: int = 100):
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получить неразмеченные алерты с полной информацией
        alerts = conn.execute(text("""
            SELECT 
                da.id, da.sent_at, s.name AS store, da.price, 
                da.deal_score, da.confidence, da.classification,
                p.canonical_name, b.name AS brand, pv.sku,
                pm.canonical_variant_id
            FROM deal_alerts da
            JOIN stores s ON da.store_id = s.id
            LEFT JOIN offers o ON o.store_id = da.store_id AND o.current_price = da.price
            LEFT JOIN product_variants pv ON o.variant_id = pv.id
            LEFT JOIN products p ON pv.product_id = p.id
            LEFT JOIN brands b ON p.brand_id = b.id
            LEFT JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
            LEFT JOIN deal_validation dv ON dv.alert_id = da.id
            WHERE dv.id IS NULL
            GROUP BY da.id, da.sent_at, s.name, da.price, da.deal_score, da.confidence,
                     da.classification, p.canonical_name, b.name, pv.sku, pm.canonical_variant_id
            ORDER BY da.sent_at DESC
            LIMIT :count
        """), {'count': count}).fetchall()
        
        if not alerts:
            print("✅ All alerts are labeled")
            return
        
        print(f"\n🏷️  Labeling {len(alerts)} alerts (1=good, 0=bad, Enter=skip):\n")
        
        labeled_count = 0
        for i, alert in enumerate(alerts, 1):
            alert_id, sent_at, store, price, score, conf, classification, name, brand, sku, canon_id = alert
            
            print(f"[{i}/{len(alerts)}] {classification}")
            print(f"  🏷️  {brand or 'Unknown'} — {name or 'Unknown product'}")
            print(f"  📦 SKU: {sku or 'N/A'}")
            print(f"  💰 ${price:.2f} @ {store}")
            print(f"  🎯 Score: {score}/100 | Conf: {conf}%")
            
            label = input("  Label (1/0/skip): ").strip()
            if label in ('1', '0'):
                notes = input("  Notes (optional): ").strip()
                conn.execute(text("""
                    INSERT INTO deal_validation (alert_id, label, notes)
                    VALUES (:alert_id, :label, :notes)
                    ON CONFLICT (alert_id, user_id) DO NOTHING
                """), {'alert_id': alert_id, 'label': int(label), 'notes': notes or None})
                conn.commit()
                print(f"  ✅ Labeled as {'🟢 GOOD' if label == '1' else '🔴 BAD'}")
                labeled_count += 1
            else:
                print("  ⏭️  Skipped")
            print()
            
            if i % 20 == 0:
                print(f"--- Progress: {labeled_count} labeled so far ---\n")
        
        # Показать финальную precision
        metrics = conn.execute(text("SELECT * FROM deal_precision_metrics")).fetchone()
        if metrics:
            tp, fp, labeled, total, precision = metrics
            print(f"\n📊 PRECISION METRICS (last 30 days):")
            print(f"   Total alerts: {total}")
            print(f"   Labeled: {labeled}")
            print(f"   True positives: {tp}")
            print(f"   False positives: {fp}")
            print(f"   🎯 Precision: {precision}% ({tp} good / {labeled} labeled)")
            print(f"\n   Это главный бизнес-KPI системы.")

if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    label_deals(count)
