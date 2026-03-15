from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient


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

    def __init__(self, client: IBKRClient) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
        """
        self._client = client
        self._ib = client.ib

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        scan_code: str,
        *,
        instrument: str = "OPT",
        location: str = "STK.US.MAJOR",
        n_rows: int = 25,
    ) -> list[ScannerResult]:
        """Run an IBKR scanner subscription and return structured results.

        Calls reqScannerSubscriptionAsync with the given parameters and
        parses each ScanData entry into a ScannerResult.

        Args:
            scan_code: IBKR scan code (e.g. SCAN_UNUSUAL_VOLUME).
            instrument: Instrument type (default "OPT" for options).
            location: IBKR location code (default "STK.US.MAJOR").
            n_rows: Maximum number of results to return (default 25).

        Returns:
            List of ScannerResult sorted by rank ascending (as returned by IBKR).
        """
        from ib_insync import ScannerSubscription

        sub = ScannerSubscription(
            instrument=instrument,
            locationCode=location,
            scanCode=scan_code,
            numberOfRows=n_rows,
        )
        logger.info(
            "scan: running {} (instrument={}, location={}, n_rows={})",
            scan_code, instrument, location, n_rows,
        )

        raw_results = await self._ib.reqScannerSubscriptionAsync(sub)
        results = [self._parse_scan_data(r, scan_code=scan_code) for r in raw_results]

        logger.info("scan: {} returned {} results", scan_code, len(results))
        return results

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
