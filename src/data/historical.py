from __future__ import annotations

import datetime as _dt
from datetime import timezone
from typing import TYPE_CHECKING

import pandas as pd
from ib_insync import Stock
from loguru import logger
from pydantic import BaseModel

from src.connection.rate_limiter import RateLimiter
from src.data.chain_fetcher import _clean, _clean_int

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient


# ---------------------------------------------------------------------------
# Valid IBKR parameter constants
# ---------------------------------------------------------------------------

VALID_BAR_SIZES: frozenset[str] = frozenset({
    "1 secs", "5 secs", "10 secs", "15 secs", "30 secs",
    "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins", "20 mins", "30 mins",
    "1 hour", "2 hours", "3 hours", "4 hours", "8 hours",
    "1 day", "1 week", "1 month",
})

VALID_WHAT_TO_SHOW: frozenset[str] = frozenset({
    "TRADES", "MIDPOINT", "BID", "ASK", "BID_ASK",
    "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY",
})


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class HistoricalBar(BaseModel):
    """A single OHLCV bar from IBKR historical data.

    Attributes:
        timestamp: Bar open time, always timezone-aware UTC.
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Trade volume for the bar. None if IBKR returns sentinel -1.
        bar_count: Number of individual trades within the bar. None if unavailable.
        average: Volume-weighted average price for the bar. None if unavailable.
    """

    timestamp: _dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    bar_count: int | None = None
    average: float | None = None


class HistoricalBars(BaseModel):
    """Historical bar data for a symbol.

    Attributes:
        symbol: Underlying ticker symbol (e.g. "SPY").
        bar_size: IBKR barSizeSetting used (e.g. "1 day").
        what_to_show: Data type returned (e.g. "TRADES").
        fetched_at: UTC datetime when the request completed.
        bars: List of OHLCV bars, oldest first.
    """

    symbol: str
    bar_size: str
    what_to_show: str
    fetched_at: _dt.datetime
    bars: list[HistoricalBar]

    def to_dataframe(self) -> pd.DataFrame:
        """Convert bars to a pandas DataFrame indexed by timestamp.

        Returns:
            DataFrame with columns open, high, low, close, volume,
            bar_count, average, indexed by timestamp. Empty DataFrame
            if no bars were returned.
        """
        if not self.bars:
            return pd.DataFrame()
        df = pd.DataFrame([b.model_dump() for b in self.bars])
        df = df.set_index("timestamp")
        return df

    def avg_daily_volume(self) -> float | None:
        """Compute mean daily volume across all bars with valid volume data.

        Useful for computing the unusual-activity multiplier in
        UnusualDetector (volume_delta / ADV).

        Returns:
            Mean volume as a float, or None if no bars carry volume data.
        """
        volumes = [b.volume for b in self.bars if b.volume is not None and b.volume > 0]
        if not volumes:
            return None
        return sum(volumes) / len(volumes)


# ---------------------------------------------------------------------------
# Historical fetcher
# ---------------------------------------------------------------------------

