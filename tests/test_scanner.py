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
# Tests: ScannerResult model
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


# ---------------------------------------------------------------------------
# Tests: _parse_scan_data
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tests: MarketScanner.scan()
# ---------------------------------------------------------------------------

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
