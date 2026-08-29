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
    # One shop running "-40% on everything" produced twelve consecutive
    # notifications, all at the same percentage. That is one fact, not twelve.
    max_alerts_per_store: int = 3
    # A "sale" whose struck-through price has not moved for this many days is
    # permanent pricing dressed up as a discount.
    fake_sale_days: int = 21
    # The window for "the lowest price this shop actually charged before the
    # drop" — the EU's formula, and the one thing a price inflated last week
    # cannot survive.
    reference_window_days: int = 30
    # How many *other* shops must stock the same article before their prices are
    # treated as a market price, and before their tags imply a recommended one.
    market_min_shops: int = 3
    msrp_min_shops: int = 3
    # A struck-through price this far above the recommended one is the shop's
    # invention, not the brand's.
    inflated_tag_pct: float = 15.0
    # Share of a shop's discounts that land on a round 5% step before its
    # struck-through prices are read as arithmetic rather than as former prices.
    # Measured across 78 shops: shops pricing individually sit near 55%, shops
    # applying "-40% to this category" sit at 100%.
    rule_priced_share: float = 0.9
    # Share of a shop's catalogue sitting at one identical discount, round or
    # not, before the same conclusion is drawn.
    blanket_sale_share: float = 0.3
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
    # Shopify's per-IP quota tolerates a few dozen stores at a time, so a sweep
    # takes a slice per run rather than charging at all of them and being cut
    # off. With least-recently-collected ordering the slices cover everything.
    max_shopify_stores: int
    log_level: str
    # Article numbers to be told about whatever the thresholds say. A missing
    # file simply means nothing is being watched, which is the usual case.
    watchlist_file: Path = ROOT / "data" / "watchlist.txt"
    # Where `pi web` can be reached from a phone, if anywhere. Unset by default
    # and the bot then simply has no button for it: the server binds to
    # localhost, and offering a link to a machine the reader is not sitting at
    # is worse than offering nothing.
    web_url: str | None = None
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
        watchlist_file=_resolve(os.getenv("PI_WATCHLIST_FILE", "data/watchlist.txt")),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        web_url=(os.getenv("PI_WEB_URL") or "").strip().rstrip("/") or None,
        concurrency=int(os.getenv("PI_CONCURRENCY", "8")),
        shopify_rate=float(os.getenv("PI_SHOPIFY_RATE", "2.0")),
        shopify_host_rate=float(os.getenv("PI_SHOPIFY_HOST_RATE", "0.5")),
        max_shopify_stores=int(os.getenv("PI_MAX_SHOPIFY_STORES", "45")),
        log_level=os.getenv("PI_LOG_LEVEL", "INFO").upper(),
        filters=load_filters(filters_file),
    )
