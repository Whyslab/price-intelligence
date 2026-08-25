"""Rate limiting for outbound requests, per host and across the platform.

Shopify enforces two different limits and they need two different answers.

*Concurrency is what it actually objects to.* Measured directly against eight
live shops, with everything else held constant:

    sequential, one request per 2s   ->  200 200 200 200
    eight requests at once           ->  429 429 429 429 429 429 429 429
    three requests at once           ->  429 429 429

Rate was not the trigger and neither was page size — six requests in a row at
limit=250 all returned 200 from the same shops that were refusing the sweep at
that very moment. Parallel requests from one IP are what Shopify refuses, across
its whole platform. So the limiter allows exactly one Shopify request in flight
at a time (`slot()`), and paces those in-flight requests with a token bucket.

*Once blocked, nothing helps but stopping.* Measured after a sweep tripped it:
requests spaced two seconds apart, strictly one at a time, still returned 429
from every shop — the same shape of request that had succeeded twenty minutes
earlier. The block is platform-wide, outlasts twenty minutes, and cannot be
negotiated down by going slower. Continuing to probe only feeds it.

So the limiter trips a breaker. Refusals alone are not the signal: a healthy
sweep of fifteen stores had three different shops rate-limit us within one
second and still collected all fifteen. What distinguishes a block is that
*nothing* is getting through — during one, not a single request succeeded. The
breaker therefore needs both several distinct hosts refusing and a total absence
of successes over the same window. The caller then abandons the Shopify part of
the run and retries on the next timer, rather than spending half an hour
collecting 429s and keeping the block alive.

*Per shop, and overall.* Each host also gets its own bucket so no one shop is
hit repeatedly in quick succession, and a global bucket paces the sweep.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager

log = logging.getLogger(__name__)

# How many distinct hosts must complain inside PLATFORM_WINDOW before we read it
# as a platform-wide block rather than a few strict shops.
PLATFORM_HOSTS = 4
PLATFORM_WINDOW = 30.0
# A sweep starts with no successes recorded, which is indistinguishable from
# "nothing is getting through" unless it is given a fair sample first. Observed:
# three shops (grailssf, extrabutterny, academyandco) refuse the very first
# request every time, so a sweep whose opening requests land on them would
# otherwise abandon all 138 stores two seconds in.
MIN_ATTEMPTS_BEFORE_BLOCK = 12


class _Bucket:
    """One token bucket: hands out slots no faster than `rate` per second."""

    __slots__ = ("base_rate", "last_penalty", "min_rate", "next_slot", "rate")

    def __init__(self, rate: float, min_rate: float):
        self.rate = self.base_rate = rate
        self.min_rate = min_rate
        self.next_slot = 0.0
        self.last_penalty = 0.0

    def claim(self, now: float, recover_after: float) -> float:
        if (
            self.rate < self.base_rate
            and self.last_penalty
            and now - self.last_penalty > recover_after
        ):
            self.rate = min(self.base_rate, self.rate * 1.5)
            self.last_penalty = now
        slot = max(now, self.next_slot)
        self.next_slot = slot + 1.0 / self.rate
        return slot - now

    def penalise(self, now: float, pause: float) -> None:
        self.rate = max(self.min_rate, self.rate / 2)
        self.last_penalty = now
        self.next_slot = max(self.next_slot, now + pause)


class RateLimiter:
    """Per-host buckets plus a global backstop. Safe to share across tasks."""

    def __init__(
        self,
        rate: float = 2.0,
        per_host_rate: float = 0.5,
        min_rate: float = 0.25,
        cooldown: float = 60.0,
        recover_after: float = 30.0,
        max_inflight: int = 1,
    ):
        # 2 req/s overall is the one configuration observed to complete a full
        # 196-store sweep with zero refusals. It was raised to 6 on the strength
        # of a conclusion that later proved wrong, and is back where the evidence
        # puts it. Per-shop stays slow so no single shop is hit in bursts.
        self._global = _Bucket(rate, min_rate)
        self._hosts: dict[str, _Bucket] = {}
        self.per_host_rate = per_host_rate
        self.min_rate = min_rate
        self.cooldown = cooldown
        self.recover_after = recover_after
        self._lock = asyncio.Lock()
        # Shopify refuses parallel requests from one IP, so only this many may be
        # in flight at once. One is what the measurement supports.
        self._inflight = asyncio.Semaphore(max_inflight)
        self._recent: deque[tuple[float, str]] = deque()
        self._last_success = 0.0
        self.attempts = 0
        self.penalties = 0
        self.successes = 0
        self.blocked_at: float | None = None

    @property
    def rate(self) -> float:
        """The current global rate, for logging and tests."""
        return self._global.rate

    @property
    def blocked(self) -> bool:
        """True once the platform has started refusing everything.

        The caller should stop making Shopify requests for the rest of the run.
        Going slower does not lift it and continuing to probe prolongs it.
        """
        return self.blocked_at is not None

    def note_attempt(self, host: str = "") -> None:
        """Record that a request was made, successful or not."""
        self.attempts += 1

    def note_success(self, host: str = "") -> None:
        """Record that a request got through. Cheap, and it is what keeps the
        breaker from tripping on a sweep that is merely bumpy."""
        self._last_success = time.monotonic()
        self.successes += 1

    def _bucket(self, host: str) -> _Bucket:
        bucket = self._hosts.get(host)
        if bucket is None:
            bucket = self._hosts[host] = _Bucket(self.per_host_rate, self.min_rate)
        return bucket

    async def acquire(self, host: str = "") -> None:
        """Wait until this caller may make its request to `host`."""
        async with self._lock:
            now = time.monotonic()
            delay = self._global.claim(now, self.recover_after)
            if host:
                delay = max(delay, self._bucket(host).claim(now, self.recover_after))
        if delay > 0:
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def slot(self, host: str = ""):
        """Wait for a turn, then hold the only in-flight slot for the request.

        Use this around the HTTP call itself, not just before it: the point is
        that no second Shopify request overlaps this one.
        """
        await self.acquire(host)
        async with self._inflight:
            yield

    async def penalise(self, pause: float | None = None, host: str = "") -> None:
        """Called on a 429. Slows `host`, and the whole sweep only if many complain."""
        async with self._lock:
            now = time.monotonic()
            wait = self.cooldown if pause is None else pause
            self.penalties += 1

            if host:
                self._bucket(host).penalise(now, wait)
                while self._recent and now - self._recent[0][0] > PLATFORM_WINDOW:
                    self._recent.popleft()
                self._recent.append((now, host))
                distinct = {h for _, h in self._recent}
                # A shop refusing while others succeed is just a strict shop.
                nothing_working = (
                    self.successes == 0
                    or now - self._last_success > PLATFORM_WINDOW
                )
                too_early = self.attempts < MIN_ATTEMPTS_BEFORE_BLOCK
                if len(distinct) < PLATFORM_HOSTS or not nothing_working or too_early:
                    log.info(
                        "%s is rate limiting us — backing off that shop for %.0fs", host, wait
                    )
                    return
                if self.blocked_at is None:
                    self.blocked_at = now
                    log.error(
                        "%d shops refused us within %.0fs — Shopify has blocked this IP "
                        "platform-wide. Going slower does not lift it, so the rest of the "
                        "Shopify sweep is being abandoned; it will retry on the next run.",
                        len(distinct), PLATFORM_WINDOW,
                    )

            self._global.penalise(now, wait)
            log.warning(
                "global rate now %.2f req/s, pausing %.0fs", self._global.rate, wait
            )


class NullLimiter:
    """No-op limiter, for tests and for callers that do their own pacing."""

    penalties = 0
    rate = float("inf")

    async def acquire(self, host: str = "") -> None:
        return

    def note_attempt(self, host: str = "") -> None:
        return

    def note_success(self, host: str = "") -> None:
        return

    @asynccontextmanager
    async def slot(self, host: str = ""):
        yield

    async def penalise(self, pause: float | None = None, host: str = "") -> None:
        return
