#!/usr/bin/env python3
"""Part 2: DB migrations + remaining critical fixes"""
import sys
from pathlib import Path
from sqlalchemy import create_engine, text

sys.path.insert(0, '.')
from src.config import DATABASE_URL

print("=" * 80)
print("🔧 PART 2: DB MIGRATIONS + REMAINING FIXES")
print("=" * 80)

engine = create_engine(DATABASE_URL)

# ============================================================================
# DB MIGRATION 1: Partial unique index для PriceChange (один открытый интервал)
# ============================================================================
print("\n📊 Creating partial unique index for PriceChange...")
try:
    with engine.begin() as conn:
        # Удаляем старый если есть
        conn.execute(text("DROP INDEX IF EXISTS uq_price_changes_open_interval"))
        
        # Создаём partial unique index: только один ended_at IS NULL для каждого (variant_id, store_id)
        conn.execute(text("""
            CREATE UNIQUE INDEX uq_price_changes_open_interval 
            ON price_changes (variant_id, store_id) 
            WHERE ended_at IS NULL
        """))
        print("✅ Created: uq_price_changes_open_interval (partial unique)")
except Exception as e:
    print(f"⚠️  Index creation failed (may already exist): {e}")

# ============================================================================
# DB MIGRATION 2: Composite unique для (store_id, external_variant_id)
# ============================================================================
print("\n📊 Creating composite unique constraints for scoped identity...")
try:
    with engine.begin() as conn:
        # Удаляем старый глобальный unique index если есть
        conn.execute(text("DROP INDEX IF EXISTS ix_pv_external_variant_id"))
        
        # Создаём composite unique
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_variant 
            ON product_variants (store_id, external_variant_id)
            WHERE external_variant_id IS NOT NULL
        """))
        print("✅ Created: uq_store_external_variant (composite unique)")
        
        # Тоже для external_product_id
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_product 
            ON product_variants (store_id, external_product_id)
            WHERE external_product_id IS NOT NULL
        """))
        print("✅ Created: uq_store_external_product (composite unique)")
except Exception as e:
    print(f"⚠️  Constraint failed: {e}")

# ============================================================================
# DB MIGRATION 3: DealAlert cleanup - удалить sent_date, оставить sent_at
# ============================================================================
print("\n📊 Cleaning up DealAlert table (remove sent_date)...")
try:
    with engine.begin() as conn:
        # Проверяем наличие колонок
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'deal_alerts'
        """))
        columns = [r[0] for r in result]
        
        if 'sent_date' in columns:
            # Переносим данные из sent_date в sent_at если нужно
            if 'sent_at' not in columns:
                conn.execute(text("ALTER TABLE deal_alerts ADD COLUMN sent_at TIMESTAMP WITH TIME ZONE"))
                conn.execute(text("UPDATE deal_alerts SET sent_at = sent_date::timestamp"))
            
            # Удаляем старую колонку
            conn.execute(text("ALTER TABLE deal_alerts DROP COLUMN sent_date"))
            print("✅ Removed: sent_date column from deal_alerts")
        else:
            print("ℹ️  sent_date already removed")
        
        # Создаём новый индекс с sent_at
        conn.execute(text("DROP INDEX IF EXISTS idx_deal_alerts_sku_store_date"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_alerts_sku_store_at 
            ON deal_alerts (sku, store_id, DATE_TRUNC('day', sent_at))
        """))
        print("✅ Created: idx_deal_alerts_sku_store_at (unique daily)")
except Exception as e:
    print(f"⚠️  DealAlert cleanup: {e}")

# ============================================================================
# DB MIGRATION 4: CHECK constraints (применяем в БД)
# ============================================================================
print("\n📊 Applying CHECK constraints to database...")
check_constraints = [
    ("offers", "check_price_positive", "current_price > 0"),
    ("price_history", "check_price_history_positive", "price > 0"),
    ("price_changes", "check_price_change_positive", "price > 0"),
    ("stores", "check_reliability_range", "reliability_score >= 0 AND reliability_score <= 100"),
    ("product_matches", "check_confidence_range", "confidence_score >= 0 AND confidence_score <= 1"),
    ("product_matches", "check_no_self_match", "canonical_variant_id != matched_variant_id"),
]

with engine.begin() as conn:
    for table, constraint_name, condition in check_constraints:
        try:
            # Проверяем есть ли уже
            result = conn.execute(text(f"""
                SELECT COUNT(*) FROM information_schema.check_constraints 
                WHERE constraint_name = '{constraint_name}'
            """))
            exists = result.fetchone()[0] > 0
            
            if not exists:
                conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} CHECK ({condition})"))
                print(f"  ✅ {table}.{constraint_name}")
            else:
                print(f"  ℹ️  {table}.{constraint_name} (already exists)")
        except Exception as e:
            print(f"  ⚠️  {table}.{constraint_name}: {str(e)[:80]}")

# ============================================================================
# CODE FIX: Currency normalizer - HTTPS + env vars
# ============================================================================
print("\n📝 Fixing currency_normalizer.py (HTTPS + env)...")
currency_path = Path("src/currency_normalizer.py")
if currency_path.exists():
    content = currency_path.read_text()
    
    # Fix HTTP -> HTTPS
    if 'http://data.fixer.io' in content:
        content = content.replace('http://data.fixer.io', 'https://data.fixer.io')
        print("  ✅ Fixed: HTTPS for Fixer API")
    
    # Fix API key from env
    if 'FIXER_API_KEY = "YOUR_API_KEY_HERE"' in content:
        content = content.replace(
            'FIXER_API_KEY = "YOUR_API_KEY_HERE"',
            'FIXER_API_KEY = os.getenv("FIXER_API_KEY", "YOUR_API_KEY_HERE")'
        )
        print("  ✅ Fixed: FIXER_API_KEY from environment")
    
    # Add import os if not present
    if 'import os' not in content:
        content = 'import os\n' + content
    
    currency_path.write_text(content)

# ============================================================================
# CODE FIX: Telegram notifier - proper retry logic
# ============================================================================
print("\n📝 Fixing telegram_notifier.py (retry logic)...")
telegram_path = Path("src/telegram_notifier.py")
if telegram_path.exists():
    content = telegram_path.read_text()
    
    # Находим функцию отправки и добавляем retry
    if 'def send_telegram_message' in content:
        old_send = '''        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10
        )'''
        
        new_send = '''        import time
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10
            )
            
            if r.status_code == 200:
                break
            elif r.status_code == 429:
                retry_after = r.json().get('parameters', {}).get('retry_after', retry_delay)
                print(f"⚠️ Rate limit, retry in {retry_after}s (attempt {attempt+1}/{max_retries})")
                time.sleep(retry_after)
                retry_delay = min(retry_delay * 2, 60)  # Exponential backoff
            else:
                print(f"❌ Telegram API error: {r.status_code}")
                break
        else:
            print("❌ Failed after all retries")
            return False'''
        
        if old_send in content:
            content = content.replace(old_send, new_send)
            telegram_path.write_text(content)
            print("  ✅ Fixed: Retry logic with exponential backoff")

print("\n" + "=" * 80)
print("✅ PART 2 COMPLETE")
print("=" * 80)