class HistoricalFetcher:
    """Fetches historical OHLCV bar data from IBKR via reqHistoricalData.

    Uses an already-connected IBKRClient and respects both IBKR pacing limits:
    - 48 msg/sec general token bucket (via limiter.acquire())
    - 55 historical requests / 10-minute sliding window
      (via limiter.acquire("historical"))

    Each call to fetch_bars() consumes one historical slot plus one general
    message slot.

    Example:
        async with IBKRClient() as client:
            limiter = RateLimiter()
            fetcher = HistoricalFetcher(client, limiter)
            bars = await fetcher.fetch_bars("SPY", duration="30 D", bar_size="1 day")
            adv = bars.avg_daily_volume()
            df = bars.to_dataframe()
    """

    def __init__(self, client: IBKRClient, limiter: RateLimiter | None = None) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
            limiter: Shared RateLimiter instance. If None, a new one is created.
                Pass the same limiter to HistoricalFetcher, ChainFetcher,
                TickStream, and MarketScanner so the 48 msg/sec and
                55 hist/10 min budgets are enforced across all components.
        """
        self._client = client
        self._ib = client.ib
        self._limiter = limiter if limiter is not None else RateLimiter()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_bars(
        self,
        symbol: str,
        duration: str = "30 D",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        end_datetime: str = "",
    ) -> HistoricalBars:
        """Fetch historical OHLCV bars for a stock symbol.

        Qualifies the underlying stock contract with IBKR, then calls
        reqHistoricalData. Consumes one historical rate-limit slot (10-min
        sliding window) and one general message slot (per-second token bucket).

        Args:
            symbol: Underlying ticker symbol (e.g. "SPY").
            duration: IBKR durationStr — amount of history to fetch.
                Valid suffixes: S (seconds), D (days), W (weeks),
                M (months), Y (years). Examples: "1 D", "5 D", "1 W",
                "1 M", "3 M", "1 Y".
            bar_size: IBKR barSizeSetting. Must be one of VALID_BAR_SIZES.
                Examples: "1 min", "5 mins", "1 hour", "1 day".
            what_to_show: IBKR whatToShow field. Must be one of
                VALID_WHAT_TO_SHOW. Use "TRADES" for price/volume,
                "HISTORICAL_VOLATILITY" for realised HV,
                "OPTION_IMPLIED_VOLATILITY" for IV context.
            use_rth: If True, only return regular-trading-hours bars
                (9:30–16:00 ET for US stocks).
            end_datetime: IBKR endDateTime string. Empty string ("") means
                the latest available data. Format: "YYYYMMDD HH:MM:SS".

        Returns:
            HistoricalBars containing all returned bars, oldest first.

        Raises:
            ValueError: If bar_size or what_to_show is not recognised, or
                if the underlying contract cannot be qualified by IBKR.
        """
        if bar_size not in VALID_BAR_SIZES:
            raise ValueError(
                f"Invalid bar_size {bar_size!r}. "
                f"Valid values: {sorted(VALID_BAR_SIZES)}"
            )
        if what_to_show not in VALID_WHAT_TO_SHOW:
            raise ValueError(
                f"Invalid what_to_show {what_to_show!r}. "
                f"Valid values: {sorted(VALID_WHAT_TO_SHOW)}"
            )

        contract = await self._qualify_underlying(symbol)

        logger.info(
            "fetch_bars: {} | duration={} bar_size={} what_to_show={} use_rth={}",
            symbol, duration, bar_size, what_to_show, use_rth,
        )

        await self._limiter.acquire("historical")
        raw_bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_datetime,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
        )

        bars = [self._parse_bar(b) for b in raw_bars]
        logger.success(
            "fetch_bars: {} bars returned for {} ({} duration, {} bar size)",
            len(bars), symbol, duration, bar_size,
        )

        return HistoricalBars(
            symbol=symbol,
            bar_size=bar_size,
            what_to_show=what_to_show,
            fetched_at=_dt.datetime.now(timezone.utc),
            bars=bars,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _qualify_underlying(self, symbol: str) -> Stock:
        """Qualify the underlying stock contract with IBKR.

        Args:
            symbol: Ticker symbol to qualify.

        Returns:
            Qualified ib_insync Stock contract with conId populated.

        Raises:
            ValueError: If IBKR cannot qualify the contract.
        """
        stock = Stock(symbol, "SMART", "USD")
        await self._limiter.acquire()
        qualified = await self._ib.qualifyContractsAsync(stock)
        if not qualified or qualified[0].conId == 0:
            raise ValueError(f"Could not qualify underlying: {symbol}")
        logger.debug("Qualified underlying: {} (conId={})", symbol, qualified[0].conId)
        return qualified[0]

    def _parse_bar(self, bar: object) -> HistoricalBar:
        """Parse a raw ib_insync BarData into a clean HistoricalBar model.

        ib_insync returns bar.date as a naive datetime for intraday bars
        and a naive datetime at midnight for daily/weekly/monthly bars
        (both treated as UTC here). Sentinel values (-1, nan) in volume,
        bar_count, and average are normalised to None via _clean/_clean_int.

        Args:
            bar: Raw BarData from reqHistoricalDataAsync.

        Returns:
            HistoricalBar with all available fields populated.
        """
        raw_date = bar.date  # type: ignore[attr-defined]
        timestamp = _parse_bar_date(raw_date)

        return HistoricalBar(
            timestamp=timestamp,
            open=bar.open,      # type: ignore[attr-defined]
            high=bar.high,      # type: ignore[attr-defined]
            low=bar.low,        # type: ignore[attr-defined]
            close=bar.close,    # type: ignore[attr-defined]
            volume=_clean_int(bar.volume),          # type: ignore[attr-defined]
            bar_count=_clean_int(bar.barCount),     # type: ignore[attr-defined]
            average=_clean(bar.average),            # type: ignore[attr-defined]
        )


def _parse_bar_date(raw: _dt.datetime | _dt.date | str) -> _dt.datetime:
    """Normalise an ib_insync bar date to a timezone-aware UTC datetime.

    ib_insync may return:
    - datetime (naive, intraday bars)
    - date (daily/weekly/monthly bars in some versions)
    - str fallback ("YYYYMMDD" or "YYYYMMDD HH:MM:SS")

    All forms are returned as UTC-aware datetime.

    Args:
        raw: Raw date value from BarData.date.

    Returns:
        Timezone-aware UTC datetime.
    """
    if isinstance(raw, _dt.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)

    if isinstance(raw, _dt.date):
        # Daily/weekly/monthly: treat as midnight UTC
        return _dt.datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)

    # Fallback: string parsing
    raw_str = str(raw).strip()
    if len(raw_str) == 8:
        return _dt.datetime.strptime(raw_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    # Handle "YYYYMMDD  HH:MM:SS" (double-space) and "YYYYMMDD HH:MM:SS"
    normalized = " ".join(raw_str.split())
    return _dt.datetime.strptime(normalized, "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from src.connection.ibkr_client import IBKRClient

    async def _main() -> None:
        async with IBKRClient() as client:
            fetcher = HistoricalFetcher(client)
            bars = await fetcher.fetch_bars("SPY", duration="10 D", bar_size="1 day")
            df = bars.to_dataframe()
            adv = bars.avg_daily_volume()
            print(f"\nSPY Historical Bars (10 D, 1 day)")
            print(f"  Bars returned   : {len(bars.bars)}")
            print(f"  Fetched at      : {bars.fetched_at}")
            print(f"  Avg daily vol   : {adv:,.0f}" if adv else "  Avg daily vol   : N/A")
            print(f"\n{df[['open','high','low','close','volume']].to_string()}")

    asyncio.run(_main())
