"""GTIN/EAN/UPC Checksum Validation

Uses python-stdnum library for reliable validation.
"""
from stdnum import ean, upc, gtin as gtin_module
from typing import Optional


def validate_gtin_checksum(gtin: str) -> bool:
    """Validates GTIN checksum using stdnum library"""
    if not gtin or not str(gtin).strip():
        return False
    
    gtin = str(gtin).strip()
    
    try:
        # Try EAN-13/EAN-8
        if len(gtin) in [8, 13]:
            ean.validate(gtin)
            return True
        # Try UPC-A (12 digits)
        elif len(gtin) == 12:
            upc.validate(gtin)
            return True
        # Try GTIN-14
        elif len(gtin) == 14:
            gtin_module.validate(gtin)
            return True
        else:
            return False
    except Exception:
        return False


def is_valid_gtin(gtin) -> bool:
    """Full GTIN validation"""
    return validate_gtin_checksum(gtin) if gtin else False


def get_gtin_type(gtin: str) -> Optional[str]:
    """Returns GTIN type or None"""
    if not gtin or not str(gtin).isdigit():
        return None
    
    l = len(str(gtin))
    return {
        8: "EAN-8",
        12: "UPC-A", 
        13: "EAN-13",
        14: "GTIN-14"
    }.get(l)
