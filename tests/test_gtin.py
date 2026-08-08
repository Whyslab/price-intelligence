"""GTIN tests #20, #95"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestGTINChecksum:
    """#20: Checksum validation"""
    def test_valid_ean13(self):
        from src.validators.gtin_validator import is_valid_gtin
        assert is_valid_gtin("4006381333931")  # Valid EAN-13
    
    def test_invalid_checksum(self):
        from src.validators.gtin_validator import is_valid_gtin
        assert not is_valid_gtin("4006381333932")  # Invalid
    
    def test_invalid_format(self):
        from src.validators.gtin_validator import is_valid_gtin
        assert not is_valid_gtin("123")  # Too short
        assert not is_valid_gtin("abc")  # Not digits
        assert not is_valid_gtin(None)
    
    def test_gtin_types(self):
        from src.validators.gtin_validator import get_gtin_type
        assert get_gtin_type("12345678") == "EAN-8"
        assert get_gtin_type("123456789012") == "UPC-A"
        assert get_gtin_type("4006381333931") == "EAN-13"
