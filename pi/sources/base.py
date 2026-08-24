"""Shared shapes every adapter produces, plus size normalisation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class ScrapedVariant:
    external_id: str
    price: float                       # in the store's own currency
    compare_at: float | None = None    # struck-through price, same currency
    in_stock: bool = True
    sku: str | None = None
    size: str | None = None
    color: str | None = None

    @property
    def size_norm(self) -> str | None:
        return normalize_size(self.size)


@dataclass(slots=True)
class ScrapedProduct:
    external_id: str
    title: str
    url: str
    brand: str | None = None
    image_url: str | None = None
    category: str | None = None
    variants: list[ScrapedVariant] = field(default_factory=list)


@dataclass(slots=True)
class FetchResult:
    """What one pass over one store produced."""

    domain: str
    products: list[ScrapedProduct] = field(default_factory=list)
    currency: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


_CLOTHING = {
    "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL",
    "OS", "ONE SIZE", "OSFA",
}
_NUM = re.compile(r"(\d+(?:[.,]5)?)")


def normalize_size(raw: str | None) -> str | None:
    """Best-effort canonical size: 'US 10.5' -> 'US10.5', 'EUR 44' -> 'EU44', 'Large' -> 'L'.

    Sizing is genuinely inconsistent across stores, so this only cleans up the
    obvious cases and leaves anything it does not recognise uppercased. It is
    used for filtering, never for matching products between shops.
    """
    if not raw:
        return None
    s = raw.strip().upper().replace("½", ".5")
    if not s or s in {"DEFAULT TITLE", "TITLE", "N/A", "-"}:
        return None

    s = re.sub(r"\b(LARGE)\b", "L", s)
    s = re.sub(r"\b(MEDIUM)\b", "M", s)
    s = re.sub(r"\b(SMALL)\b", "S", s)
    if s in _CLOTHING:
        return "OS" if s in {"ONE SIZE", "OSFA"} else s

    region = None
    if re.search(r"\b(US|USA|AMERICAN)\b", s):
        region = "US"
    elif re.search(r"\b(EU|EUR|EURO|EUROPEAN)\b", s):
        region = "EU"
    elif re.search(r"\b(UK|BRITISH)\b", s):
        region = "UK"

    match = _NUM.search(s)
    if not match:
        return s[:20] or None
    number = match.group(1).replace(",", ".")
    if "." in number:  # 10.0 -> 10, 10.50 -> 10.5, but 10 must stay 10
        number = number.rstrip("0").rstrip(".")
    if region:
        return f"{region}{number}"
    # A bare number in the 35-50 range is almost always European sizing.
    try:
        value = float(number)
    except ValueError:
        return s[:20]
    return f"EU{number}" if 35 <= value <= 50 else f"US{number}"
