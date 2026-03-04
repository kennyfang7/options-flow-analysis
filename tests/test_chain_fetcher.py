from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.chain_fetcher import (
    ChainFetcher,
    OptionChainSnapshot,
    OptionContract,
    _clean,
    _clean_int,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_chain_params() -> MagicMock:
    """Fake OptionChainParams with a small set of expirations and strikes."""
    params = MagicMock()
    params.exchange = "SMART"
    params.expirations = frozenset([
        "20260320", "20260417", "20260515", "20260619",
        "20260717", "20260821", "20260918", "20261218",
    ])
    params.strikes = frozenset([float(s) for s in range(400, 650, 5)])
    return params


@pytest.fixture
def mock_ibkr_client(mock_ib: MagicMock) -> MagicMock:
    """Fake IBKRClient whose .ib property returns mock_ib."""
    client = MagicMock()
    client.ib = mock_ib
    return client


def make_fake_ticker(
    symbol: str = "SPY",
    expiry: str = "20260320",
    strike: float = 580.0,
    right: str = "C",
    con_id: int = 12345,
    bid: float = 5.10,
    ask: float = 5.20,
    last: float = 5.15,
    volume: float = 1000,
    open_interest: float = 5000,
    delta: float = 0.45,
    gamma: float = 0.02,
    theta: float = -0.30,
    vega: float = 0.25,
    implied_vol: float = 0.20,
) -> MagicMock:
    """Build a fake ib_insync Ticker with the given field values."""
    contract = MagicMock()
    contract.symbol = symbol
    contract.lastTradeDateOrContractMonth = expiry
    contract.strike = strike
    contract.right = right
    contract.conId = con_id

    greeks = MagicMock()
    greeks.delta = delta
    greeks.gamma = gamma
    greeks.theta = theta
    greeks.vega = vega
    greeks.impliedVol = implied_vol

    ticker = MagicMock()
    ticker.contract = contract
    ticker.bid = bid
    ticker.ask = ask
    ticker.last = last
    ticker.volume = volume
    ticker.openInterest = open_interest
    ticker.modelGreeks = greeks

    return ticker


# ---------------------------------------------------------------------------
# Unit tests: _clean helpers
# ---------------------------------------------------------------------------

def test_clean_nan_returns_none() -> None:
    assert _clean(float("nan")) is None


def test_clean_sentinel_returns_none() -> None:
    assert _clean(-1.0) is None


def test_clean_none_returns_none() -> None:
    assert _clean(None) is None


def test_clean_valid_value_passes_through() -> None:
    assert _clean(5.25) == 5.25


def test_clean_int_converts_float_to_int() -> None:
    assert _clean_int(1000.0) == 1000


def test_clean_int_nan_returns_none() -> None:
    assert _clean_int(float("nan")) is None


# ---------------------------------------------------------------------------
# Unit tests: OptionContract
# ---------------------------------------------------------------------------

def test_option_contract_mid_computed() -> None:
    contract = OptionContract(symbol="SPY", expiry="20260320", strike=580.0, right="C", bid=5.0, ask=5.2)
    assert contract.mid == 5.1


def test_option_contract_mid_none_when_bid_missing() -> None:
    contract = OptionContract(symbol="SPY", expiry="20260320", strike=580.0, right="C", ask=5.2)
    assert contract.mid is None


# ---------------------------------------------------------------------------
# Unit tests: filter helpers
# ---------------------------------------------------------------------------

def test_filter_expirations_returns_nearest(
    mock_ibkr_client: MagicMock, mock_chain_params: MagicMock
) -> None:
    fetcher = ChainFetcher(mock_ibkr_client)
    expiries, _ = fetcher._select_expirations_and_strikes(
        mock_chain_params, underlying_price=580.0, max_expiries=4, strike_range_pct=1.0
    )
    assert len(expiries) == 4
    assert expiries == sorted(expiries)
    assert expiries[0] == "20260320"


def test_filter_strikes_within_range(
    mock_ibkr_client: MagicMock, mock_chain_params: MagicMock
) -> None:
    fetcher = ChainFetcher(mock_ibkr_client)
    _, strikes = fetcher._select_expirations_and_strikes(
        mock_chain_params, underlying_price=500.0, max_expiries=6, strike_range_pct=0.10
    )
    # 10% of 500 = 50, so window is [450, 550]
    assert all(450.0 <= s <= 550.0 for s in strikes)
    assert 500.0 in strikes


# ---------------------------------------------------------------------------
# Unit tests: _parse_ticker
# ---------------------------------------------------------------------------

def test_parse_ticker_full_data(mock_ibkr_client: MagicMock) -> None:
    fetcher = ChainFetcher(mock_ibkr_client)
    ticker = make_fake_ticker()
    contract = fetcher._parse_ticker(ticker)

    assert contract.symbol == "SPY"
    assert contract.expiry == "20260320"
    assert contract.strike == 580.0
    assert contract.right == "C"
    assert contract.bid == 5.10
    assert contract.ask == 5.20
    assert contract.delta == 0.45
    assert contract.implied_vol == 0.20
    assert contract.volume == 1000


def test_parse_ticker_missing_greeks(mock_ibkr_client: MagicMock) -> None:
    fetcher = ChainFetcher(mock_ibkr_client)
    ticker = make_fake_ticker(bid=float("nan"), ask=-1.0)
    ticker.modelGreeks = None

    contract = fetcher._parse_ticker(ticker)

    assert contract.bid is None
    assert contract.ask is None
    assert contract.delta is None
    assert contract.implied_vol is None


# ---------------------------------------------------------------------------
# Unit tests: OptionChainSnapshot.to_dataframe
# ---------------------------------------------------------------------------

def test_snapshot_to_dataframe() -> None:
    contracts = [
        OptionContract(symbol="SPY", expiry="20260320", strike=580.0, right="C", bid=5.0, ask=5.2),
        OptionContract(symbol="SPY", expiry="20260320", strike=580.0, right="P", bid=4.8, ask=5.0),
        OptionContract(symbol="SPY", expiry="20260417", strike=570.0, right="C", bid=8.0, ask=8.2),
    ]
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=580.0,
        timestamp=datetime.now(timezone.utc),
        contracts=contracts,
    )
    df = snapshot.to_dataframe()

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
    assert set(["symbol", "expiry", "strike", "right", "bid", "ask", "mid"]).issubset(df.columns)


# ---------------------------------------------------------------------------
# Integration test (requires live TWS/Gateway)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_spy_chain_live() -> None:
    """Fetch a small SPY chain slice from live TWS and validate the snapshot.

    Requires TWS or IB Gateway running with API access enabled.
    Run with: pytest -m integration
    """
    from src.connection.ibkr_client import IBKRClient

    async with IBKRClient() as client:
        fetcher = ChainFetcher(client)
        snapshot = await fetcher.fetch_chain("SPY", max_expiries=2, strike_range_pct=0.05)

    assert snapshot.underlying == "SPY"
    assert snapshot.underlying_price > 0
    assert len(snapshot.contracts) > 0
    assert all(c.symbol == "SPY" for c in snapshot.contracts)
    assert all(c.right in ("C", "P") for c in snapshot.contracts)

    df = snapshot.to_dataframe()
    assert len(df) == len(snapshot.contracts)
