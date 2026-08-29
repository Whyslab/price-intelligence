"""Rate limiting: pace each shop, and the whole sweep only when it must."""
from __future__ import annotations

import asyncio
import time

import pytest

from pi.throttle import MIN_ATTEMPTS_BEFORE_BLOCK, RateLimiter


def give_it_a_sample(limiter):
    """A sweep must attempt a fair number of requests before the breaker may
    conclude anything — otherwise its first two seconds decide for it."""
    for _ in range(MIN_ATTEMPTS_BEFORE_BLOCK):
        limiter.note_attempt()


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
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")

        assert limiter.rate == 50.0, "the whole sweep slows down"
        assert limiter.penalties == 4

    async def test_three_strict_shops_are_not_a_platform_block(self):
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.01)
        give_it_a_sample(limiter)
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
        give_it_a_sample(limiter)
        for n in range(40):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.rate == 0.5

    async def test_the_global_rate_recovers_once_complaints_stop(self):
        limiter = RateLimiter(
            rate=100.0, per_host_rate=100.0, cooldown=0, recover_after=0.01
        )
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.rate == 50.0

        await asyncio.sleep(0.02)
        await limiter.acquire("fresh.example")
        assert limiter.rate > 50.0

    async def test_an_explicit_pause_is_honoured(self):
        """Jitter may stretch a pause but must never shorten one: a shop that
        sent Retry-After named the number, and coming back early is not ours to
        choose."""
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=10.0)
        await limiter.penalise(pause=0.05, host="shop.example")
        start = time.monotonic()
        await limiter.acquire("shop.example")
        assert 0.05 <= time.monotonic() - start < 1.0


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


class TestInFlight:
    """Shopify refuses parallel requests from one IP; the rate is not the issue."""

    async def test_only_one_request_is_in_flight_at_a_time(self):
        limiter = RateLimiter(rate=10_000.0, per_host_rate=10_000.0)
        overlap = 0
        current = 0

        async def request(host):
            nonlocal overlap, current
            async with limiter.slot(host):
                current += 1
                overlap = max(overlap, current)
                await asyncio.sleep(0.01)
                current -= 1

        await asyncio.gather(*(request(f"shop{n}.example") for n in range(8)))
        assert overlap == 1, "eight concurrent callers must still go one at a time"

    async def test_the_slot_is_released_even_when_the_request_raises(self):
        limiter = RateLimiter(rate=10_000.0, per_host_rate=10_000.0)
        with pytest.raises(RuntimeError):
            async with limiter.slot("shop.example"):
                raise RuntimeError("network died")
        # If the slot leaked, this would hang forever.
        await asyncio.wait_for(limiter.acquire("shop.example"), timeout=1.0)
        async with limiter.slot("shop.example"):
            pass

    async def test_max_inflight_is_configurable(self):
        limiter = RateLimiter(rate=10_000.0, per_host_rate=10_000.0, max_inflight=3)
        overlap = 0
        current = 0

        async def request(host):
            nonlocal overlap, current
            async with limiter.slot(host):
                current += 1
                overlap = max(overlap, current)
                await asyncio.sleep(0.02)
                current -= 1

        await asyncio.gather(*(request(f"shop{n}.example") for n in range(6)))
        assert overlap == 3


