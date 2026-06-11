from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TTL_SECONDS: float = 3600.0  # 1-hour cache TTL


# ---------------------------------------------------------------------------
# EarningsCalendar
# ---------------------------------------------------------------------------


class EarningsCalendar:
    """Async cache for upcoming earnings dates sourced from yfinance.

    Wraps yfinance's sync API in asyncio.to_thread() so it is safe to call
    from async pipeline code without blocking the event loop.

    Cache entries are (next_earnings_date | None, fetched_at). A None date
    means the symbol either has no upcoming earnings (ETF, no data) or
    yfinance returned no future dates.

    Example::

        cal = EarningsCalendar()
        days = await cal.get_days_to_earnings("AAPL")
        # days == 12  (or None for ETFs / unavailable)

    Args:
        ttl_seconds: Cache time-to-live in seconds. Default 3600 (1 hour).
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        """Initialize with an optional TTL override.

        Args:
            ttl_seconds: Seconds before a cached entry is considered stale.
        """
        self._ttl = ttl_seconds
        # symbol → (next_earnings_date | None, fetched_at)
        self._cache: dict[str, tuple[date | None, datetime]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_days_to_earnings(self, symbol: str) -> int | None:
        """Return calendar days until the next earnings event.

        Args:
            symbol: Ticker symbol (e.g. "AAPL").

        Returns:
            Days until next earnings (0 = today). None when the date is
            unavailable (ETF, yfinance failure, no future dates).
        """
        next_date = await self._get_cached(symbol)
        if next_date is None:
            return None
        delta = (next_date - date.today()).days
        return max(delta, 0)

    async def prefetch(self, symbols: list[str]) -> None:
        """Warm the cache for multiple symbols concurrently.

        Fetches all symbols in parallel so the first tick of each symbol
        does not incur a blocking lookup.

        Args:
            symbols: List of ticker symbols to prefetch.
        """
        await asyncio.gather(*[self._get_cached(s) for s in symbols])

    def invalidate(self, symbol: str) -> None:
        """Remove a single symbol from the cache.

        Args:
            symbol: Ticker symbol to invalidate.
        """
        self._cache.pop(symbol, None)

    def clear(self) -> None:
        """Remove all symbols from the cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_cached(self, symbol: str) -> date | None:
        """Return the cached next-earnings date, refreshing if stale.

        Args:
            symbol: Ticker symbol.

        Returns:
            Next earnings date, or None.
        """
        entry = self._cache.get(symbol)
        if entry is not None:
            cached_date, fetched_at = entry
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
            if age < self._ttl:
                return cached_date

        # Cache miss or expired — fetch from yfinance
        next_date = await asyncio.to_thread(self._fetch_yfinance, symbol)
        self._cache[symbol] = (next_date, datetime.now(timezone.utc))
        return next_date

    def _fetch_yfinance(self, symbol: str) -> date | None:
        """Synchronous yfinance fetch — runs in a thread pool.

        Handles both DataFrame and dict formats that yfinance may return.
        Filters to only future or today dates.

        Args:
            symbol: Ticker symbol.

        Returns:
            Nearest upcoming earnings date, or None.
        """
        try:
            import yfinance as yf  # imported lazily to keep startup fast

            ticker = yf.Ticker(symbol)
            earnings_dates = ticker.get_earnings_dates(limit=8)

            today = date.today()

            if earnings_dates is None:
                return None

            # DataFrame path (standard yfinance response)
            try:
                import pandas as pd

                if isinstance(earnings_dates, pd.DataFrame) and not earnings_dates.empty:
                    future_dates: list[date] = []
                    for idx in earnings_dates.index:
                        try:
                            # index is a DatetimeTzDtype or datetime object
                            if hasattr(idx, "date"):
                                d = idx.date()
                            else:
                                d = pd.Timestamp(idx).date()
                            if d >= today:
                                future_dates.append(d)
                        except Exception:
                            continue
                    return min(future_dates) if future_dates else None
            except Exception:
                pass

            # Dict / list path (some yfinance versions)
            if isinstance(earnings_dates, dict):
                raw_values: list[Any] = list(earnings_dates.keys())
            elif hasattr(earnings_dates, "__iter__"):
                raw_values = list(earnings_dates)
            else:
                return None

            future: list[date] = []
            for val in raw_values:
                try:
                    if isinstance(val, date) and not isinstance(val, datetime):
                        d = val
                    elif isinstance(val, datetime):
                        d = val.date()
                    else:
                        from datetime import date as _date
                        import pandas as pd
                        d = pd.Timestamp(val).date()
                    if d >= today:
                        future.append(d)
                except Exception:
                    continue
            return min(future) if future else None

        except Exception as exc:
            logger.debug("EarningsCalendar._fetch_yfinance({}): {}", symbol, exc)
            return None


if __name__ == "__main__":
    import asyncio as _asyncio

    async def _main() -> None:
        cal = EarningsCalendar()
        for sym in ["AAPL", "SPY", "MSFT"]:
            days = await cal.get_days_to_earnings(sym)
            logger.info("{}: days_to_earnings={}", sym, days)

    _asyncio.run(_main())
