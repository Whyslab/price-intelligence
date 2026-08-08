"""
Telegram Notifications: отправляет уведомления о лучших сделках.
Использует Deal Engine v2 + Fake Discount Analysis + 24h cooldown.
"""
import os
import requests
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
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
        # P1-42: Проверяем JSON response, а не только HTTP 200
        if r.status_code == 200:
            try:
                resp_json = r.json()
                if resp_json.get("ok"):
                    print("✅ Sent")
                else:
                    print(f"❌ Telegram API error: {resp_json.get('description', 'Unknown')}")
            except ValueError:
                print("❌ Invalid JSON from Telegram")
        elif r.status_code == 429:
            # P1-43: Rate limit handling
            retry_after = r.json().get('parameters', {}).get('retry_after', 5)
            print(f"⚠️ Telegram rate limit. Retry after {retry_after}s")
            import time; time.sleep(retry_after)
        else:
            print(f"❌ HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"❌ {e}")

def log_deal_alert(deal_data: dict):
    """Логирует отправленный алерт для precision measurement."""
    import hashlib
    key = f"{deal_data.get('sku', '')}|{deal_data.get('store', '')}|{deal_data.get('price', 0)}"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO deal_alerts (fingerprint, canonical_variant_id, matched_variant_id,
                                         store_id, price, deal_score, confidence, classification, reason, sku)
                SELECT :fingerprint, :canon_id, :match_id, s.id, :price, :score, :conf, :class, :reason, :sku
                FROM stores s
                WHERE s.name = :store
                ON CONFLICT (sku, store_id, sent_date) DO NOTHING
            """), {
                'fingerprint': fingerprint,
                'canon_id': deal_data.get('canonical_id'),
                'match_id': deal_data.get('matched_id'),
                'store': deal_data.get('store'),
                'price': deal_data.get('price'),
                'score': deal_data.get('deal_score'),
                'conf': deal_data.get('confidence'),
                'class': deal_data.get('classification'),
                'reason': deal_data.get('reason'),
                'sku': deal_data.get('sku')
            })
    except Exception as e:
        print(f"⚠️  Failed to log deal alert: {e}")

def check_and_notify(min_deal_score: int = 70, min_confidence: int = 50):
    """Использует Deal Engine v2 + 24h cooldown + Fake Discount Analysis."""
    from src.deal_engine import calculate_deal_score_v2

    engine = create_engine(DATABASE_URL)
    notifications = []

    with engine.connect() as conn:
        canon_ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT canonical_variant_id FROM product_matches"
        )).fetchall()]

        historical_data = {}
        hist_rows = conn.execute(text("""
            SELECT variant_id,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price) as median,
                   PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY price) as p10,
                   COUNT(*) as intervals,
                   EXTRACT(DAY FROM (NOW() - MAX(started_at))) as days_since
            FROM price_changes
            GROUP BY variant_id
        """)).fetchall()
        for r in hist_rows:
            historical_data[r[0]] = {
                'median': float(r[1]) if r[1] else 0,
                'percentile_10': float(r[2]) if r[2] else 0,
                'total_intervals': r[3],
                'days_since_update': r[4] or 0,
                'total_days': r[4] or 0
            }

        for canon_id in canon_ids:
            rows = conn.execute(text("""
                SELECT o.current_price, o.old_price, o.in_stock, s.name,
                       p.canonical_name, pv.sku, pv.normalized_size, pv.normalized_color,
                       b.name, o.url, pv.id
                FROM offers o
                JOIN stores s ON o.store_id = s.id
                JOIN product_variants pv ON o.variant_id = pv.id
                JOIN products p ON pv.product_id = p.id
                JOIN brands b ON p.brand_id = b.id
                WHERE o.variant_id = :id
                UNION
                SELECT o.current_price, o.old_price, o.in_stock, s.name,
                       p.canonical_name, pv.sku, pv.normalized_size, pv.normalized_color,
                       b.name, o.url, pv.id
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
                'brand': r[8], 'url': r[9], 'variant_id': r[10],
                'canonical_id': canon_id
            } for r in rows]

            in_stock = [p for p in prices_data if p['in_stock']]
            if not in_stock:
                continue

            hist = historical_data.get(canon_id, {})
            metrics = calculate_deal_score_v2(prices_data, hist, conn)

            if metrics['deal_score'] >= min_deal_score and metrics['confidence'] >= min_confidence:
                best = min(in_stock, key=lambda x: x['price'])
                notifications.append({**best, **metrics})

    notifications.sort(key=lambda x: x['deal_score'], reverse=True)

    # P1-28: 24h cooldown
    with engine.connect() as conn:
        recent_alerts = conn.execute(text("""
            SELECT canonical_variant_id, MIN(price)
            FROM deal_alerts
            WHERE sent_at > NOW() - INTERVAL '24 hours'
            GROUP BY canonical_variant_id
        """)).fetchall()
        cooldown_map = {r[0]: float(r[1]) for r in recent_alerts if r[1]}

    filtered_notifications = []
    for d in notifications:
        canon_id = d.get('canonical_id')
        if canon_id in cooldown_map:
            if d['price'] >= cooldown_map[canon_id] * 0.95:
                continue
        filtered_notifications.append(d)

    print(f"📱 Found {len(notifications)} deals, {len(filtered_notifications)} after 24h cooldown")

    for d in filtered_notifications[:5]:
        size_str = d.get('size') or 'N/A'
        sku_str = d.get('sku') or 'N/A'
        msg = (
            d['classification'] + "\n\n"
            "<b>" + d['brand'] + "</b>\n" + d['name'] + "\n"
            "Size: " + str(size_str) + " | SKU: <code>" + str(sku_str) + "</code>\n\n"
            "💰 <b>$" + f"{d['price']:.2f}" + "</b> @ " + d['store'] + "\n"
            "📊 Market: $" + f"{d['market_median']:.2f}" + " (" + f"{d['discount_pct']:.0f}" + "% off)\n"
            "🎯 Score: " + str(d['deal_score']) + "/100 | Confidence: " + str(d['confidence']) + "%\n"
        )

        da = d.get('discount_analysis')
        if da:
            if da.get('is_fake'):
                msg += "⚠️ PERMANENT SALE (" + f"{da['duration_days']:.0f}" + " days)\n"
            elif da.get('is_real'):
                msg += "🔥 REAL SALE (" + f"{da['duration_days']:.0f}" + " days)\n"

        msg += "\n💡 " + d['reason'] + "\n"
        msg += "🔗 " + d['url']

        send_telegram_message(msg)
        log_deal_alert(d)

