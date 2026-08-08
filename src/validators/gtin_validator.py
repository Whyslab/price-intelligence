"""GTIN/EAN/UPC Checksum Validation"""

def validate_gtin_checksum(gtin: str) -> bool:
    """Validates GTIN checksum (EAN-8/13, UPC-A, GTIN-14)"""
    if not gtin or not str(gtin).strip().isdigit():
        return False
    gtin = str(gtin).strip()
    if len(gtin) not in [8, 12, 13, 14]:
        return False
    
    # Pad to 14 digits
    padded = gtin.zfill(14)
    
    # Calculate checksum: positions 1,3,5,... (1-indexed) multiply by 1
    # positions 2,4,6,... multiply by 3
    # In 0-indexed: positions 0,2,4,... (even) multiply by 1
    # positions 1,3,5,... (odd) multiply by 3
    total = 0
    for i in range(13):
        digit = int(padded[i])
        if i % 2 == 0:  # 0-indexed even = 1-indexed odd
            total += digit * 1
        else:  # 0-indexed odd = 1-indexed even
            total += digit * 3
    
    check_digit = (10 - (total % 10)) % 10
    return check_digit == int(padded[13])

def is_valid_gtin(gtin) -> bool:
    """Full GTIN validation"""
    return validate_gtin_checksum(gtin) if gtin else False

def get_gtin_type(gtin: str):
    """Returns GTIN type or None"""
    if not gtin or not str(gtin).isdigit():
        return None
    l = len(str(gtin))
    return {8: "EAN-8", 12: "UPC-A", 13: "EAN-13", 14: "GTIN-14"}.get(l)
