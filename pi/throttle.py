"""A shared rate limit for outbound requests.

Shopify's storefront limit applies to our IP across the whole platform, not per
shop. Sixteen stores fetched in parallel therefore share one budget, blow it,
and every one of them starts answering 429 — measured at 58 of 138 stores lost
in a single sweep. The 429 then persists for minutes and carries no Retry-After
header, so there is nothing useful to obey and no quick recovery.

The answer is to stay under the limit rather than recover from it: one token
bucket in front of every Shopify request, which slows down when it sees a 429
and drifts back up when it stops seeing them.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket with adaptive backoff, safe to share across tasks."""

    def __init__(
        self,
        rate: float = 2.0,
        min_rate: float = 0.25,
        cooldown: float = 60.0,
        recover_after: float = 30.0,
    ):
        self.base_rate = rate
        self.rate = rate
        self.min_rate = min_rate
        self.cooldown = cooldown
        self.recover_after = recover_after
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._last_penalty = 0.0
        self.penalties = 0

    async def acquire(self) -> None:
        """Wait until this caller is allowed to make its request."""
        while True:
            async with self._lock:
                now = time.monotonic()
                # Drift the rate back up once the throttling has stopped.
                if (
                    self.rate < self.base_rate
                    and self._last_penalty
                    and now - self._last_penalty > self.recover_after
                ):
                    self.rate = min(self.base_rate, self.rate * 1.5)
                    self._last_penalty = now

                slot = max(now, self._next_slot)
                self._next_slot = slot + 1.0 / self.rate
                delay = slot - now
            if delay <= 0:
                return
            await asyncio.sleep(delay)
            return

    async def penalise(self, pause: float | None = None) -> None:
        """Called on a 429: halve the rate and hold everyone back for a while."""
        async with self._lock:
            self.penalties += 1
            self.rate = max(self.min_rate, self.rate / 2)
            self._last_penalty = time.monotonic()
            wait = self.cooldown if pause is None else pause
            # Push the next slot out so every waiting task backs off together,
            # not just the one that happened to receive the 429.
            self._next_slot = max(self._next_slot, time.monotonic() + wait)
            log.warning(
                "rate limited — slowing to %.2f req/s and pausing %.0fs", self.rate, wait
            )


class NullLimiter:
    """No-op limiter, for tests and for callers that do their own pacing."""

    penalties = 0

    async def acquire(self) -> None:
        return

    async def penalise(self, pause: float | None = None) -> None:
        return
