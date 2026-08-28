"""What it costs to actually have the thing, not what the price tag says.

A −50% shoe from Los Angeles and a −50% shoe from Berlin are not the same offer
when you live in Norway, and the difference is not small: shipping and 25% VAT
turn a $120 find into $180. Presenting only the shop's price hides exactly the
part that decides whether to buy.

Three things this deliberately does not do.

It does not decide what counts as a discount. The thresholds in `filters.toml`
stay on the price of the item. Delivery is an estimate — there is no API that
will tell you what a shop charges to ship — and an estimate that rough must not
silently discard real finds. It costs the offer points and it is shown; it does
not veto.

It does not pick a country for you. Both destinations are on the card, because
the sum is often what decides where to send something, and hiding half the
answer behind a button means switching back and forth on every offer.

It does not pretend to be a customs calculator. It is a good-faith arithmetic on
published rates, and it says so. The rates were checked against the tax
authorities on 28.08.2026 and they change.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# Which shipping bracket a shop's country falls in. Not a geography lesson — the
# only question is roughly what a parcel from there costs and whether it crosses
# a customs border on the way.
_EU = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK",
}
_ASIA = {"AU", "CN", "HK", "ID", "JP", "KR", "MY", "NZ", "PH", "SG", "TH", "TW", "VN"}


def region_of(country: str | None) -> str:
    code = (country or "").strip().upper()
    if code in _EU:
        return "EU"
    if code == "GB":
        return "GB"
    if code == "US":
        return "US"
    if code in _ASIA:
        return "ASIA"
    return "OTHER"


@dataclass(frozen=True)
class Landed:
    """One destination's answer for one offer. Every figure in US dollars."""

    destination: str
    name: str
    price_usd: float
    shipping_usd: float
    duty_usd: float
    vat_usd: float
    clearance_usd: float

    @property
    def total_usd(self) -> float:
        return (
            self.price_usd + self.shipping_usd + self.duty_usd
            + self.vat_usd + self.clearance_usd
        )

    @property
    def overhead_pct(self) -> float:
        """How much the price grows on the way here. The number worth comparing."""
        if self.price_usd <= 0:
            return 0.0
        return (self.total_usd - self.price_usd) / self.price_usd * 100


@dataclass(frozen=True)
class Rules:
    """Everything shipping.toml says, in the shape the arithmetic wants."""

    destinations: dict[str, dict]
    shipping: dict[str, dict[str, float]]
    free_over: dict[str, float]
    per_shop: dict[str, dict[str, float]]

    @property
    def enabled(self) -> bool:
        return bool(self.destinations)


EMPTY = Rules(destinations={}, shipping={}, free_over={}, per_shop={})


def load_rules(path: Path | None = None) -> Rules:
    """Read shipping.toml. A missing file simply means no delivery estimate.

    Silence rather than a default guess: numbers invented by this module and
    presented as costs would be worse than saying nothing, and the file ships
    with an example to copy.
    """
    path = path or ROOT / "shipping.toml"
    if not path.exists():
        return EMPTY
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    shipping = data.get("shipping", {})
    return Rules(
        destinations=data.get("destination", {}),
        shipping=shipping.get("default", {}),
        free_over=shipping.get("free_over", {}),
        per_shop={k.lower(): v for k, v in shipping.get("shop", {}).items()},
    )


def shipping_for(
    rules: Rules, domain: str | None, country: str | None, destination: str, price_usd: float
) -> float:
    """What getting one item here is estimated to cost.

    A shop you have ordered from overrides the regional guess, because a figure
    you have checked beats four you have not.
    """
    region = region_of(country)
    override = rules.per_shop.get((domain or "").lower())
    if override is not None:
        free_over = override.get("free_over")
        if free_over is not None and price_usd >= free_over:
            return 0.0
        if destination in override:
            return float(override[destination])
    free_over = rules.free_over.get(region)
    if free_over is not None and price_usd >= float(free_over):
        return 0.0
    return float(rules.shipping.get(region, {}).get(destination, 0.0))


def _duty_rate(destination: dict, kind: str | None) -> float:
    rates = destination.get("duty_pct", {})
    return float(rates.get(kind or "unknown", rates.get("unknown", 0.0)))


def landed_for(
    rules: Rules,
    destination: str,
    price_usd: float,
    kind: str | None,
    domain: str | None,
    country: str | None,
    eur_usd: float | None = None,
) -> Landed | None:
    """The whole sum for one destination, or None if it is not configured.

    The two destinations work differently and the difference is the point.

    Norway charges 25% VAT from the first krone and 10.7% duty on clothing but
    nothing on shoes, so a jacket and a pair of trainers at the same price do
    not land at the same price.

    Ukraine charges nothing at all below €150 a shipment, and above it charges
    10% duty and 20% VAT *on the excess only* — so a €160 parcel owes about €3,
    not €48. Applying the rate to the whole value, which is the easy mistake,
    would overstate it sixteenfold and make every good find look bad.
    """
    config = rules.destinations.get(destination)
    if config is None:
        return None

    shipping = shipping_for(rules, domain, country, destination, price_usd)
    duty_rate = _duty_rate(config, kind)
    vat_rate = float(config.get("vat_pct", 0.0))
    clearance = float(config.get("clearance_fee_usd", 0.0))

    allowance = float(config.get("duty_free_usd", 0.0))
    if "duty_free_eur" in config and eur_usd:
        allowance = float(config["duty_free_eur"]) * eur_usd

    # The declared value is the goods, not the postage: both authorities set
    # their threshold on what the item cost.
    dutiable = max(0.0, price_usd - allowance)
    if dutiable <= 0:
        # Under the allowance nothing is owed — not duty, and not VAT either.
        return Landed(destination, str(config.get("name", destination)),
                      price_usd, shipping, 0.0, 0.0, 0.0)

    duty = dutiable * duty_rate / 100
    vat = (dutiable + duty) * vat_rate / 100
    return Landed(
        destination=destination,
        name=str(config.get("name", destination)),
        price_usd=price_usd,
        shipping_usd=shipping,
        duty_usd=duty,
        vat_usd=vat,
        clearance_usd=clearance,
    )


def landed_all(
    rules: Rules,
    price_usd: float,
    kind: str | None,
    domain: str | None,
    country: str | None,
    eur_usd: float | None = None,
) -> list[Landed]:
    """Every configured destination, in the order the file lists them."""
    if not rules.enabled:
        return []
    found = [
        landed_for(rules, code, price_usd, kind, domain, country, eur_usd)
        for code in rules.destinations
    ]
    return [item for item in found if item is not None]


# How much delivery is allowed to cost an offer in the queue. Capped, because
# the estimate is rough and a rough number must not be able to bury a real find
# on its own: at worst it moves a deal down the list, never off it.
MAX_PENALTY = 15


def penalty(landed: list[Landed]) -> int:
    """Points off the ordering for what the cheapest route adds to the price.

    Cheapest, not average: you get to choose where to send it, so the offer
    should be judged on the better of the two, not punished for the worse.
    """
    if not landed:
        return 0
    overhead = min(item.overhead_pct for item in landed)
    return min(MAX_PENALTY, int(overhead / 5))
