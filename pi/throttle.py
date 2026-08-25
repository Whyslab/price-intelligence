"""Rate limiting for outbound requests, per host and across the platform.

Shopify enforces two different limits and they need two different answers.

*Per shop.* Individual shops throttle harder than others, and at any aggregate
rate some of them will answer 429 while the rest are perfectly happy — measured
directly: during a sweep that was collecting 429s, kith.com and feature.com
still answered 200 to a single request. Slowing everything down because one shop
is strict wastes the whole sweep's budget; that shop alone should back off.

*Per IP, across the whole platform.* Push hard enough and every shop starts
refusing at once, for minutes, with no Retry-After — measured at 58 of 138
stores lost in one sweep. Nothing shop-specific can see that coming.

So each host gets its own bucket, and a global bucket sits behind them as the
platform backstop. The global rate only tightens when 429s arrive from several
distinct hosts at once, which is what a platform-level block looks like and what
a single strict shop does not.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger(__name__)

# How many distinct hosts must complain inside PLATFORM_WINDOW before we read it
# as a platform-wide block rather than a few strict shops.
PLATFORM_HOSTS = 4
PLATFORM_WINDOW = 30.0


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
        rate: float = 6.0,
        per_host_rate: float = 0.5,
        min_rate: float = 0.25,
        cooldown: float = 60.0,
        recover_after: float = 30.0,
    ):
        # Defaults come from measurement: a single global 2 req/s was gentle in
        # aggregate yet still hammered individual shops hard enough to earn 429s,
        # while unrelated shops answered fine throughout. Being slow per shop
        # (one request every two seconds) and quicker overall fits what the
        # platform actually enforces, and the backstop below catches the rest.
        self._global = _Bucket(rate, min_rate)
        self._hosts: dict[str, _Bucket] = {}
        self.per_host_rate = per_host_rate
        self.min_rate = min_rate
        self.cooldown = cooldown
        self.recover_after = recover_after
        self._lock = asyncio.Lock()
        self._recent: deque[tuple[float, str]] = deque()
        self.penalties = 0

    @property
    def rate(self) -> float:
        """The current global rate, for logging and tests."""
        return self._global.rate

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
                if len(distinct) < PLATFORM_HOSTS:
                    log.info(
                        "%s is rate limiting us — backing off that shop for %.0fs", host, wait
                    )
                    return
                log.warning(
                    "%d shops rate limited us within %.0fs — this looks platform-wide, "
                    "slowing the whole sweep", len(distinct), PLATFORM_WINDOW,
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

    async def penalise(self, pause: float | None = None, host: str = "") -> None:
        return
