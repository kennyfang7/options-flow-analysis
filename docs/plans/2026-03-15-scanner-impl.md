# Scanner Module Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement `src/data/scanner.py` — IBKR market scanners that use `reqScannerSubscription` to surface unusual options activity across the market.

**Architecture:** `MarketScanner` wraps `IBKRClient` (same pattern as `ChainFetcher`), calls `reqScannerSubscriptionAsync` with an `ib_insync.ScannerSubscription`, and parses the raw `ScanData` list into typed `ScannerResult` models. Three convenience wrappers expose the most useful scan codes: unusual volume, IV gainers, and hot-by-volume. Scan codes and row limits are configurable via settings.

**Tech Stack:** Python 3.11+, ib_insync (`ScannerSubscription`, `ScanData`), pydantic v2, loguru, pytest + pytest-asyncio, unittest.mock

---

## Context

### IBKR Scanner API (ib_insync)
```python
from ib_insync import ScannerSubscription

sub = ScannerSubscription(
    instrument="OPT",           # "OPT" for options, "STK" for stocks
    locationCode="STK.US.MAJOR", # US major exchanges
    scanCode="OPT_VOLUME_MOST_ACTIVE",
    numberOfRows=25,
)
results = await ib.reqScannerSubscriptionAsync(sub)
# results: list[ScanData]
# Each ScanData has: .rank (int), .contractDetails (ContractDetails),
#                    .distance (str), .benchmark (str), .projection (str), .legsStr (str)
# contractDetails.contract has: .symbol, .conId, .localSymbol, .secType
```

### Scan codes used in this module
| Constant | IBKR scan code | Meaning |
|---|---|---|
| `SCAN_UNUSUAL_VOLUME` | `"OPT_VOLUME_MOST_ACTIVE"` | Options with highest volume today |
| `SCAN_TOP_IV_GAINERS` | `"TOP_OPT_IMP_VOLAT_GAIN"` | Largest IV increase |
| `SCAN_HOT_BY_VOLUME` | `"HOT_BY_OPT_VOLUME"` | Hottest by option volume relative to avg |