def send_stale_store_alerts(
    stale_hours: int = 24,
    very_stale_hours: int = 72,
    dead_hours: int = 168
):
    """
    Отправляет алерты о магазинах с устаревшими данными.
    Категории: fresh (<6h), stale (6-24h), very stale (24-72h), dead (>72h)
    """
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        problem_stores = conn.execute(text("""
            SELECT name, domain, sync_status, last_sync, last_successful_sync,
                   last_error, products_count,
                   EXTRACT(EPOCH FROM (NOW() - COALESCE(last_successful_sync, last_sync))) / 3600 AS hours_since_sync
            FROM stores
            WHERE sync_status IN ('error', 'empty')
               OR (last_successful_sync IS NOT NULL
                   AND EXTRACT(EPOCH FROM (NOW() - last_successful_sync)) / 3600 > :stale_hours)
               OR (last_successful_sync IS NULL AND last_sync IS NULL)
            ORDER BY hours_since_sync DESC NULLS LAST
        """), {'stale_hours': stale_hours}).fetchall()

        if not problem_stores:
            print("✅ All stores are fresh")
            return

        errors = [s for s in problem_stores if s[2] in ('error', 'empty')]
        dead = [s for s in problem_stores if s[7] and s[7] > dead_hours]
        very_stale = [s for s in problem_stores if s[7] and very_stale_hours < s[7] <= dead_hours]
        stale = [s for s in problem_stores if s[7] and stale_hours < s[7] <= very_stale_hours]

        msg_parts = ["🚨 <b>STORE FRESHNESS ALERT</b>\n"]

        if errors:
            msg_parts.append(f"❌ <b>Errors ({len(errors)}):</b>")
            for name, domain, status, _, _, error, _, _ in errors[:5]:
                err_short = (error or 'unknown')[:60]
                msg_parts.append(f"  • {name}: {err_short}")
            if len(errors) > 5:
                msg_parts.append(f"  <i>...and {len(errors) - 5} more</i>")
            msg_parts.append("")

        if dead:
            msg_parts.append(f"💀 <b>Dead >{dead_hours//24}d ({len(dead)}):</b>")
            for name, domain, _, _, _, _, _, hours in dead[:5]:
                msg_parts.append(f"  • {name} ({hours/24:.1f}d)")
            if len(dead) > 5:
                msg_parts.append(f"  <i>...and {len(dead) - 5} more</i>")
            msg_parts.append("")

        if very_stale:
            msg_parts.append(f"⚠️ <b>Very Stale {very_stale_hours//24}-{dead_hours//24}d ({len(very_stale)}):</b>")
            for name, domain, _, _, _, _, _, hours in very_stale[:5]:
                msg_parts.append(f"  • {name} ({hours:.0f}h)")
            msg_parts.append("")

        if stale:
            msg_parts.append(f"🕐 <b>Stale {stale_hours}-{very_stale_hours}h ({len(stale)}):</b>")
            for name, domain, _, _, _, _, _, hours in stale[:10]:
                msg_parts.append(f"  • {name} ({hours:.0f}h)")

        total_stores = conn.execute(text("SELECT COUNT(*) FROM stores")).scalar()
        fresh_count = conn.execute(text("""
            SELECT COUNT(*) FROM stores
            WHERE last_successful_sync IS NOT NULL
              AND EXTRACT(EPOCH FROM (NOW() - last_successful_sync)) / 3600 <= :hours
        """), {'hours': stale_hours}).scalar()

        msg_parts.append(f"\n📊 Summary: {fresh_count}/{total_stores} fresh ({100*fresh_count/total_stores:.0f}%)")

        msg = "\n".join(msg_parts)
        print(f"Sending alert for {len(problem_stores)} problem stores...")
        send_telegram_message(msg)

