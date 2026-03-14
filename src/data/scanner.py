from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel

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


# ---------------------------------------------------------------------------
# MarketScanner (stub — to be expanded in later tasks)
# ---------------------------------------------------------------------------

class MarketScanner:
    """Placeholder — full implementation in later tasks."""

    def __init__(self, client: IBKRClient) -> None:
        self._client = client
        self._ib = client.ib
