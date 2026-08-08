"""GTIN/EAN/UPC Checksum Validation

Uses standard GS1 GTIN checksum algorithm.
"""
from typing import Optional


def validate_gtin_checksum(gtin: str) -> bool:
    """
    Validates GTIN checksum (EAN-8, UPC-A, EAN-13, GTIN-14).
    
    GS1 Standard Algorithm:
    - All formats: odd positions (1-indexed) = weight 1, even positions = weight 3
    - Sum all weighted digits (excluding check digit)
    - Check digit = (10 - (sum % 10)) % 10
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
    
    # Convert to list of digits
    digits = [int(d) for d in gtin]
    
    # Calculate checksum
    total = 0
    for i in range(len(digits) - 1):  # Exclude check digit
        # 0-indexed position i corresponds to 1-indexed position (i+1)
        # Odd positions (1-indexed) = weight 1, even = weight 3
        weight = 1 if (i + 1) % 2 == 1 else 3
        total += digits[i] * weight
    
    # Check digit
    expected_check = (10 - (total % 10)) % 10
    actual_check = digits[-1]
    
    return expected_check == actual_check


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
