"""Currency conversion to USD.

Rates come from frankfurter.dev (ECB data, no API key, no account). They are
cached on disk for a day. If the API is unreachable the cache is used past its
expiry, and only if there is no cache at all do the built-in rates apply.

An unknown currency is never silently treated as USD — that turns a 1,300,000 KRW
sneaker into a 1,300,000 dollar one. Convert() returns None and the caller drops
the price.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.frankfurter.dev/v1/latest"
CACHE_TTL = timedelta(hours=24)

# Last-resort rates, 1 USD = N units. Only used on a cold start with no network.
# Wrong by a few percent is fine here; wrong by an order of magnitude is not.
FALLBACK_RATES: dict[str, float] = {
    "USD": 1.0, "EUR": 0.86, "GBP": 0.73, "JPY": 159.0, "KRW": 1384.0,
    "CAD": 1.38, "AUD": 1.53, "CHF": 0.80, "SEK": 9.6, "NOK": 10.2,
    "DKK": 6.4, "PLN": 3.69, "CZK": 21.0, "HUF": 340.0, "TRY": 40.0,
    "SGD": 1.29, "HKD": 7.8, "NZD": 1.68, "CNY": 7.1, "BGN": 1.68, "RON": 4.35,
}


class Rates:
    """1 USD = self.rates[code] units of that currency."""

    def __init__(self, rates: dict[str, float], fetched_at: datetime, source: str):
        self.rates = {k.upper(): float(v) for k, v in rates.items()}
        self.rates.setdefault("USD", 1.0)
        self.fetched_at = fetched_at
        self.source = source
        # Prices thrown away for want of a rate, by the currency they named.
        # Counted rather than logged one by one: www.ssense.com's Saudi pages
        # quote "USE", which is no currency, and wrote 536 identical warnings a
        # day. The run logs the tally once (see pipeline.run).
        self.dropped: Counter[str] = Counter()

    def to_usd(self, amount: float, currency: str | None) -> tuple[float, float] | None:
        """Return (usd_amount, rate_used), or None if the currency is unknown."""
        if not currency:
            return None
        code = currency.upper()
        rate = self.rates.get(code)
        if not rate or rate <= 0:
            self.dropped[code] += 1
            return None
        return round(amount / rate, 2), rate

    def __contains__(self, currency: str) -> bool:
        return currency.upper() in self.rates


def _read_cache(path: Path) -> tuple[dict[str, float], datetime] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw["rates"], datetime.fromisoformat(raw["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(path: Path, rates: dict[str, float], fetched_at: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": fetched_at.isoformat(), "rates": rates}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write rate cache %s: %s", path, exc)


def load_rates(cache_path: Path, client: httpx.Client | None = None) -> Rates:
    now = datetime.now(UTC)
    cached = _read_cache(cache_path)
    if cached and now - cached[1] < CACHE_TTL:
        return Rates(cached[0], cached[1], "cache")

    owned = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(API_URL, params={"base": "USD"})
        resp.raise_for_status()
        rates = resp.json()["rates"]
        rates["USD"] = 1.0
        _write_cache(cache_path, rates, now)
        return Rates(rates, now, "frankfurter")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if cached:
            log.warning("rate API unavailable (%s) — using cache from %s", exc, cached[1])
            return Rates(cached[0], cached[1], "stale-cache")
        log.warning("rate API unavailable (%s) and no cache — using built-in rates", exc)
        return Rates(FALLBACK_RATES, now, "fallback")
    finally:
        if owned:
            client.close()