def send_weekly_digest():
    """Еженедельный digest с summary по всем магазинам."""
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total_stores = conn.execute(text("SELECT COUNT(*) FROM stores")).scalar()
        fresh = conn.execute(text("""
            SELECT COUNT(*) FROM stores
            WHERE last_successful_sync IS NOT NULL
              AND EXTRACT(EPOCH FROM (NOW() - last_successful_sync)) / 3600 <= 24
        """)).scalar()
        errors = conn.execute(text("SELECT COUNT(*) FROM stores WHERE sync_status = 'error'")).scalar()
        dead = conn.execute(text("SELECT COUNT(*) FROM stores WHERE sync_status = 'dead'")).scalar()

        total_products = conn.execute(text("SELECT COUNT(*) FROM products")).scalar()
        total_offers = conn.execute(text("SELECT COUNT(*) FROM offers")).scalar()
        total_matches = conn.execute(text("SELECT COUNT(*) FROM product_matches")).scalar()

        top_deals = conn.execute(text("""
            SELECT p.canonical_name, b.name, MIN(o.current_price) AS min_price,
                   COUNT(DISTINCT o.store_id) AS store_count
            FROM offers o
            JOIN product_variants pv ON o.variant_id = pv.id
            JOIN products p ON pv.product_id = p.id
            JOIN brands b ON p.brand_id = b.id
            JOIN product_matches pm ON pv.id IN (pm.canonical_variant_id, pm.matched_variant_id)
            WHERE o.in_stock = true
            GROUP BY p.id, p.canonical_name, b.name
            HAVING COUNT(DISTINCT o.store_id) >= 3
            ORDER BY MIN(o.current_price)
            LIMIT 5
        """)).fetchall()

        msg = f"""📅 <b>WEEKLY DIGEST</b>

🏪 <b>Stores:</b>
  • Total: {total_stores}
  • Fresh (24h): {fresh} ({100*fresh/total_stores:.0f}%)
  • Errors: {errors}
  • Dead: {dead}

📦 <b>Data:</b>
  • Products: {total_products:,}
  • Offers: {total_offers:,}
  • Matches: {total_matches:,}

🔥 <b>Top Deals:</b>
"""
        for name, brand, price, stores in top_deals:
            msg += f"  • {brand} {name[:30]}... ${price:.0f} ({stores} stores)\n"

        send_telegram_message(msg)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stale":
        send_stale_store_alerts()
    elif len(sys.argv) > 1 and sys.argv[1] == "digest":
        send_weekly_digest()
    else:
        check_and_notify()

