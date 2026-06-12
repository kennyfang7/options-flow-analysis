from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.analysis.unusual_detector import UnusualDetector, UnusualReason, UnusualSignal
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
        timestamp=datetime.now(timezone.utc),
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


@pytest.fixture
def unusual_settings() -> Settings:
    """Settings with low thresholds so test trades qualify easily."""
    return Settings(
        min_premium=100.0,
        min_block_size=500,
        unusual_premium_threshold=500.0,     # > min_premium=100
        unusual_oi_ratio_threshold=0.50,
        unusual_signal_threshold=5.0,
        otm_delta_threshold=0.30,
        otm_premium_threshold=200.0,
    )


@pytest.fixture
def detector(unusual_settings) -> UnusualDetector:
    return UnusualDetector(unusual_settings)


# ---------------------------------------------------------------------------
# detect(): returns None when no conditions fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_returns_none_when_no_conditions_fire(detector):
    """Trade below all thresholds → None."""
    # premium=12250 > 500... wait, make_trade default premium=12_250.0
    # We need premium below 500. Override it.
    trade = make_trade(premium=150.0, signal_strength=1.0, delta=0.45)
    assert await detector.detect(trade) is None


@pytest.mark.asyncio
async def test_unusual_detector_processes_multi_leg_trade(detector):
    """MULTI_LEG trades now flow through detect() and can fire conditions."""
    tick = make_tick(last_size=100, volume=100, open_interest=100,
                     bid=5.00, ask=5.50, last=5.45, delta=0.25)
    trade = make_trade(tick=tick, trade_type=TradeType.MULTI_LEG,
                       premium=600.0, volume_delta=100, signal_strength=1.0)
    signal = await detector.detect(trade)
    assert signal is not None
    assert UnusualReason.PREMIUM_SIZE in signal.reasons


