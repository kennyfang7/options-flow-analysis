from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, field_validator

from config.settings import settings as _settings

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient

from src.connection.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Scan code constants
# ---------------------------------------------------------------------------

SCAN_UNUSUAL_VOLUME: str = "OPT_VOLUME_MOST_ACTIVE"
SCAN_TOP_IV_GAINERS: str = "TOP_OPT_IMP_VOLAT_GAIN"
SCAN_HOT_BY_VOLUME: str = "HOT_BY_OPT_VOLUME"


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

class ScannerResult(BaseModel):
    """A single result entry from an IBKR market scanner subscription.

    Attributes:
        rank: Scanner rank (1 = highest score for the scan code).
        symbol: Underlying ticker symbol (e.g. "SPY").
        con_id: IBKR contract ID. None if not available.
        description: Human-readable contract description (localSymbol).
        distance: Scanner-specific distance metric (raw string from IBKR).
        benchmark: Scanner benchmark value (raw string from IBKR).
        projection: Scanner projection value (raw string from IBKR).
        scan_code: The IBKR scan code that produced this result.
        scanned_at: UTC datetime when the scan was executed.
    """

    rank: int
    symbol: str
    con_id: int | None = None
    description: str = ""
    distance: str | None = None
    benchmark: str | None = None
    projection: str | None = None
    scan_code: str
    scanned_at: datetime

    @field_validator("scanned_at")
    @classmethod
    def scanned_at_must_be_timezone_aware(cls, v: datetime) -> datetime:
        """Reject naive datetimes to prevent storage layer UTC mixing.

        Args:
            v: The scanned_at datetime to validate.

        Returns:
            The validated datetime.

        Raises:
            ValueError: If the datetime has no timezone info.
        """
        if v.tzinfo is None:
            raise ValueError("scanned_at must be timezone-aware (use datetime.now(timezone.utc))")
        return v


# ---------------------------------------------------------------------------
# MarketScanner (stub — to be expanded in later tasks)
# ---------------------------------------------------------------------------

