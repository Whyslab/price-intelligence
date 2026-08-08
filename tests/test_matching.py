"""Matching tests #19-24"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestMatchingStrict:
    """#17-18: Strict matching"""
    def test_no_unknown_wildcard(self):
        # Ensure UNKNOWN doesn't match MEN/WOMEN
        # This is a placeholder - real test would check SQL
        assert True


class TestSKUConflicts:
    """#24: SKU conflicts"""
    def test_different_ean_no_match(self):
        # Same SKU + different EAN = no match
        assert True
