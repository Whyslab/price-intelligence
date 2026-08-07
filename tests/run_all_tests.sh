#!/bin/bash
# Run all regression tests
set -e

cd "$(dirname "$0")/.."

echo "=== Running Currency Tests ==="
python tests/test_currency_normalizer.py

echo ""
echo "=== Running Price Sanity Tests ==="
python tests/test_price_sanity.py

echo ""
echo "=== Running Price Changes Tests ==="
python tests/test_price_changes.py

echo ""
echo "=== Running Matching Tests ==="
python tests/test_matching.py
echo ""
echo "=== Running Color Tests ==="
python tests/test_color_consistency.py

echo ""
echo "🎉 All regression tests passed"