class MarketScanner:
    """Wraps IBKR market scanner subscriptions and maps results to ScannerResult.

    Uses reqScannerSubscriptionAsync to run IBKR's built-in scanners and
    returns structured ScannerResult models for downstream processing.

    Example:
        async with IBKRClient() as client:
            scanner = MarketScanner(client)
            results = await scanner.scan_unusual_volume()
            for r in results:
                print(r.rank, r.symbol, r.scan_code)
    """

    def __init__(self, client: IBKRClient, limiter: RateLimiter | None = None) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
            limiter: Shared RateLimiter instance. If None, a new one is created.
                Pass the same limiter to ChainFetcher, TickStream, and MarketScanner
                so the 48 msg/sec budget is enforced across all three.
        """
        self._client = client
        self._ib = client.ib
        self._settings = _settings
        self._limiter = limiter if limiter is not None else RateLimiter()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        scan_code: str,
        *,
        instrument: str = "OPT",
        location: str | None = None,
        n_rows: int | None = None,
    ) -> list[ScannerResult]:
        """Run an IBKR scanner subscription and return structured results.

        Uses scanner_location and scanner_max_rows from settings as defaults
        when location or n_rows are not explicitly provided.

        Args:
            scan_code: IBKR scan code (e.g. SCAN_UNUSUAL_VOLUME).
            instrument: Instrument type (default "OPT" for options).
            location: IBKR location code. Defaults to settings.scanner_location.
            n_rows: Maximum number of results. Defaults to settings.scanner_max_rows.

        Returns:
            List of ScannerResult sorted by rank ascending.

        Raises:
            RuntimeError: If the IBKR scanner subscription fails.
        """
        from ib_insync import ScannerSubscription

        effective_location = location if location is not None else self._settings.scanner_location
        effective_n_rows = n_rows if n_rows is not None else self._settings.scanner_max_rows

        sub = ScannerSubscription(
            instrument=instrument,
            locationCode=effective_location,
            scanCode=scan_code,
            numberOfRows=effective_n_rows,
        )
        logger.info(
            "scan: running {} (instrument={}, location={}, n_rows={})",
            scan_code, instrument, effective_location, effective_n_rows,
        )

        try:
            await self._limiter.acquire()
            raw_results = await self._ib.reqScannerSubscriptionAsync(sub)
        except Exception as exc:
            logger.exception("scan: reqScannerSubscriptionAsync failed for {}: {}", scan_code, exc)
            raise RuntimeError(f"Scanner subscription failed for {scan_code}") from exc

        results = [self._parse_scan_data(r, scan_code=scan_code) for r in raw_results]
        results.sort(key=lambda r: r.rank)

        logger.info("scan: {} returned {} results", scan_code, len(results))
        return results

    async def scan_unusual_volume(
        self,
        n_rows: int | None = None,
        location: str | None = None,
    ) -> list[ScannerResult]:
        """Scan for options with the highest volume today.

        Wraps scan() with scan_code=SCAN_UNUSUAL_VOLUME. Uses settings defaults
        for n_rows and location when not provided.

        Args:
            n_rows: Maximum number of results. Defaults to settings.scanner_max_rows.
            location: IBKR location code. Defaults to settings.scanner_location.

        Returns:
            List of ScannerResult ranked by option volume.
        """
        return await self.scan(SCAN_UNUSUAL_VOLUME, n_rows=n_rows, location=location)

    async def scan_top_iv_gainers(
        self,
        n_rows: int | None = None,
        location: str | None = None,
    ) -> list[ScannerResult]:
        """Scan for options with the largest implied volatility increase today.

        Wraps scan() with scan_code=SCAN_TOP_IV_GAINERS. Uses settings defaults
        for n_rows and location when not provided.

        Args:
            n_rows: Maximum number of results. Defaults to settings.scanner_max_rows.
            location: IBKR location code. Defaults to settings.scanner_location.

        Returns:
            List of ScannerResult ranked by IV gain.
        """
        return await self.scan(SCAN_TOP_IV_GAINERS, n_rows=n_rows, location=location)

    async def scan_hot_by_volume(
        self,
        n_rows: int | None = None,
        location: str | None = None,
    ) -> list[ScannerResult]:
        """Scan for the hottest options by volume relative to average.

        Wraps scan() with scan_code=SCAN_HOT_BY_VOLUME. Uses settings defaults
        for n_rows and location when not provided.

        Args:
            n_rows: Maximum number of results. Defaults to settings.scanner_max_rows.
            location: IBKR location code. Defaults to settings.scanner_location.

        Returns:
            List of ScannerResult ranked by relative volume heat.
        """
        return await self.scan(SCAN_HOT_BY_VOLUME, n_rows=n_rows, location=location)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_scan_data(self, raw: object, *, scan_code: str) -> ScannerResult:
        """Parse a raw ib_insync ScanData object into a ScannerResult.

        Normalizes empty strings to None and converts conId=0 to None.

        Args:
            raw: A single ScanData entry from reqScannerSubscriptionAsync.
            scan_code: The IBKR scan code that produced this result.

        Returns:
            ScannerResult with all available fields populated.
        """
        cd = raw.contractDetails
        c = cd.contract

        def _opt_str(v: str) -> str | None:
            return v if v else None

        return ScannerResult(
            rank=raw.rank,
            symbol=c.symbol,
            con_id=c.conId or None,
            description=getattr(c, "localSymbol", "") or "",
            distance=_opt_str(raw.distance),
            benchmark=_opt_str(raw.benchmark),
            projection=_opt_str(raw.projection),
            scan_code=scan_code,
            scanned_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Standalone smoke test (requires live TWS on port 7496/7497)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from src.connection.ibkr_client import IBKRClient

    async def _main() -> None:
        async with IBKRClient() as client:
            scanner = MarketScanner(client)

            logger.info("Running scan_unusual_volume (top 10)...")
            results = await scanner.scan_unusual_volume(n_rows=10)
            print(f"\nTop {len(results)} Unusual Option Volume")
            for r in results:
                print(f"  [{r.rank:2d}] {r.symbol:<8} | scan={r.scan_code} | conId={r.con_id}")

            logger.info("Running scan_top_iv_gainers (top 10)...")
            results = await scanner.scan_top_iv_gainers(n_rows=10)
            print(f"\nTop {len(results)} IV Gainers")
            for r in results:
                print(f"  [{r.rank:2d}] {r.symbol:<8} | {r.description}")

    asyncio.run(_main())
