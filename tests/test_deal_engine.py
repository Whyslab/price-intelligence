"""Deal Engine tests #13, #16"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCanonicalDealEngine:
    """#13: Canonical engine"""
    def test_canonical_exists(self):
        from src.canonical_deal_engine import calculate_canonical_deal_score
        assert callable(calculate_canonical_deal_score)
    
    def test_empty_prices(self):
        from src.canonical_deal_engine import calculate_canonical_deal_score
        result = calculate_canonical_deal_score([])
        assert result["classification"] == "NO_DATA"
    
    def test_out_of_stock(self):
        from src.canonical_deal_engine import calculate_canonical_deal_score
        result = calculate_canonical_deal_score([
            {"price": 100, "in_stock": False}
        ])
        assert result["classification"] == "OUT_OF_STOCK"
    
    def test_deal_score_range(self):
        from src.canonical_deal_engine import calculate_canonical_deal_score
        result = calculate_canonical_deal_score([
            {"price": 100, "store_id": 1, "in_stock": True},
            {"price": 120, "store_id": 2, "in_stock": True},
        ])
        assert 0 <= result["deal_score"] <= 100