@pytest.mark.asyncio
async def test_detect_returns_none_when_oi_cache_empty_and_no_oi_on_tick(detector):
    """OI_RATIO cannot fire when cache is empty and tick has no OI."""
    tick = make_tick(open_interest=None)
    trade = make_trade(tick=tick, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None  # no OI cache, no other conditions


# ---------------------------------------------------------------------------
# detect(): PREMIUM_SIZE condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_premium_size_fires(detector):
    """premium >= unusual_premium_threshold → PREMIUM_SIZE."""
    # unusual_premium_threshold=500.0; set premium=600.0
    trade = make_trade(premium=600.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.PREMIUM_SIZE in result.reasons
    assert result.top_reason == UnusualReason.PREMIUM_SIZE


@pytest.mark.asyncio
async def test_detect_premium_size_does_not_fire_below_threshold(detector):
    """premium < unusual_premium_threshold → PREMIUM_SIZE does not fire."""
    trade = make_trade(premium=400.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_premium_size_does_not_fire_when_premium_none(detector):
    """premium=None → PREMIUM_SIZE silently skipped."""
    trade = make_trade(premium=None, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): OI_RATIO condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_oi_ratio_fires_from_live_tick(detector):
    """volume_delta / tick.open_interest >= threshold → OI_RATIO."""
    # OI=100, volume_delta=60 → ratio=0.60 >= 0.50
    tick = make_tick(open_interest=100)
    trade = make_trade(tick=tick, volume_delta=60, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OI_RATIO in result.reasons


@pytest.mark.asyncio
async def test_detect_oi_ratio_uses_cached_oi(detector):
    """OI from prior tick is used even when current tick has OI=None."""
    # First tick populates cache with OI=100
    tick1 = make_tick(open_interest=100)
    trade1 = make_trade(tick=tick1, volume_delta=10, premium=150.0, signal_strength=1.0, delta=0.45)
    await detector.detect(trade1)

    # Second tick has OI=None but cache still has 100
    tick2 = make_tick(open_interest=None)
    trade2 = make_trade(tick=tick2, volume_delta=60, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade2)
    assert result is not None
    assert UnusualReason.OI_RATIO in result.reasons


@pytest.mark.asyncio
async def test_detect_oi_ratio_does_not_fire_below_threshold(detector):
    """volume_delta / OI < threshold → OI_RATIO does not fire."""
    # OI=1000, volume_delta=10 → ratio=0.01 < 0.50
    tick = make_tick(open_interest=1000)
    trade = make_trade(tick=tick, volume_delta=10, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): SIGNAL_STRENGTH condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_signal_strength_fires(detector):
    """signal_strength >= unusual_signal_threshold → SIGNAL_STRENGTH."""
    trade = make_trade(signal_strength=6.0, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.SIGNAL_STRENGTH in result.reasons


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_below_threshold(detector):
    """signal_strength < threshold → SIGNAL_STRENGTH does not fire."""
    trade = make_trade(signal_strength=4.9, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_when_none(detector):
    """signal_strength=None → SIGNAL_STRENGTH silently skipped (no open_interest)."""
    trade = make_trade(signal_strength=None, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_at_zero(detector):
    """signal_strength=0.0 must not pass threshold=5.0 (truthiness trap guard)."""
    trade = make_trade(signal_strength=0.0, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): OTM_PREMIUM condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_otm_premium_fires(detector):
    """|delta|=0.20 (OTM) and premium=300.0 >= otm_premium_threshold=200.0 → OTM_PREMIUM."""
    trade = make_trade(delta=0.20, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OTM_PREMIUM in result.reasons


@pytest.mark.asyncio
async def test_detect_otm_premium_fires_for_put(detector):
    """Put delta is negative; abs() used correctly."""
    trade = make_trade(delta=-0.20, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OTM_PREMIUM in result.reasons


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_when_itm(detector):
    """|delta|=0.70 (ITM) → OTM_PREMIUM does not fire even with large premium."""
    trade = make_trade(delta=0.70, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_below_otm_threshold(detector):
    """|delta|=0.20 (OTM) but premium=150.0 < otm_premium_threshold=200.0."""
    trade = make_trade(delta=0.20, premium=150.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_when_delta_none(detector):
    """delta=None → OTM check silently skipped."""
    trade = make_trade(delta=None, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): multiple reasons + top_reason priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_multiple_reasons(detector):
    """Trade qualifying on >1 condition produces all reasons."""
    # PREMIUM_SIZE: premium=600 >= 500
    # SIGNAL_STRENGTH: signal_strength=6.0 >= 5.0
    trade = make_trade(premium=600.0, signal_strength=6.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.PREMIUM_SIZE in result.reasons
    assert UnusualReason.SIGNAL_STRENGTH in result.reasons


@pytest.mark.asyncio
async def test_detect_top_reason_priority(detector):
    """PREMIUM_SIZE outranks OI_RATIO; OI_RATIO outranks SIGNAL_STRENGTH."""
    tick = make_tick(open_interest=100)
    # PREMIUM_SIZE: 600 >= 500, OI_RATIO: 60/100=0.60 >= 0.50, SIGNAL_STRENGTH: 6.0 >= 5.0
    trade = make_trade(
        tick=tick, premium=600.0, volume_delta=60, signal_strength=6.0, delta=0.45
    )
    result = await detector.detect(trade)
    assert result is not None
    assert result.top_reason == UnusualReason.PREMIUM_SIZE

    # Now remove PREMIUM_SIZE — OI_RATIO should be top
    trade2 = make_trade(tick=tick, premium=150.0, volume_delta=60, signal_strength=6.0, delta=0.45)
    result2 = await detector.detect(trade2)
    assert result2 is not None
    assert result2.top_reason == UnusualReason.OI_RATIO


# ---------------------------------------------------------------------------
# detect(): signal fields on result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_signal_has_correct_fields(detector):
    """UnusualSignal contains correct identity and trade fields."""
    trade = make_trade(premium=600.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert result.symbol == "SPY"
    assert result.con_id == 12345
    assert result.premium == pytest.approx(600.0)
    assert result.trade is trade  # same object reference


@pytest.mark.asyncio
async def test_detect_flagged_at_is_set(detector):
    """flagged_at is a timezone-aware datetime set during detect()."""
    trade = make_trade(premium=600.0)
    result = await detector.detect(trade)
    assert result is not None
    assert result.flagged_at.tzinfo is not None


# ---------------------------------------------------------------------------
# purge_stale
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_stale_evicts_old_entries(detector):
    """purge_stale() evicts con_ids not seen within max_age_seconds."""
    old_tick = make_tick(con_id=111, open_interest=500,
                         timestamp=datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc))
    trade = make_trade(
        tick=old_tick, con_id=111, premium=600.0,
        timestamp=datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    await detector.detect(trade)
    assert 111 in detector._oi_cache

    purged = detector.purge_stale(max_age_seconds=1.0)
    assert purged == 1
    assert 111 not in detector._oi_cache


@pytest.mark.asyncio
async def test_purge_stale_keeps_recent_entries(detector):
    """purge_stale() does not evict recently-seen con_ids."""
    trade = make_trade(premium=600.0)
    await detector.detect(trade)
    purged = detector.purge_stale(max_age_seconds=86400.0)
    assert purged == 0
    assert 12345 in detector._oi_cache