def format_deal_message_v2(deal_result: dict, product_info: dict) -> str:
    """
    Форматирует сообщение для Telegram с учетом P1-23 (взвешенная медиана)
    и P1-26 (детекция фейковых скидок)
    """
    score = deal_result.get("score", 0)
    level = deal_result.get("level", "❓ UNKNOWN")
    market_median = deal_result.get("market_median", 0)
    price_ratio = deal_result.get("price_ratio", 1.0)
    discount_tag = deal_result.get("discount_tag", "")
    is_fake = deal_result.get("is_fake_discount", False)
    discount_days = deal_result.get("discount_days", 0)
    
    # Базовая информация о продукте
    title = product_info.get("title", "Unknown Product")
    brand = product_info.get("brand", "Unknown Brand")
    current_price = product_info.get("price", 0)
    old_price = product_info.get("compare_at_price", 0)
    discount_percent = product_info.get("discount_percent", 0)
    url = product_info.get("url", "")
    
    # Эмодзи для уровня сделки
    level_emoji = {
        "🔥 HOT DEAL": "🔥",
        "✅ GOOD DEAL": "✅",
        "👍 FAIR DEAL": "👍",
        "⚠️ WEAK DEAL": "⚠️"
    }.get(level, "❓")
    
    # Формируем сообщение
    lines = [
        f"{level_emoji} <b>{level}</b> | Score: {score}/100",
        f"{discount_tag}",
        "",
        f"👟 <b>{brand}</b>",
        f"📦 {title}",
        "",
        f"💰 Цена: <b>{current_price:.2f}</b>",
    ]
    
    if old_price and old_price > current_price:
        lines.append(f"🏷 Было: <s>{old_price:.2f}</s> (-{discount_percent:.0f}%)")
    
    if market_median > 0:
        ratio_emoji = "📉" if price_ratio < 0.95 else ("📊" if price_ratio < 1.05 else "📈")
        lines.append(f"{ratio_emoji} Рыночная медиана: {market_median:.2f} (ratio: {price_ratio:.2f})")
    
    if is_fake:
        lines.append(f"⚠️ Скидка длится уже {discount_days:.0f} дней - возможен PERMANENT SALE")
    
    if url:
        lines.append(f"\n🔗 <a href=\"{url}\">Посмотреть предложение</a>")
    
    return "\n".join(lines)

def format_deal_message(deal_data: dict) -> str:
    """
    Форматирует сообщение для Telegram с P1-23: Weighted Market Median
    """
    classification = deal_data.get('classification', '❓ UNKNOWN')
    brand = deal_data.get('brand', 'Unknown')
    name = deal_data.get('name', 'Unknown Product')
    sku = deal_data.get('sku', 'N/A')
    best_price = deal_data.get('best_price', 0)
    best_store = deal_data.get('best_store', 'Unknown')
    market_median = deal_data.get('market_median', 0)
    discount_pct = deal_data.get('discount_pct', 0)
    deal_score = deal_data.get('deal_score', 0)
    confidence = deal_data.get('confidence', 0)
    store_count = deal_data.get('store_count', 0)
    in_stock_count = deal_data.get('in_stock_count', 0)
    reason = deal_data.get('reason', '')
    time_at_price_hours = deal_data.get('time_at_price_hours')
    discount_tag = deal_data.get('discount_tag', '💰 DEAL')
    
    # Эмодзи для классификации
    emoji_map = {
        '🔥 VERY GOOD DEAL': '🔥',
        '🟢 GOOD DEAL': '✅',
        '🟡 NORMAL PRICE': '⚠️',
        '🔴 BAD DEAL': '❌'
    }
    emoji = emoji_map.get(classification, '❓')
    
    lines = [
        f"{emoji} <b>{classification}</b>",
        f"{discount_tag}",
        f"🏷️ <b>{brand}</b>",
        f"📦 {name}",
        f"📦 SKU: <code>{sku}</code>",
        "",
        f"💰 <b>Best Price: ${best_price:.2f}</b> @ {best_store}",
        f"📊 Market Median: ${market_median:.2f} ({discount_pct:.0f}% off)",
        "",
        f"🎯 Deal Score: {deal_score}/100 | Confidence: {confidence}%",
        f"🏪 Stores: {store_count} ({in_stock_count} in stock)",
    ]
    
    # P1-25: Time-at-price
    if time_at_price_hours is not None:
        if time_at_price_hours < 24:
            lines.append(f"⚡ Цена держится всего {time_at_price_hours:.0f} ч — свежее снижение!")
        elif time_at_price_hours < 72:
            lines.append(f"🕐 Цена держится {time_at_price_hours/24:.1f} дн")
        else:
            lines.append(f"🕰 Цена держится {time_at_price_hours/24:.0f} дн — стабильная")
    
    lines.extend([
        "",
        f"💡 {reason}"
    ])
    
    return "\n".join(lines)