class TestPlatformBreaker:
    """Once Shopify blocks the IP, going slower does not help — only stopping does."""

    async def test_the_breaker_trips_when_many_hosts_refuse(self):
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.01)
        give_it_a_sample(limiter)
        assert not limiter.blocked

        for n in range(3):
            await limiter.penalise(host=f"shop{n}.example")
        assert not limiter.blocked, "three strict shops are not a platform block"

        await limiter.penalise(host="shop3.example")
        assert limiter.blocked

    async def test_repeats_from_one_shop_never_trip_the_breaker(self):
        limiter = RateLimiter(rate=100.0, per_host_rate=100.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for _ in range(50):
            await limiter.penalise(host="strict.example")
        assert not limiter.blocked


class TestBreakerNeedsASuccessDrought:
    """Refusals alone are not a block — a healthy sweep has them too.

    Measured: fifteen stores collected successfully while three different shops
    rate-limited us inside one second. During a real block, nothing succeeded
    at all.
    """

    async def test_refusals_alongside_successes_do_not_trip_it(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(6):
            limiter.note_success(f"working{n}.example")
            await limiter.penalise(host=f"strict{n}.example")
        assert not limiter.blocked
        assert limiter.rate == 1000.0

    async def test_refusals_with_nothing_getting_through_trip_it(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked

    async def test_a_success_before_the_window_does_not_count(self, monkeypatch):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        limiter.note_success("long-ago.example")
        # Pretend that success happened well outside the window.
        limiter._last_success -= 120.0
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked


class TestTheConvoy:
    """Shops refused in the same second must not come back in the same second.

    The live failure: eight shops were refused at 00:24:41, each paused for
    exactly 60s, and four of them woke together at 00:25:42. Nothing had been
    allowed to succeed during that minute, so the breaker read a drought and
    abandoned seventeen stores — one second before six of those same shops
    returned 200.
    """

    async def test_identical_pauses_come_back_at_different_times(self):
        limiter = RateLimiter(rate=10_000.0, per_host_rate=10_000.0, cooldown=0.05)
        start = time.monotonic()
        for n in range(8):
            await limiter.penalise(host=f"shop{n}.example")

        async def when(host):
            await limiter.acquire(host)
            return time.monotonic() - start

        waits = await asyncio.gather(*(when(f"shop{n}.example") for n in range(8)))
        assert min(waits) >= 0.05, "nobody comes back early"
        assert max(waits) - min(waits) > 0.005, "and they do not come back together"

    async def test_a_request_getting_through_lifts_the_block(self):
        """A platform-wide refusal cannot produce a success. One arriving means
        the breaker was reading a convoy, and the sweep should carry on."""
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked

        limiter.note_success("shop0.example")
        assert not limiter.blocked

    async def test_a_real_block_stays_tripped(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(8):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked, "nothing got through, so the block holds"


class TestConfirmingABlock:
    """Tripping the breaker must not fail the queue in the same instant.

    Live evidence: the block was declared at 10:45:14, fifteen queued stores were
    marked "skipped: Shopify blocked this IP" inside that second, and at 10:45:30
    one of them returned 750 products. The stores that had not started yet are
    the ones a revocation can still save, so they wait.
    """

    async def test_a_waiter_is_released_when_a_request_gets_through(self, monkeypatch):
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 5.0)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked

        async def a_request_lands():
            await asyncio.sleep(0.05)
            limiter.note_success("slow.example")

        start = time.monotonic()
        confirmed, _ = await asyncio.gather(limiter.confirm_blocked(), a_request_lands())
        assert confirmed is False, "one success is proof the platform is not refusing all"
        assert time.monotonic() - start < 1.0, "and the waiter leaves at once"

    async def test_a_real_block_is_confirmed_once_the_window_passes(self, monkeypatch):
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 0.1)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert await limiter.confirm_blocked() is True

    async def test_waiters_are_released_together_not_one_by_one(self, monkeypatch):
        """The cost of a genuine block is one window for the whole run."""
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 0.2)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")

        start = time.monotonic()
        results = await asyncio.gather(*(limiter.confirm_blocked() for _ in range(15)))
        assert all(results)
        assert time.monotonic() - start < 0.6, "fifteen waiters, one window"

    async def test_giving_up_is_remembered_even_after_the_block_lifts(self, monkeypatch):
        """A run that abandoned nineteen stores and then recovered must not
        report a clean sheet."""
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 0.05)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            await limiter.penalise(host=f"shop{n}.example")
        assert await limiter.confirm_blocked() is True

        limiter.note_success("late.example")
        assert not limiter.blocked, "the block itself is lifted"
        assert limiter.abandoned == 1, "but the store it cost is still counted"

    async def test_nothing_to_confirm_when_there_is_no_block(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0)
        assert await limiter.confirm_blocked() is False


