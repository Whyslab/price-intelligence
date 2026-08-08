"""GTIN/EAN/UPC Checksum Validation"""

def validate_gtin_checksum(gtin: str) -> bool:
    """Validates GTIN checksum (EAN-8/13, UPC-A, GTIN-14)"""
    if not gtin or not str(gtin).strip().isdigit():
        return False
    gtin = str(gtin).strip()
    if len(gtin) not in [8, 12, 13, 14]:
        return False
    padded = gtin.zfill(14)
    total = sum(int(padded[i]) * (1 if i % 2 == 0 else 3) for i in range(13))
    check = (10 - (total % 10)) % 10
    return check == int(padded[13])

def is_valid_gtin(gtin) -> bool:
    """Full GTIN validation"""
    return validate_gtin_checksum(gtin) if gtin else False

def get_gtin_type(gtin: str):
    """Returns GTIN type or None"""
    if not gtin or not str(gtin).isdigit():
        return None
    l = len(str(gtin))
    return {8: "EAN-8", 12: "UPC-A", 13: "EAN-13", 14: "GTIN-14"}.get(l)
