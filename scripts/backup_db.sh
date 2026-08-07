#!/bin/bash
# Daily PostgreSQL backup + optional restore-test (P0-12, P0-30)
# Usage:
#   ./backup_db.sh              # только backup
#   ./backup_db.sh --test       # backup + restore-test
set -euo pipefail

BACKUP_DIR="$HOME/backups"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
DUMP="$BACKUP_DIR/price_intelligence_$STAMP.dump"

pg_dump -Fc price_intelligence -f "$DUMP"

# Retention: последние 7 дампов (используем find, а не ls — избегаем алиасов eza)
find "$BACKUP_DIR" -maxdepth 1 -name 'price_intelligence_*.dump' -printf '%T@ %p\n' \
  | sort -rn | tail -n +8 | cut -d' ' -f2- | xargs -r rm --

SIZE=$(du -h "$DUMP" | cut -f1)
echo "[$(date '+%F %T')] backup ok: price_intelligence_$STAMP.dump ($SIZE)"

# === Restore-test (опционально) ===
if [ "${1:-}" = "--test" ]; then
    echo "--- restore-test start ---"
    TEST_DB="restore_test_$$"
    
    if psql -lqt | cut -d \| -f 1 | grep -qw "$TEST_DB"; then
        dropdb "$TEST_DB"
    fi
    createdb "$TEST_DB"
    
    if ! pg_restore -d "$TEST_DB" "$DUMP" 2>&1 | tail -5; then
        echo "❌ pg_restore failed"
        dropdb "$TEST_DB" 2>/dev/null || true
        exit 1
    fi
    
    echo "--- counts: restore_test vs prod ---"
    for db in "$TEST_DB" price_intelligence; do
        psql -d "$db" -Atc "
            SELECT '$db' AS db,
                   (SELECT COUNT(*) FROM offers) AS offers,
                   (SELECT COUNT(*) FROM price_changes) AS price_changes,
                   (SELECT COUNT(*) FROM product_matches) AS matches,
                   (SELECT COUNT(*) FROM stores) AS stores
        "
    done
    
    dropdb "$TEST_DB"
    echo "--- restore-test OK ---"
fi
