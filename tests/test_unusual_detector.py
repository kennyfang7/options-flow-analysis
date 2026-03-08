from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.analysis.unusual_detector import UnusualReason, UnusualSignal
from src.data.tick_stream import TickUpdate


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_unusual_detector_settings_defaults():
    """All unusual detector settings load with correct defaults."""
    s = Settings()
    assert s.unusual_premium_threshold == 250_000.0
    assert s.unusual_oi_ratio_threshold == 0.50
    assert s.unusual_signal_threshold == 5.0
    assert s.otm_delta_threshold == 0.30
    assert s.otm_premium_threshold == 100_000.0


def test_unusual_premium_threshold_must_exceed_min_premium():
    """ValidationError when unusual_premium_threshold <= min_premium."""
    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=50.0)

    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=100.0)


def test_unusual_premium_threshold_valid_when_above_min_premium():
    """No error when unusual_premium_threshold > min_premium."""
    s = Settings(min_premium=100.0, unusual_premium_threshold=200.0)
    assert s.unusual_premium_threshold == 200.0


def test_oi_ratio_threshold_must_be_positive():
    """ValidationError when unusual_oi_ratio_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=0.0)

    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=-1.0)


def test_otm_delta_threshold_must_be_between_0_and_1():
    """ValidationError when otm_delta_threshold is 0 or 1."""
    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=0.0)

    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=1.0)


def test_unusual_signal_threshold_must_be_positive():
    """ValidationError when unusual_signal_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_signal_threshold must be greater than 0"):
        Settings(unusual_signal_threshold=0.0)


def make_tick(**overrides) -> TickUpdate:
    """Factory for TickUpdate with sensible defaults for unit tests."""
    defaults = dict(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        bid=2.00,
        ask=2.50,
        last=2.45,
        volume=100,
        open_interest=1000,
        last_size=50,
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.45,
    )
    defaults.update(overrides)
    return TickUpdate(**defaults)


def make_trade(tick: TickUpdate | None = None, **overrides) -> ClassifiedTrade:
    """Factory for ClassifiedTrade with sensible defaults for unit tests."""
    if tick is None:
        tick = make_tick()
    defaults = dict(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        right=tick.right,
        strike=tick.strike,
        underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol,
        delta=tick.delta,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        spread_position=0.9,
        effective_price=2.45,
        last_size=50,
        premium=12_250.0,   # 50 * 2.45 * 100
        signal_strength=1.0,
        volume_delta=50,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


# ---------------------------------------------------------------------------
# UnusualSignal model tests
# ---------------------------------------------------------------------------

def test_unusual_signal_constructs():
    """UnusualSignal builds from all required fields."""
    trade = make_trade()
    signal = UnusualSignal(
        symbol=trade.symbol,
        con_id=trade.con_id,
        expiry=trade.expiry,
        right=trade.right,
        strike=trade.strike,
        trade_type=trade.trade_type,
        aggressor=trade.aggressor,
        premium=trade.premium,
        volume_delta=trade.volume_delta,
        signal_strength=trade.signal_strength,
        delta=trade.delta,
        underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol,
        effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        trade=trade,
    )
    assert signal.top_reason == UnusualReason.PREMIUM_SIZE
    assert signal.symbol == "SPY"


def test_unusual_signal_trade_excluded_from_serialization():
    """trade field is excluded from model_dump()."""
    trade = make_trade()
    signal = UnusualSignal(
        symbol=trade.symbol,
        con_id=trade.con_id,
        expiry=trade.expiry,
        right=trade.right,
        strike=trade.strike,
        trade_type=trade.trade_type,
        aggressor=trade.aggressor,
        premium=trade.premium,
        volume_delta=trade.volume_delta,
        signal_strength=trade.signal_strength,
        delta=trade.delta,
        underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol,
        effective_price=trade.effective_price,
        reasons=[UnusualReason.OI_RATIO],
        top_reason=UnusualReason.OI_RATIO,
        flagged_at=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        trade=trade,
    )
    dumped = signal.model_dump()
    assert "trade" not in dumped
    assert "symbol" in dumped
