from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from config.settings import Settings

# Recognised acquire() kinds and which limiters they hit.
_KIND_GENERAL = "general"
_KIND_HISTORICAL = "historical"
_VALID_KINDS = {_KIND_GENERAL, _KIND_HISTORICAL}

_HISTORICAL_WINDOW_SECONDS = 600.0  # IBKR pacing window: 10 minutes


class _TokenBucket:
    """Token bucket for per-second message rate limiting.

    Fills continuously at ``rate`` tokens/second up to ``capacity``.
    Each call to ``consume()`` removes one token; if the bucket is empty
    the coroutine sleeps until a token becomes available.

    Args:
        rate: Tokens added per second (e.g. 48.0).
        capacity: Maximum tokens the bucket can hold (defaults to ``rate``).
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = rate
        self._capacity = capacity if capacity is not None else rate
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def consume(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Sleep until the next token arrives — outside the lock.
                wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)

    @property
    def available(self) -> float:
        """Current token count after a refill (read-only snapshot)."""
        self._refill()
        return self._tokens

    @property
    def capacity(self) -> float:
        """Maximum token capacity of this bucket (read-only)."""
        return self._capacity


class _SlidingWindow:
    """Sliding window counter for IBKR historical data pacing.

    Tracks timestamps of the last ``max_requests`` calls within
    ``window_seconds``. Blocks until the oldest call has aged out when the
    window is full.

    Args:
        max_requests: Maximum calls allowed within the window.
        window_seconds: Length of the rolling window in seconds.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _evict_old(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:  # M11: strict < keeps boundary timestamp
            self._timestamps.popleft()

    async def consume(self) -> None:
        """Block until capacity is available in the window, then record a slot."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._evict_old(now)
                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    return
                # Sleep until the oldest entry expires — outside the lock.
                wait = self._window_seconds - (now - self._timestamps[0])
            await asyncio.sleep(max(wait, 0.0))

    @property
    def used(self) -> int:
        """Requests recorded in the current window (read-only snapshot)."""
        self._evict_old(time.monotonic())
        return len(self._timestamps)


class RateLimiter:
    """Centralised async rate limiter for all IBKR API calls.

    Enforces two IBKR limits:
    - **general**: ≤ ``ibkr_max_messages_per_sec`` messages/second (token bucket).
    - **historical**: ≤ ``ibkr_max_historical_per_10min`` requests per 10 minutes
      (sliding window) *and* also counts against the general per-second limit.

    Typical usage::

        limiter = RateLimiter()

        # Before any IBKR call:
        await limiter.acquire()

        # Before reqHistoricalData specifically:
        await limiter.acquire("historical")

    Args:
        settings: Pydantic Settings instance. Defaults to the shared singleton.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            from config.settings import settings as _settings
            settings = _settings

        self._settings = settings
        self._bucket = _TokenBucket(rate=float(settings.ibkr_max_messages_per_sec))
        self._historical = _SlidingWindow(
            max_requests=settings.ibkr_max_historical_per_10min,
            window_seconds=_HISTORICAL_WINDOW_SECONDS,
        )

    async def acquire(self, kind: str = _KIND_GENERAL) -> None:
        """Acquire a rate-limit slot before making an IBKR API call.

        For ``"general"`` calls, consumes one token from the per-second bucket.
        For ``"historical"`` calls, also checks the 10-minute pacing window
        (the pacing check runs first so the caller waits for the slower limit
        before consuming a per-second token).

        Args:
            kind: ``"general"`` (default) or ``"historical"``.

        Raises:
            ValueError: If ``kind`` is not a recognised limiter type.
        """
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"Unknown rate limit kind {kind!r}. Valid kinds: {_VALID_KINDS}"
            )

        if kind == _KIND_HISTORICAL:
            await self._historical.consume()
            logger.debug(
                "RateLimiter: historical slot acquired ({}/{} used in window)",
                self._historical.used,
                self._settings.ibkr_max_historical_per_10min,
            )

        available_before = self._bucket.available
        await self._bucket.consume()
        logger.debug(
            "RateLimiter: general token acquired ({:.1f} remaining)",
            available_before - 1.0,
        )

    def stats(self) -> dict[str, object]:
        """Return a snapshot of current limiter state for monitoring.

        Returns:
            Dict with keys:
            - ``bucket_available``: Current token count (float).
            - ``bucket_capacity``: Max token capacity (float).
            - ``historical_used``: Requests used in current 10-min window (int).
            - ``historical_max``: Max requests allowed per window (int).
        """
        return {
            "bucket_available": round(self._bucket.available, 2),
            "bucket_capacity": self._bucket.capacity,
            "historical_used": self._historical.used,
            "historical_max": self._settings.ibkr_max_historical_per_10min,
        }
