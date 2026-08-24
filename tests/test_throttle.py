"""The shared rate limit that keeps a sweep under Shopify's per-IP budget."""
from __future__ import annotations

import asyncio
import time

import pytest

from pi.throttle import NullLimiter, RateLimiter


async def test_requests_are_spaced_by_the_configured_rate():
    limiter = RateLimiter(rate=50.0)          # 20ms apart
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 4 * 0.02 * 0.9          # four gaps between five requests


async def test_the_budget_is_shared_across_concurrent_tasks():
    """Sixteen stores in parallel must share one budget, not get one each."""
    limiter = RateLimiter(rate=50.0)
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(10)))
    assert time.monotonic() - start >= 9 * 0.02 * 0.9


async def test_a_429_slows_everyone_down_not_just_the_caller():
    limiter = RateLimiter(rate=100.0, cooldown=0.05)
    await limiter.penalise()

    assert limiter.rate == 50.0               # halved
    assert limiter.penalties == 1

    start = time.monotonic()
    await limiter.acquire()                   # a task that never saw the 429
    assert time.monotonic() - start >= 0.04   # still made to wait out the cooldown


async def test_the_rate_never_falls_below_the_floor():
    limiter = RateLimiter(rate=2.0, min_rate=0.5, cooldown=0)
    for _ in range(10):
        await limiter.penalise()
    assert limiter.rate == 0.5


async def test_the_rate_recovers_once_the_throttling_stops():
    limiter = RateLimiter(rate=100.0, cooldown=0, recover_after=0.01)
    await limiter.penalise()
    assert limiter.rate == 50.0

    await asyncio.sleep(0.02)
    await limiter.acquire()
    assert limiter.rate > 50.0


async def test_an_explicit_pause_is_honoured():
    limiter = RateLimiter(rate=1000.0, cooldown=10.0)
    await limiter.penalise(pause=0.05)        # e.g. a Retry-After header
    start = time.monotonic()
    await limiter.acquire()
    waited = time.monotonic() - start
    assert 0.04 <= waited < 1.0               # the explicit pause, not the 10s default


async def test_the_null_limiter_does_nothing():
    limiter = NullLimiter()
    start = time.monotonic()
    await limiter.acquire()
    await limiter.penalise()
    assert time.monotonic() - start < 0.01


@pytest.mark.parametrize("rate", [0.5, 2.0, 10.0])
async def test_any_configured_rate_is_respected(rate):
    limiter = RateLimiter(rate=rate * 100)    # scaled up to keep the test quick
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - start >= (1.0 / (rate * 100)) * 0.9
