"""
Telegram Notifications: отправляет уведомления о лучших сделках.
Использует Price Sanity Layer для защиты от аномалий.
"""
import os
import requests
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
from src.pricing import deal_metrics
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️  Telegram credentials not set")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10
        )
        print("✅ Sent" if r.status_code == 200 else f"❌ {r.text[:100]}")
    except Exception as e:
        print(f"❌ {e}")

def check_and_notify(min_deal_score: int = 70, min_confidence: int = 50):
    engine = create_engine(DATABASE_URL)
    notifications = []
    
    with engine.connect() as conn:
        canon_ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT canonical_variant_id FROM product_matches"
        )).fetchall()]
        
        for canon_id in canon_ids:
            rows = conn.execute(text("""
                SELECT o.current_price, o.old_price, o.in_stock, s.name,
                       p.canonical_name, pv.sku, pv.size, pv.color, b.name, o.url
                FROM offers o
                JOIN stores s ON o.store_id = s.id
                JOIN product_variants pv ON o.variant_id = pv.id
                JOIN products p ON pv.product_id = p.id
                JOIN brands b ON p.brand_id = b.id
                WHERE o.variant_id = :id
                UNION
                SELECT o.current_price, o.old_price, o.in_stock, s.name,
                       p.canonical_name, pv.sku, pv.size, pv.color, b.name, o.url
                FROM offers o
                JOIN stores s ON o.store_id = s.id
                JOIN product_variants pv ON o.variant_id = pv.id
                JOIN products p ON pv.product_id = p.id
                JOIN brands b ON p.brand_id = b.id
                JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
                WHERE pm.canonical_variant_id = :id
            """), {'id': canon_id}).fetchall()
            
            if not rows:
                continue
            
            prices_data = [{
                'price': float(r[0]), 'in_stock': r[2], 'store': r[3],
                'name': r[4], 'sku': r[5], 'size': r[6], 'color': r[7],
                'brand': r[8], 'url': r[9]
            } for r in rows]
            
            in_stock = [p for p in prices_data if p['in_stock']]
            if not in_stock:
                continue
            
            best = min(in_stock, key=lambda x: x['price'])
            metrics = deal_metrics([p['price'] for p in prices_data], best['price'])
            
            if not metrics:
                continue
            
            confidence = min(100, len(set(p['store'] for p in prices_data)) * 25)
            
            if metrics['deal_score'] >= min_deal_score and confidence >= min_confidence:
                notifications.append({**best, **metrics, 'confidence': confidence})
    
    notifications.sort(key=lambda x: x['deal_score'], reverse=True)
    print(f"📱 Found {len(notifications)} deals to notify")
    
    for d in notifications[:5]:
        send_telegram_message(
            f"{d['classification']} DEAL\n\n"
            f"<b>{d['brand']}</b>\n{d['name']}\n"
            f"Size: {d['size']} | SKU: <code>{d['sku']}</code>\n\n"
            f"💰 <b>${d['price']:.2f}</b> @ {d['store']}\n"
            f"📊 Market: ${d['market_median']:.2f} ({d['discount_pct']:.0f}% off)\n"
            f"🎯 Score: {d['deal_score']}/100 | Confidence: {d['confidence']}%\n\n"
            f"🔗 {d['url']}"
        )

if __name__ == "__main__":
    check_and_notify()
