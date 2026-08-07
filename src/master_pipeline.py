"""
Master Pipeline v2: автоматический запуск всех этапов.
- Advisory lock (P0-13): защита от параллельных запусков.
- Early exit (P0-15): если критический шаг упал — остальные не запускаются.
- Pipeline state logging (P1-33): каждая итерация сохраняется в pipeline_runs.
Запуск: python -m src.master_pipeline
"""
import subprocess
import time
import sys
import os
from datetime import datetime
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

LOCK_KEY = 0x50524943  # "PRIC" in hex

def ensure_pipeline_runs_table():
    """Создаёт таблицу pipeline_runs если её нет."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                finished_at TIMESTAMP WITH TIME ZONE,
                duration_seconds INTEGER,
                status VARCHAR(20) NOT NULL,
                steps_completed INTEGER DEFAULT 0,
                steps_total INTEGER DEFAULT 0,
                error_message TEXT
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started
            ON pipeline_runs(started_at DESC)
        """))

def acquire_lock(conn):
    """Пытается получить advisory lock, возвращает True если успешно."""
    result = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
    return result.fetchone()[0]

def release_lock(conn):
    """Освобождает advisory lock."""
    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})

def run_step(name: str, command: str, run_id: int = None) -> bool:
    """Запускает шаг и возвращает успех."""
    print(f"\n{'='*80}")
    print(f"🚀 {name}")
    print(f"{'='*80}")
    
    start = time.time()
    env = os.environ.copy()
    if run_id:
        env['PIPELINE_RUN_ID'] = str(run_id)
    result = subprocess.run(command, shell=True, capture_output=False, env=env)
    elapsed = time.time() - start
    
    if result.returncode == 0:
        print(f"✅ {name} completed in {elapsed:.1f}s")
        return True
    else:
        print(f"❌ {name} failed with code {result.returncode}")
        return False

def main():
    print(f"🕐 Starting master pipeline v2 at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ensure_pipeline_runs_table()
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        if not acquire_lock(conn):
            print("⚠️  Another pipeline is already running (advisory lock held). Exiting.")
            sys.exit(0)
        
        steps = [
            ("Batch Import (152 Shopify sites)", "python -m src.batch_import_fast", True),
            ("Normalize Sizes (incremental)", "python scripts/normalize_sizes.py", False),
            ("Normalize Colors (incremental)", "python scripts/normalize_colors.py", False),
            ("Calculate Store Reliability", "python scripts/calculate_store_reliability.py", False),
            ("Product Matching v2", "python -m src.match_products", True),
            ("Deal Engine Analysis", "python -m src.deal_engine", False),
            ("Telegram Notifications", "python -m src.telegram_notifier", False),
            ("Stale Store Alerts", "python -m src.telegram_notifier stale", False),
        ]
        
        run_id = None
        try:
            result = conn.execute(text("""
                INSERT INTO pipeline_runs (status, steps_total)
                VALUES ('running', :total) RETURNING id
            """), {"total": len(steps)})
            conn.commit()
            run_id = result.fetchone()[0]
        except Exception as e:
            print(f"⚠️  Failed to create pipeline_runs entry: {e}")
            conn.rollback()
        
        start_total = time.time()
        try:
            results = []
            completed = 0
            for name, cmd, critical in steps:
                success = run_step(name, cmd, run_id)
                results.append((name, success))
                if success:
                    completed += 1
                elif critical:
                    print(f"❌ Critical step '{name}' failed. Aborting remaining steps.")
                    break
            
            total_time = time.time() - start_total
            status = "completed" if completed == len(steps) else "partial" if completed > 0 else "failed"
            
            print(f"\n{'='*80}")
            print(f"📊 PIPELINE SUMMARY")
            print(f"{'='*80}")
            for name, success in results:
                status_icon = "✅" if success else "❌"
                print(f"{status_icon} {name}")
            
            print(f"\n⏱️  Total time: {total_time:.1f}s")
            print(f"🕐 Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            if run_id:
                try:
                    conn.execute(text("""
                        UPDATE pipeline_runs
                        SET finished_at = NOW(),
                            duration_seconds = :duration,
                            status = :status,
                            steps_completed = :completed
                        WHERE id = :run_id
                    """), {
                        "duration": int(total_time),
                        "status": status,
                        "completed": completed,
                        "run_id": run_id
                    })
                    conn.commit()
                except Exception as e:
                    print(f"⚠️  Failed to update pipeline_runs: {e}")
        finally:
            release_lock(conn)

if __name__ == "__main__":
    main()
