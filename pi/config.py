"""Configuration: environment for secrets, filters.toml for alerting rules.

Importing this module has no side effects — no I/O, no printing, no raising.
Everything happens inside load_config(), so tests and tooling can import freely.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Filters:
    """Rules deciding which discounts are worth a notification."""

    min_discount_pct: float = 30.0
    min_saving_usd: float = 40.0
    min_price_usd: float = 25.0
    max_price_usd: float = 2000.0
    min_score: int = 55
    max_alerts_per_run: int = 15
    # A "sale" whose struck-through price has not moved for this many days is
    # permanent pricing dressed up as a discount.
    fake_sale_days: int = 21
    brands_allow: tuple[str, ...] = ()
    brands_deny: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()

    def wants_brand(self, brand: str | None) -> bool:
        b = (brand or "").strip().lower()
        if self.brands_deny and any(d in b for d in self.brands_deny):
            return False
        if self.brands_allow:
            return any(a in b for a in self.brands_allow)
        return True

    def wants_size(self, size_norm: str | None) -> bool:
        if not self.sizes:
            return True
        return (size_norm or "") in self.sizes


@dataclass(frozen=True)
class Config:
    db_path: Path
    sites_file: Path
    bot_token: str | None
    chat_id: str | None
    concurrency: int
    # Two limits, because Shopify enforces two: shopify_rate is the whole
    # sweep's budget, shopify_host_rate is what any single shop gets. See pi.throttle.
    shopify_rate: float
    shopify_host_rate: float
    log_level: str
    filters: Filters = field(default_factory=Filters)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.bot_token and self.chat_id)


def _resolve(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


def load_filters(path: Path) -> Filters:
    """Read filters.toml. A missing file means "use the defaults"."""
    if not path.exists():
        return Filters()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f for f in Filters.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"{path}: unknown option(s) {sorted(unknown)}. Known: {sorted(known)}"
        )
    for key in ("brands_allow", "brands_deny"):
        if key in data:
            data[key] = tuple(str(v).strip().lower() for v in data[key])
    if "sizes" in data:
        data["sizes"] = tuple(str(v).strip().upper() for v in data["sizes"])
    return Filters(**data)


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or ROOT / ".env")
    filters_file = _resolve(os.getenv("PI_FILTERS_FILE", "filters.toml"))
    return Config(
        db_path=_resolve(os.getenv("PI_DB_PATH", "data/pi.db")),
        sites_file=_resolve(os.getenv("PI_SITES_FILE", "data/sites.txt")),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        concurrency=int(os.getenv("PI_CONCURRENCY", "8")),
        shopify_rate=float(os.getenv("PI_SHOPIFY_RATE", "6.0")),
        shopify_host_rate=float(os.getenv("PI_SHOPIFY_HOST_RATE", "0.5")),
        log_level=os.getenv("PI_LOG_LEVEL", "INFO").upper(),
        filters=load_filters(filters_file),
    )