class TestSilenceIsNotEvidence:
    """A drought only counts if the sweep was actually asking.

    Live evidence (29.08, run of eight shops): six of them refused inside one
    second sixteen seconds in, each sat out a 60-95s pause, and during that quiet
    the breaker declared the platform blocked. It had not been refused during the
    quiet — it had asked nobody. All six then delivered their catalogues in full,
    3,247 products, `stores ok 8 / failed 0`.
    """

    async def test_refusals_during_a_lull_do_not_trip_the_breaker(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        # Plenty of requests overall, but none of them recent: this is a sweep
        # whose shops are all serving out their back-off.
        give_it_a_sample(limiter)
        limiter._tried.clear()
        for n in range(6):
            await limiter.penalise(host=f"shop{n}.example")
        assert not limiter.blocked, "nobody was asking, so nobody was refused"

    async def test_refusals_while_the_sweep_is_asking_still_trip_it(self):
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            limiter.note_attempt(f"shop{n}.example")
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked, "asked four hosts, refused by four hosts"


class TestTheWindowProbes:
    """A suspected block is a hypothesis, and the window is where it is tested.

    Before this, `confirm_blocked` refused every caller for the whole window, so
    the success that would revoke the block could only come from a request
    already in flight — and at one request in flight, usually none was. Measured
    over 28-29.08: thirteen blocks declared, none revoked, while /products.json
    was answering 200 again inside a minute.
    """

    async def test_a_waiter_is_let_through_to_ask(self, monkeypatch):
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 5.0)
        monkeypatch.setattr("pi.throttle.PROBE_EVERY", 0.05)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            limiter.note_attempt(f"shop{n}.example")
            await limiter.penalise(host=f"shop{n}.example")
        assert limiter.blocked

        start = time.monotonic()
        assert await limiter.confirm_blocked() is False, "let through to try"
        assert time.monotonic() - start < 1.0
        assert limiter.blocked, "and the block still stands until one gets through"
        assert limiter.abandoned == 0, "being sent to ask is not giving up"

    async def test_only_one_waiter_probes_per_interval(self, monkeypatch):
        """Ten stores waiting must not all charge at a platform that may really
        be refusing — that is the volley the breaker exists to stop."""
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 30.0)
        monkeypatch.setattr("pi.throttle.PROBE_EVERY", 0.3)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            limiter.note_attempt(f"shop{n}.example")
            await limiter.penalise(host=f"shop{n}.example")

        tasks = [asyncio.create_task(limiter.confirm_blocked()) for _ in range(10)]
        done, pending = await asyncio.wait(tasks, timeout=0.5)
        for task in pending:
            task.cancel()
        assert [t.result() for t in done] == [False], "one prober in one interval"

    async def test_a_successful_probe_frees_the_whole_sweep(self, monkeypatch):
        monkeypatch.setattr("pi.throttle.CONFIRM_WINDOW", 5.0)
        monkeypatch.setattr("pi.throttle.PROBE_EVERY", 0.05)
        limiter = RateLimiter(rate=1000.0, per_host_rate=1000.0, cooldown=0.001)
        give_it_a_sample(limiter)
        for n in range(4):
            limiter.note_attempt(f"shop{n}.example")
            await limiter.penalise(host=f"shop{n}.example")

        async def the_prober():
            assert await limiter.confirm_blocked() is False
            limiter.note_success("shop0.example")   # the platform is answering

        waiters = [limiter.confirm_blocked() for _ in range(5)]
        results = await asyncio.gather(the_prober(), *waiters)
        assert results[1:] == [False] * 5, "everyone carries on"
        assert limiter.abandoned == 0, "and no store was given up on"
