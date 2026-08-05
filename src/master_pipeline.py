"""
Master Pipeline: автоматический запуск всех этапов.
Запуск: python -m src.master_pipeline
"""
import subprocess
import time
from datetime import datetime

def run_step(name: str, command: str) -> bool:
    """Запускает шаг и возвращает успех."""
    print(f"\n{'='*80}")
    print(f"🚀 {name}")
    print(f"{'='*80}")
    
    start = time.time()
    result = subprocess.run(command, shell=True, capture_output=False)
    elapsed = time.time() - start
    
    if result.returncode == 0:
        print(f"✅ {name} completed in {elapsed:.1f}s")
        return True
    else:
        print(f"❌ {name} failed with code {result.returncode}")
        return False

def main():
    print(f"🕐 Starting master pipeline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    steps = [
        ("Batch Import (152 Shopify sites)", "python -m src.batch_import_fast"),
        ("Product Matching", "python -m src.match_products"),
        ("Deal Engine Analysis", "python -m src.deal_engine"),
        ("Telegram Notifications", "python -m src.telegram_notifier"),
    ]
    
    results = []
    for name, cmd in steps:
        success = run_step(name, cmd)
        results.append((name, success))
    
    print(f"\n{'='*80}")
    print(f"📊 PIPELINE SUMMARY")
    print(f"{'='*80}")
    
    for name, success in results:
        status = "✅" if success else "❌"
        print(f"{status} {name}")
    
    total_time = time.time() - start_total
    print(f"\n⏱️  Total time: {total_time:.1f}s")
    print(f"🕐 Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    start_total = time.time()
    main()
