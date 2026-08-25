"""Currency conversion, including what happens when the rate API is down."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from pi import fx


def test_conversion_uses_the_right_direction():
    rates = fx.Rates({"EUR": 0.86, "GBP": 0.73}, datetime.now(UTC), "test")
    usd, rate = rates.to_usd(86.0, "EUR")
    assert usd == 100.0
    assert rate == 0.86
    assert rates.to_usd(100.0, "usd") == (100.0, 1.0)


def test_an_unknown_currency_is_refused_not_assumed():
    """Treating 1,300,000 KRW as dollars is the failure this prevents."""
    rates = fx.Rates({"EUR": 0.86}, datetime.now(UTC), "test")
    assert rates.to_usd(1_300_000, "KRW") is None
    assert rates.to_usd(100.0, None) is None
    assert rates.to_usd(100.0, "") is None


@respx.mock
def test_rates_are_fetched_and_cached(tmp_path):
    route = respx.get(fx.API_URL).mock(
        return_value=httpx.Response(200, json={"base": "USD", "rates": {"EUR": 0.85, "GBP": 0.73}})
    )
    cache = tmp_path / "fx.json"

    first = fx.load_rates(cache)
    assert first.source == "frankfurter"
    assert cache.exists()

    second = fx.load_rates(cache)
    assert second.source == "cache"
    assert route.call_count == 1, "a fresh cache must not hit the network again"
    assert second.to_usd(85.0, "EUR") == (100.0, 0.85)


@respx.mock
def test_a_stale_cache_beats_no_rates_at_all(tmp_path):
    cache = tmp_path / "fx.json"
    long_ago = datetime.now(UTC) - timedelta(days=9)
    cache.write_text(json.dumps({"fetched_at": long_ago.isoformat(), "rates": {"EUR": 0.9}}))
    respx.get(fx.API_URL).mock(side_effect=httpx.ConnectError("offline"))

    rates = fx.load_rates(cache)
    assert rates.source == "stale-cache"
    assert rates.to_usd(90.0, "EUR") == (100.0, 0.9)


@respx.mock
def test_built_in_rates_are_the_last_resort(tmp_path):
    respx.get(fx.API_URL).mock(side_effect=httpx.ConnectError("offline"))
    rates = fx.load_rates(tmp_path / "missing.json")
    assert rates.source == "fallback"
    assert rates.to_usd(100.0, "USD") == (100.0, 1.0)
    assert rates.to_usd(138_400, "KRW")[0] == 100.0


@respx.mock
def test_a_malformed_reply_does_not_crash_the_run(tmp_path):
    respx.get(fx.API_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    assert fx.load_rates(tmp_path / "fx.json").source == "fallback"
