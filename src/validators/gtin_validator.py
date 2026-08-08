"""GTIN/EAN/UPC Checksum Validation

Uses standard GTIN checksum algorithm (works for EAN-8, UPC-A, EAN-13, GTIN-14).
"""
from typing import Optional


def _compute_check_digit(digits_13: str) -> int:
    """
    Computes GTIN check digit from first 13 digits.
    
    Algorithm (GS1 standard):
    - Position 1, 3, 5, ... (odd, 1-indexed) = weight 1
    - Position 2, 4, 6, ... (even, 1-indexed) = weight 3
    
    For 14-digit GTIN padded from shorter:
    - 0-indexed positions 0,2,4,6,8,10,12 have weight 1
    - 0-indexed positions 1,3,5,7,9,11 have weight 3
    """
    total = 0
    for i, ch in enumerate(digits_13):
        digit = int(ch)
        # For GTIN-14: weights alternate 1,3,1,3,...
        weight = 1 if i % 2 == 0 else 3
        total += digit * weight
    
    check = (10 - (total % 10)) % 10
    return check


def validate_gtin_checksum(gtin: str) -> bool:
    """
    Validates GTIN checksum (EAN-8, UPC-A, EAN-13, GTIN-14).
    
    All these formats use the same check digit algorithm after
    padding to 14 digits (zero-pad on the left).
    """
    if not gtin:
        return False
    
    gtin = str(gtin).strip()
    
    # Must be all digits
    if not gtin.isdigit():
        return False
    
    # Valid lengths
    if len(gtin) not in (8, 12, 13, 14):
        return False
    
    # Pad to 14 digits
    padded = gtin.zfill(14)
    
    # Compute expected check digit from first 13 digits
    expected = _compute_check_digit(padded[:13])
    actual = int(padded[13])
    
    return expected == actual


def is_valid_gtin(gtin) -> bool:
    """Full GTIN validation (format + checksum)."""
    return validate_gtin_checksum(gtin) if gtin else False


def get_gtin_type(gtin: str) -> Optional[str]:
    """Returns GTIN type based on length."""
    if not gtin or not str(gtin).strip().isdigit():
        return None
    
    l = len(str(gtin).strip())
    return {
        8: "EAN-8",
        12: "UPC-A",
        13: "EAN-13",
        14: "GTIN-14"
    }.get(l)