### Existing patterns to follow
- `ChainFetcher` in `src/data/chain_fetcher.py` — `__init__(client)`, `_ib = client.ib`, async methods
- `_clean` / `_clean_int` helpers — import from `src.data.chain_fetcher` (don't duplicate)
- `mock_ibkr_client` / `mock_ib` fixtures — already in `tests/conftest.py`
- Settings in `config/settings.py` — add new fields following existing pattern

### Current test count
276 tests passing. Target: ≥10 new scanner tests, total ≥286.

---

## Task 1: Add Scanner Settings

**Files:**
- Modify: `config/settings.py` (add 2 fields after `dashboard_max_alerts`)
- Modify: `tests/test_settings.py` (add 2 tests)

**Step 1: Write failing tests**

Add to `tests/test_settings.py`:

```python
def test_scanner_max_rows_default() -> None:
    s = Settings()
    assert s.scanner_max_rows == 25


def test_scanner_max_rows_too_large_raises() -> None:
    with pytest.raises(Exception):
        Settings(scanner_max_rows=51)


def test_scanner_location_default() -> None:
    s = Settings()
    assert s.scanner_location == "STK.US.MAJOR"
```

**Step 2: Run tests to confirm they fail**

```
pytest tests/test_settings.py -k "scanner" -v
```
Expected: FAILED — `Settings` has no `scanner_max_rows` attribute.

**Step 3: Add settings fields**

In `config/settings.py`, after `dashboard_max_alerts` field (before the validators):

```python
# Scanner
scanner_max_rows: int = Field(
    default=25,
    ge=1,
    le=50,
    description="Maximum results per scanner subscription call",
)
scanner_location: str = Field(
    default="STK.US.MAJOR",
    description="IBKR location code for scanner (e.g. STK.US.MAJOR)",
)
```

**Step 4: Run tests to confirm they pass**

```
pytest tests/test_settings.py -k "scanner" -v
```
Expected: PASSED (3 tests).

**Step 5: Run full suite to check no regressions**

```
pytest --tb=short -q
```
Expected: 279+ tests passing, 0 failed.

**Step 6: Commit**

```bash
git add config/settings.py tests/test_settings.py
git commit -m "feat: add scanner_max_rows and scanner_location settings"
```

---

## Task 2: ScannerResult Model

**Files:**
- Create: `src/data/scanner.py`
- Create: `tests/test_scanner.py`

**Step 1: Write failing tests**

Create `tests/test_scanner.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.data.scanner import (
    SCAN_HOT_BY_VOLUME,
    SCAN_TOP_IV_GAINERS,
    SCAN_UNUSUAL_VOLUME,
    MarketScanner,
    ScannerResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scan_data(rank: int = 1, symbol: str = "SPY", con_id: int = 12345) -> MagicMock:
    """Build a fake ib_insync ScanData object."""
    contract = MagicMock()
    contract.symbol = symbol
    contract.conId = con_id
    contract.localSymbol = f"{symbol} option"
    contract.secType = "OPT"

    contract_details = MagicMock()
    contract_details.contract = contract

    scan_data = MagicMock()
    scan_data.rank = rank
    scan_data.contractDetails = contract_details
    scan_data.distance = "1.5"
    scan_data.benchmark = "500000"
    scan_data.projection = "750000"
    scan_data.legsStr = ""
    return scan_data


# ---------------------------------------------------------------------------
# Task 2: ScannerResult model
# ---------------------------------------------------------------------------

def test_scanner_result_fields() -> None:
    result = ScannerResult(
        rank=1,
        symbol="SPY",
        con_id=12345,
        description="SPY option",
        distance="1.5",
        benchmark="500000",
        projection="750000",
        scan_code="OPT_VOLUME_MOST_ACTIVE",
        scanned_at=datetime.now(timezone.utc),
    )
    assert result.rank == 1
    assert result.symbol == "SPY"
    assert result.con_id == 12345
    assert result.scan_code == "OPT_VOLUME_MOST_ACTIVE"


def test_scanner_result_con_id_optional() -> None:
    result = ScannerResult(
        rank=2,
        symbol="AAPL",
        con_id=None,
        description="AAPL option",
        scan_code="HOT_BY_OPT_VOLUME",
        scanned_at=datetime.now(timezone.utc),
    )
    assert result.con_id is None
    assert result.distance is None
    assert result.benchmark is None
    assert result.projection is None


def test_scan_code_constants_are_strings() -> None:
    assert SCAN_UNUSUAL_VOLUME == "OPT_VOLUME_MOST_ACTIVE"
    assert SCAN_TOP_IV_GAINERS == "TOP_OPT_IMP_VOLAT_GAIN"
    assert SCAN_HOT_BY_VOLUME == "HOT_BY_OPT_VOLUME"
```

**Step 2: Run tests to confirm they fail**

```
pytest tests/test_scanner.py -v
```
Expected: FAILED — `src.data.scanner` module does not exist.

**Step 3: Create `src/data/scanner.py` with model only**

```python
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
# MarketScanner (stub — implemented in Task 3)
# ---------------------------------------------------------------------------

class MarketScanner:
    """Placeholder — full implementation in Task 3."""

    def __init__(self, client: IBKRClient) -> None:
        self._client = client
        self._ib = client.ib
```

**Step 4: Run tests to confirm they pass**

```
pytest tests/test_scanner.py -k "scanner_result or scan_code_constants" -v
```
Expected: PASSED (3 tests).

**Step 5: Commit**

```bash
git add src/data/scanner.py tests/test_scanner.py
git commit -m "feat: add ScannerResult model and scan code constants"
```

---

## Task 3: `_parse_scan_data` Helper

**Files:**
- Modify: `src/data/scanner.py`
- Modify: `tests/test_scanner.py`

**Step 1: Write failing tests**

Add to `tests/test_scanner.py`:

```python
def test_parse_scan_data_full_fields(mock_ibkr_client: MagicMock) -> None:
    scanner = MarketScanner(mock_ibkr_client)
    raw = _make_scan_data(rank=3, symbol="AAPL", con_id=99999)
    result = scanner._parse_scan_data(raw, scan_code=SCAN_UNUSUAL_VOLUME)

    assert result.rank == 3
    assert result.symbol == "AAPL"
    assert result.con_id == 99999
    assert result.description == "AAPL option"
    assert result.distance == "1.5"
    assert result.benchmark == "500000"
    assert result.scan_code == SCAN_UNUSUAL_VOLUME
    assert isinstance(result.scanned_at, datetime)


def test_parse_scan_data_zero_con_id_becomes_none(mock_ibkr_client: MagicMock) -> None:
    scanner = MarketScanner(mock_ibkr_client)
    raw = _make_scan_data(con_id=0)
    result = scanner._parse_scan_data(raw, scan_code=SCAN_HOT_BY_VOLUME)
    assert result.con_id is None


def test_parse_scan_data_empty_strings_become_none(mock_ibkr_client: MagicMock) -> None:
    scanner = MarketScanner(mock_ibkr_client)
    raw = _make_scan_data()
    raw.distance = ""
    raw.benchmark = ""
    raw.projection = ""
    result = scanner._parse_scan_data(raw, scan_code=SCAN_TOP_IV_GAINERS)
    assert result.distance is None
    assert result.benchmark is None
    assert result.projection is None
```

**Step 2: Run tests to confirm they fail**

```
pytest tests/test_scanner.py -k "parse_scan_data" -v
```
Expected: FAILED — `MarketScanner` has no `_parse_scan_data` method.

**Step 3: Implement `_parse_scan_data` in `MarketScanner`**

Replace the `MarketScanner` stub body:

```python
class MarketScanner:
    """Fetches IBKR market scanner results for options flow analysis.

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
```

**Step 4: Run tests to confirm they pass**

```
pytest tests/test_scanner.py -k "parse_scan_data" -v
```
Expected: PASSED (3 tests).

**Step 5: Run full suite**

```
pytest --tb=short -q
```
Expected: 285+ passing, 0 failed.

**Step 6: Commit**

```bash
git add src/data/scanner.py tests/test_scanner.py
git commit -m "feat: add MarketScanner._parse_scan_data helper"
```

---

## Task 4: `MarketScanner.scan()` Generic Method

**Files:**
- Modify: `src/data/scanner.py`
- Modify: `tests/test_scanner.py`

**Step 1: Write failing tests**

Add to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_scan_returns_parsed_results(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(
        return_value=[
            _make_scan_data(rank=1, symbol="SPY"),
            _make_scan_data(rank=2, symbol="AAPL"),
        ]
    )
    scanner = MarketScanner(mock_ibkr_client)
    results = await scanner.scan(SCAN_UNUSUAL_VOLUME)

    assert len(results) == 2
    assert results[0].rank == 1
    assert results[0].symbol == "SPY"
    assert results[0].scan_code == SCAN_UNUSUAL_VOLUME
    assert results[1].symbol == "AAPL"


@pytest.mark.asyncio
async def test_scan_empty_results(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(return_value=[])
    scanner = MarketScanner(mock_ibkr_client)
    results = await scanner.scan(SCAN_TOP_IV_GAINERS)
    assert results == []


@pytest.mark.asyncio
async def test_scan_passes_correct_subscription_params(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(return_value=[])
    scanner = MarketScanner(mock_ibkr_client)
    await scanner.scan(
        SCAN_HOT_BY_VOLUME,
        instrument="OPT",
        location="STK.US.MAJOR",
        n_rows=10,
    )
    call_args = mock_ibkr_client.ib.reqScannerSubscriptionAsync.call_args
    sub = call_args[0][0]  # positional arg 0
    assert sub.scanCode == SCAN_HOT_BY_VOLUME
    assert sub.instrument == "OPT"
    assert sub.locationCode == "STK.US.MAJOR"
    assert sub.numberOfRows == 10
```

**Step 2: Run tests to confirm they fail**

```
pytest tests/test_scanner.py -k "test_scan" -v
```
Expected: FAILED — `MarketScanner` has no `scan` method.

**Step 3: Implement `scan()` in `MarketScanner`**

Add to the `MarketScanner` class (after `_parse_scan_data`):

```python
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
            List of ScannerResult sorted by rank ascending.
        """
        from ib_insync import ScannerSubscription

        sub = ScannerSubscription(
            instrument=instrument,
            locationCode=location,
            scanCode=scan_code,
            numberOfRows=n_rows,
        )
        logger.info("scan: running {} (instrument={}, location={}, n_rows={})",
                    scan_code, instrument, location, n_rows)

        raw_results = await self._ib.reqScannerSubscriptionAsync(sub)
        results = [self._parse_scan_data(r, scan_code=scan_code) for r in raw_results]

        logger.info("scan: {} returned {} results", scan_code, len(results))
        return results
```

**Step 4: Run tests to confirm they pass**

```
pytest tests/test_scanner.py -k "test_scan" -v
```
Expected: PASSED (3 tests).

**Step 5: Run full suite**

```
pytest --tb=short -q
```
Expected: 288+ passing, 0 failed.

**Step 6: Commit**

```bash
git add src/data/scanner.py tests/test_scanner.py
git commit -m "feat: implement MarketScanner.scan() generic method"
```

---

## Task 5: Convenience Scan Methods

**Files:**
- Modify: `src/data/scanner.py`
- Modify: `tests/test_scanner.py`

**Step 1: Write failing tests**

Add to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_scan_unusual_volume_calls_correct_code(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(
        return_value=[_make_scan_data(rank=1, symbol="SPY")]
    )
    scanner = MarketScanner(mock_ibkr_client)
    results = await scanner.scan_unusual_volume()
    sub = mock_ibkr_client.ib.reqScannerSubscriptionAsync.call_args[0][0]
    assert sub.scanCode == SCAN_UNUSUAL_VOLUME
    assert len(results) == 1


@pytest.mark.asyncio
async def test_scan_top_iv_gainers_calls_correct_code(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(return_value=[])
    scanner = MarketScanner(mock_ibkr_client)
    await scanner.scan_top_iv_gainers()
    sub = mock_ibkr_client.ib.reqScannerSubscriptionAsync.call_args[0][0]
    assert sub.scanCode == SCAN_TOP_IV_GAINERS


@pytest.mark.asyncio
async def test_scan_hot_by_volume_calls_correct_code(mock_ibkr_client: MagicMock) -> None:
    mock_ibkr_client.ib.reqScannerSubscriptionAsync = AsyncMock(return_value=[])
    scanner = MarketScanner(mock_ibkr_client)
    await scanner.scan_hot_by_volume(n_rows=10)
    sub = mock_ibkr_client.ib.reqScannerSubscriptionAsync.call_args[0][0]
    assert sub.scanCode == SCAN_HOT_BY_VOLUME
    assert sub.numberOfRows == 10
```

**Step 2: Run tests to confirm they fail**

```
pytest tests/test_scanner.py -k "scan_unusual or scan_top or scan_hot" -v
```
Expected: FAILED — methods not defined.

**Step 3: Add convenience methods to `MarketScanner`**

Add after `scan()`:

```python
    async def scan_unusual_volume(
        self,
        n_rows: int = 25,
        location: str = "STK.US.MAJOR",
    ) -> list[ScannerResult]:
        """Scan for options with the highest volume today.

        Wraps scan() with scan_code=SCAN_UNUSUAL_VOLUME.

        Args:
            n_rows: Maximum number of results (default 25).
            location: IBKR location code (default "STK.US.MAJOR").

        Returns:
            List of ScannerResult ranked by option volume.
        """
        return await self.scan(SCAN_UNUSUAL_VOLUME, n_rows=n_rows, location=location)

    async def scan_top_iv_gainers(
        self,
        n_rows: int = 25,
        location: str = "STK.US.MAJOR",
    ) -> list[ScannerResult]:
        """Scan for options with the largest implied volatility increase today.

        Wraps scan() with scan_code=SCAN_TOP_IV_GAINERS.

        Args:
            n_rows: Maximum number of results (default 25).
            location: IBKR location code (default "STK.US.MAJOR").

        Returns:
            List of ScannerResult ranked by IV gain.
        """
        return await self.scan(SCAN_TOP_IV_GAINERS, n_rows=n_rows, location=location)

    async def scan_hot_by_volume(
        self,
        n_rows: int = 25,
        location: str = "STK.US.MAJOR",
    ) -> list[ScannerResult]:
        """Scan for the hottest options by volume relative to average.

        Wraps scan() with scan_code=SCAN_HOT_BY_VOLUME.

        Args:
            n_rows: Maximum number of results (default 25).
            location: IBKR location code (default "STK.US.MAJOR").

        Returns:
            List of ScannerResult ranked by relative volume heat.
        """
        return await self.scan(SCAN_HOT_BY_VOLUME, n_rows=n_rows, location=location)
```

**Step 4: Run tests to confirm they pass**

```
pytest tests/test_scanner.py -v
```
Expected: all scanner tests pass.

**Step 5: Run full suite**

```
pytest --tb=short -q
```
Expected: 291+ passing, 0 failed.

**Step 6: Commit**

```bash
git add src/data/scanner.py tests/test_scanner.py
git commit -m "feat: add scan_unusual_volume, scan_top_iv_gainers, scan_hot_by_volume convenience methods"
```

---

## Task 6: Exports, `__init__` Update, and Smoke Test

**Files:**
- Modify: `src/data/__init__.py` (add scanner exports)
- Modify: `src/data/scanner.py` (add `__main__` block)

**Step 1: Update `src/data/__init__.py`**

```python
from .chain_fetcher import ChainFetcher, OptionChainSnapshot, OptionContract
from .tick_stream import TickStream, TickUpdate, TickStreamError
from .scanner import MarketScanner, ScannerResult, SCAN_UNUSUAL_VOLUME, SCAN_TOP_IV_GAINERS, SCAN_HOT_BY_VOLUME

__all__ = [
    "ChainFetcher",
    "OptionChainSnapshot",
    "OptionContract",
    "TickStream",
    "TickUpdate",
    "TickStreamError",
    "MarketScanner",
    "ScannerResult",
    "SCAN_UNUSUAL_VOLUME",
    "SCAN_TOP_IV_GAINERS",
    "SCAN_HOT_BY_VOLUME",
]
```

**Step 2: Add `__main__` smoke test to `src/data/scanner.py`**

Add at the bottom of `scanner.py`:

```python
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
```

**Step 3: Add integration test to `tests/test_scanner.py`**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_unusual_volume_live() -> None:
    """Smoke test against live TWS.

    Run with: pytest -m integration
    """
    from src.connection.ibkr_client import IBKRClient

    async with IBKRClient() as client:
        scanner = MarketScanner(client)
        results = await scanner.scan_unusual_volume(n_rows=5)

    assert isinstance(results, list)
    assert len(results) <= 5
    for r in results:
        assert r.symbol
        assert r.scan_code == SCAN_UNUSUAL_VOLUME
        assert r.rank >= 1
```

**Step 4: Run full suite (excluding integration)**

```
pytest --tb=short -q -m "not integration"
```
Expected: 291+ passing, 0 failed.

**Step 5: Verify imports work**

```
python -c "from src.data import MarketScanner, ScannerResult, SCAN_UNUSUAL_VOLUME; print('OK')"
```
Expected: `OK`

**Step 6: Commit**

```bash
git add src/data/scanner.py src/data/__init__.py tests/test_scanner.py
git commit -m "feat: complete scanner module with exports and integration test"
```

---

## Final Verification

Run the complete suite one last time:

```
pytest --tb=short -q -m "not integration"
```

Expected output:
```
..............................................................................
NNN passed in X.XXs
```
Target: ≥ 291 tests passing (276 existing + ≥ 15 new scanner tests).

Update `memory/MEMORY.md`:
- Step 13 progress
- Scanner layer key details
