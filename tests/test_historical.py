from __future__ import annotations

import datetime as _dt
from datetime import timezone

import pytest


# ---------------------------------------------------------------------------
# T3 — HistoricalBar field validator
# ---------------------------------------------------------------------------


def test_historical_bar_rejects_naive_timestamp():
    from pydantic import ValidationError
    from src.data.historical import HistoricalBar

    with pytest.raises(ValidationError, match="timezone-aware"):
        HistoricalBar(
            timestamp=_dt.datetime(2026, 1, 15, 10, 30, 0),  # naive — no tzinfo
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
        )


def test_historical_bar_accepts_utc_timestamp():
    from src.data.historical import HistoricalBar

    ts = _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    bar = HistoricalBar(timestamp=ts, open=1.0, high=2.0, low=0.5, close=1.5)

    assert bar.timestamp == ts
    assert bar.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# T3 — _parse_bar_date
# ---------------------------------------------------------------------------


def test_parse_bar_date_naive_datetime():
    from src.data.historical import _parse_bar_date

    naive = _dt.datetime(2026, 1, 15, 10, 30, 0)
    result = _parse_bar_date(naive)

    assert result == _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_aware_datetime():
    from src.data.historical import _parse_bar_date

    aware = _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    result = _parse_bar_date(aware)

    assert result == _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_date_object():
    from src.data.historical import _parse_bar_date

    d = _dt.date(2026, 1, 15)
    result = _parse_bar_date(d)

    assert result == _dt.datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_8char_string():
    from src.data.historical import _parse_bar_date

    result = _parse_bar_date("20260115")

    assert result == _dt.datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_datetime_string():
    from src.data.historical import _parse_bar_date

    result = _parse_bar_date("20260115 10:30:00")

    assert result == _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_datetime_string_double_space():
    from src.data.historical import _parse_bar_date

    # Double space between date and time — normalized via " ".join(raw.split())
    result = _parse_bar_date("20260115  10:30:00")

    assert result == _dt.datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_parse_bar_date_invalid_string_raises():
    from src.data.historical import _parse_bar_date

    with pytest.raises(ValueError):
        _parse_bar_date("invalid_date")


# ---------------------------------------------------------------------------
# T3 — HistoricalBars.to_dataframe() and avg_daily_volume()
# ---------------------------------------------------------------------------


def _make_bars(bars_data: list[dict]):
    """Build a HistoricalBars instance for use in tests.

    Each dict in bars_data is passed as keyword overrides on top of sensible
    OHLC defaults. Supply ``volume`` to exercise volume-related behaviour.
    """
    from datetime import datetime, timezone
    from src.data.historical import HistoricalBars, HistoricalBar

    bars = [
        HistoricalBar(
            timestamp=datetime.now(timezone.utc),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            **d,
        )
        for d in bars_data
    ]
    return HistoricalBars(
        symbol="SPY",
        bar_size="1 day",
        what_to_show="TRADES",
        fetched_at=datetime.now(timezone.utc),
        bars=bars,
    )


def test_to_dataframe_empty_bars():
    import pandas as pd

    hbars = _make_bars([])
    df = hbars.to_dataframe()

    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_to_dataframe_with_bars():
    hbars = _make_bars([{"volume": 1000}, {"volume": 2000}])
    df = hbars.to_dataframe()

    # Index should be the timestamp column
    assert df.index.name == "timestamp"
    assert len(df) == 2

    for col in ("open", "high", "low", "close", "volume"):
        assert col in df.columns


def test_avg_daily_volume_all_none():
    hbars = _make_bars([{"volume": None}, {"volume": None}])
    result = hbars.avg_daily_volume()

    assert result is None


def test_avg_daily_volume_with_values():
    hbars = _make_bars([{"volume": 100}, {"volume": 200}, {"volume": 300}])
    result = hbars.avg_daily_volume()

    assert result == 200.0


def test_avg_daily_volume_filters_zero_volume():
    # Zeros must not be counted — only volume > 0 qualifies
    hbars = _make_bars([{"volume": 0}, {"volume": 0}, {"volume": 0}])
    result = hbars.avg_daily_volume()

    assert result is None


def test_avg_daily_volume_mixed_none_and_values():
    hbars = _make_bars([{"volume": None}, {"volume": 100}, {"volume": None}, {"volume": 300}])
    result = hbars.avg_daily_volume()

    assert result == 200.0


# ---------------------------------------------------------------------------
# T3 — fetch_bars validation
# ---------------------------------------------------------------------------


def _make_fetcher():
    """Return (HistoricalFetcher, mock_ib, mock_limiter) for unit tests."""
    from unittest.mock import MagicMock, AsyncMock
    from src.data.historical import HistoricalFetcher

    client = MagicMock()
    client.ib = MagicMock()
    client.ib.qualifyContractsAsync = AsyncMock()
    client.ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return HistoricalFetcher(client, limiter), client.ib, limiter


async def test_fetch_bars_invalid_bar_size_raises():
    fetcher, _ib, _limiter = _make_fetcher()

    with pytest.raises(ValueError, match="Invalid bar_size"):
        await fetcher.fetch_bars("SPY", bar_size="99 years")


async def test_fetch_bars_invalid_what_to_show_raises():
    fetcher, _ib, _limiter = _make_fetcher()

    with pytest.raises(ValueError, match="Invalid what_to_show"):
        await fetcher.fetch_bars("SPY", what_to_show="NONSENSE")


# ---------------------------------------------------------------------------
# T10 — _qualify_underlying() failure paths
# ---------------------------------------------------------------------------


async def test_qualify_underlying_empty_result_raises():
    from unittest.mock import AsyncMock

    fetcher, ib, _limiter = _make_fetcher()
    ib.qualifyContractsAsync = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="Could not qualify"):
        await fetcher._qualify_underlying("FAKESYM")


async def test_qualify_underlying_con_id_zero_raises():
    from unittest.mock import MagicMock, AsyncMock

    fetcher, ib, _limiter = _make_fetcher()
    contract = MagicMock()
    contract.conId = 0
    ib.qualifyContractsAsync = AsyncMock(return_value=[contract])

    with pytest.raises(ValueError, match="Could not qualify"):
        await fetcher._qualify_underlying("FAKESYM")
