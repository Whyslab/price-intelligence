"""Currency tests #25-30"""
import pytest
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCurrencyConversion:
    """#5: Correct math"""
    def test_eur_to_usd(self):
        from src.currency_normalizer import convert_to_usd
        rates = {'EUR': Decimal('0.92')}
        usd, _, _ = convert_to_usd(Decimal('100'), 'EUR', rates)
        assert abs(float(usd) - 108.70) < 0.5
    
    def test_gbp_to_usd(self):
        from src.currency_normalizer import convert_to_usd
        rates = {'GBP': Decimal('0.79')}
        usd, _, _ = convert_to_usd(Decimal('100'), 'GBP', rates)
        assert abs(float(usd) - 126.58) < 0.5


class TestCNY:
    """#29: CNY support"""
    def test_cny_supported(self):
        from src.currency_normalizer import SUPPORTED_CURRENCIES
        assert 'CNY' in SUPPORTED_CURRENCIES
