"""Rate limiting: pace each shop, and the whole sweep only when it must."""
from __future__ import annotations

import asyncio
import time

import pytest

from pi.throttle import RateLimiter


async def test_requests_to_one_host_are_spaced_by_its_rate():
    limiter = RateLimiter(rate=10_000.0, per_host_rate=50.0)   # 20ms apart
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire("shop.example")
    assert time.monotonic() - start >= 4 * 0.02 * 0.9


async def test_different_hosts_do_not_wait_on_each_other():
    """The per-host bucket is what keeps one slow shop from blocking the rest."""
    limiter = RateLimiter(rate=10_000.0, per_host_rate=2.0)    # 500ms per host
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire(f"shop{n}.example") for n in range(10)))
    assert time.monotonic() - start < 0.2, "ten different shops go at once"


async def test_the_global_budget_still_paces_the_sweep_as_a_whole():
    limiter = RateLimiter(rate=50.0, per_host_rate=10_000.0)
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire(f"shop{n}.example") for n in range(10)))
    assert time.monotonic() - start >= 9 * 0.02 * 0.9


class TestPenalties:
    async def test_one_strict_shop_slows_only_itself(self):
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.05)
        await limiter.penalise(host="strict.example")

        assert limiter.rate == 100.0, "the global budget is untouched"

        start = time.monotonic()
        await limiter.acquire("other.example")
        assert time.monotonic() - start < 0.02, "an unrelated shop is not held back"

        start = time.monotonic()
        await limiter.acquire("strict.example")
        assert time.monotonic() - start >= 0.04, "the strict one waits out its cooldown"

    async def test_many_shops_complaining_at_once_reads_as_a_platform_block(self):
        """58 of 138 stores were lost to this in one sweep — the global backstop
        exists for exactly this signal, and nothing shop-specific can see it."""
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.01)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")

        assert limiter.rate == 50.0, "the whole sweep slows down"
        assert limiter.penalties == 4

    async def test_three_strict_shops_are_not_a_platform_block(self):
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.01)
        for n in range(3):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.rate == 100.0

    async def test_one_shop_complaining_repeatedly_is_not_a_platform_block(self):
        """Otherwise a single aggressive shop drags the whole sweep to the floor,
        which is what happened live: 64 penalties, global rate pinned at 0.25."""
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.001)
        for _ in range(20):
            await limiter.penalise(host="strict.example")
        assert limiter.rate == 100.0, "distinct hosts are what counts, not repeats"

    async def test_the_rate_never_falls_below_the_floor(self):
        limiter = RateLimiter(rate=2.0, per_host_rate=2.0, min_rate=0.5, cooldown=0)
        for n in range(40):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.rate == 0.5

    async def test_the_global_rate_recovers_once_complaints_stop(self):
        limiter = RateLimiter(
            rate=100.0, per_host_rate=100.0, cooldown=0, recover_after=0.01
        )
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.rate == 50.0

        await asyncio.sleep(0.02)
        await limiter.acquire("fresh.example")
        assert limiter.rate > 50.0

    async def test_an_explicit_pause_is_honoured(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=10.0)
        await limiter.penalise(pause=0.05, host="shop.example")
        start = time.monotonic()
        await limiter.acquire("shop.example")
        assert 0.04 <= time.monotonic() - start < 1.0


async def test_the_null_limiter_does_nothing():
    from pi.throttle import NullLimiter

    limiter = NullLimiter()
    start = time.monotonic()
    await limiter.acquire("shop.example")
    await limiter.penalise(host="shop.example")
    assert time.monotonic() - start < 0.01


@pytest.mark.parametrize("rate", [0.5, 2.0, 10.0])
async def test_any_configured_rate_is_respected(rate):
    limiter = RateLimiter(rate=rate * 100, per_host_rate=10_000.0)
    start = time.monotonic()
    await limiter.acquire("a.example")
    await limiter.acquire("b.example")
    assert time.monotonic() - start >= (1.0 / (rate * 100)) * 0.9
