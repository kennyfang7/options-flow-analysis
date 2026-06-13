"""Tests for src/connection/rate_limiter.py.

Strategy:
- Patch time.monotonic to control the clock deterministically.
- Patch asyncio.sleep to avoid real waits and capture sleep durations.
- All tests are synchronous where possible; async tests use pytest-asyncio.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config.settings import Settings
from src.connection.rate_limiter import (
    RateLimiter,
    _SlidingWindow,
    _TokenBucket,
    _HISTORICAL_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_settings(**overrides) -> Settings:
    """Settings with rate-limit-friendly defaults."""
    defaults = dict(
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=99,
        ibkr_timeout=5,
        ibkr_max_retries=2,
        ibkr_readonly=True,
        ibkr_max_messages_per_sec=48,
        ibkr_max_historical_per_10min=55,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# _TokenBucket
# ---------------------------------------------------------------------------

class TestTokenBucket:
    def test_full_bucket_on_init(self):
        bucket = _TokenBucket(rate=10.0)
        assert bucket.available == pytest.approx(10.0, abs=0.01)

    def test_consume_reduces_tokens(self):
        """Consuming from a full bucket reduces token count by 1."""
        bucket = _TokenBucket(rate=10.0)
        # Patch sleep so consume() doesn't block; bucket starts full so no sleep needed.
        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(bucket.consume())
        assert bucket.available == pytest.approx(9.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_consume_sleeps_when_empty(self):
        """consume() sleeps when the bucket is empty, then proceeds after refill."""
        bucket = _TokenBucket(rate=1.0, capacity=1.0)
        # Drain the bucket.
        await bucket.consume()
        assert bucket.available < 1.0

        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            # Simulate time passing by advancing _last_refill via monotonic patch.
            sleep_calls.append(duration)
            bucket._tokens += duration * bucket._rate  # manually refill

        with patch("src.connection.rate_limiter.asyncio.sleep", side_effect=fake_sleep):
            await bucket.consume()

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] > 0

    @pytest.mark.asyncio
    async def test_rapid_consume_does_not_exceed_capacity(self):
        """Multiple rapid consumes from a full bucket never over-refill tokens."""
        bucket = _TokenBucket(rate=5.0, capacity=5.0)
        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            for _ in range(5):
                await bucket.consume()
        # After 5 consumes from a capacity-5 bucket (no real time passing),
        # tokens should be near zero (some tiny refill from monotonic drift).
        assert bucket._tokens < 1.0

    @pytest.mark.asyncio
    async def test_refill_over_time(self):
        """Tokens refill proportionally to elapsed real time."""
        bucket = _TokenBucket(rate=10.0, capacity=10.0)
        bucket._tokens = 0.0  # empty
        # Simulate 0.5s elapsed by moving _last_refill back.
        bucket._last_refill -= 0.5
        bucket._refill()
        assert bucket.available == pytest.approx(5.0, abs=0.2)

    @pytest.mark.asyncio
    async def test_tokens_capped_at_capacity(self):
        """Tokens never exceed bucket capacity after refill."""
        bucket = _TokenBucket(rate=10.0, capacity=10.0)
        bucket._last_refill -= 100.0  # simulate 100 seconds
        bucket._refill()
        assert bucket.available == pytest.approx(10.0, abs=0.01)


# ---------------------------------------------------------------------------
# _SlidingWindow
# ---------------------------------------------------------------------------

class TestSlidingWindow:
    @pytest.mark.asyncio
    async def test_allows_requests_under_limit(self):
        """Requests up to max_requests are allowed immediately."""
        window = _SlidingWindow(max_requests=3, window_seconds=600.0)
        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            for _ in range(3):
                await window.consume()
            mock_sleep.assert_not_called()
        assert window.used == 3

    @pytest.mark.asyncio
    async def test_blocks_when_window_full(self):
        """The 4th request blocks when max_requests=3 is reached."""
        window = _SlidingWindow(max_requests=3, window_seconds=600.0)
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            # Evict oldest entry by advancing its timestamp out of the window.
            if window._timestamps:
                window._timestamps[0] -= window._window_seconds + 1

        with patch("src.connection.rate_limiter.asyncio.sleep", side_effect=fake_sleep):
            for _ in range(4):
                await window.consume()

        assert len(sleep_calls) >= 1

    @pytest.mark.asyncio
    async def test_used_decreases_after_window_expires(self):
        """Old entries are evicted once they age past the window."""
        window = _SlidingWindow(max_requests=3, window_seconds=10.0)
        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            for _ in range(3):
                await window.consume()
        assert window.used == 3

        # Age all entries beyond the window.
        for i in range(len(window._timestamps)):
            window._timestamps[i] -= 11.0

        assert window.used == 0

    @pytest.mark.asyncio
    async def test_window_with_max_one(self):
        """Edge case: window of 1 request blocks the second immediately."""
        window = _SlidingWindow(max_requests=1, window_seconds=600.0)
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            window._timestamps[0] -= window._window_seconds + 1

        with patch("src.connection.rate_limiter.asyncio.sleep", side_effect=fake_sleep):
            await window.consume()
            await window.consume()

        assert len(sleep_calls) >= 1


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_general_no_sleep_under_limit(self):
        """acquire() for general kind completes without sleeping when under limit."""
        settings = make_settings(ibkr_max_messages_per_sec=10)
        limiter = RateLimiter(settings=settings)

        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await limiter.acquire()
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_acquire_historical_consumes_both_limiters(self):
        """acquire("historical") hits the sliding window AND the token bucket."""
        settings = make_settings(ibkr_max_messages_per_sec=10, ibkr_max_historical_per_10min=55)
        limiter = RateLimiter(settings=settings)

        original_hist_used = limiter._historical.used
        original_tokens = limiter._bucket.available

        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            await limiter.acquire("historical")

        assert limiter._historical.used == original_hist_used + 1
        assert limiter._bucket._tokens < original_tokens

    @pytest.mark.asyncio
    async def test_acquire_general_does_not_touch_historical_window(self):
        """acquire("general") does NOT consume a historical slot."""
        settings = make_settings()
        limiter = RateLimiter(settings=settings)

        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            await limiter.acquire("general")

        assert limiter._historical.used == 0

    @pytest.mark.asyncio
    async def test_acquire_unknown_kind_raises(self):
        """acquire() with an unrecognised kind raises ValueError immediately."""
        limiter = RateLimiter(settings=make_settings())
        with pytest.raises(ValueError, match="Unknown rate limit kind"):
            await limiter.acquire("bogus")

    def test_stats_returns_expected_keys(self):
        """stats() returns all four expected keys with correct types."""
        limiter = RateLimiter(settings=make_settings())
        s = limiter.stats()
        assert set(s.keys()) == {
            "bucket_available", "bucket_capacity",
            "historical_used", "historical_max",
        }
        assert isinstance(s["bucket_available"], float)
        assert isinstance(s["bucket_capacity"], float)
        assert isinstance(s["historical_used"], int)
        assert isinstance(s["historical_max"], int)

    def test_stats_reflects_settings(self):
        """stats() values align with the settings passed at construction."""
        settings = make_settings(ibkr_max_messages_per_sec=48, ibkr_max_historical_per_10min=55)
        limiter = RateLimiter(settings=settings)
        s = limiter.stats()
        assert s["bucket_capacity"] == 48.0
        assert s["historical_max"] == 55
        assert s["bucket_available"] == pytest.approx(48.0, abs=0.1)
        assert s["historical_used"] == 0

    @pytest.mark.asyncio
    async def test_stats_historical_used_increments(self):
        """stats() reports increasing historical_used after historical acquires."""
        settings = make_settings(ibkr_max_messages_per_sec=10, ibkr_max_historical_per_10min=55)
        limiter = RateLimiter(settings=settings)

        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            for _ in range(5):
                await limiter.acquire("historical")

        assert limiter.stats()["historical_used"] == 5

    @pytest.mark.asyncio
    async def test_default_settings_loaded(self):
        """RateLimiter() with no args loads from the singleton settings."""
        # Just verify it constructs without error and returns sane stats.
        limiter = RateLimiter()
        s = limiter.stats()
        assert s["bucket_capacity"] >= 1
        assert s["historical_max"] >= 1

    def test_historical_window_constant(self):
        """Pacing window constant is exactly 600 seconds (10 minutes)."""
        assert _HISTORICAL_WINDOW_SECONDS == 600.0

    @pytest.mark.asyncio
    async def test_concurrent_acquire_no_token_leakage(self):
        """10 concurrent acquire() calls never produce a negative token count."""
        settings = make_settings(ibkr_max_messages_per_sec=10)
        limiter = RateLimiter(settings=settings)

        with patch("src.connection.rate_limiter.asyncio.sleep", new=AsyncMock()):
            tasks = [asyncio.create_task(limiter.acquire()) for _ in range(10)]
            await asyncio.gather(*tasks)

        assert limiter._bucket._tokens >= 0.0, (
            f"Token leakage detected: _tokens={limiter._bucket._tokens}"
        )
