from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_settings_flow_classifier_defaults():
    """New flow classifier fields load with correct defaults."""
    s = Settings()
    assert s.sweep_window_seconds == 2.0
    assert s.sweep_min_legs == 3
    assert s.split_window_seconds == 5.0
    assert s.split_min_legs == 3
    assert s.split_size_tolerance == 0.20
    assert s.classifier_window_seconds == 30.0
    assert s.aggressor_buy_threshold == 0.70
    assert s.aggressor_sell_threshold == 0.30


def test_settings_min_premium_must_be_positive():
    """Settings raises ValidationError when min_premium <= 0."""
    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=0.0)

    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=-1.0)


def test_settings_min_premium_positive_is_valid():
    """Settings accepts any positive min_premium."""
    s = Settings(min_premium=1.0)
    assert s.min_premium == 1.0


def test_settings_aggressor_thresholds_must_be_ordered():
    """Settings raises ValidationError when buy threshold <= sell threshold."""
    with pytest.raises(ValidationError, match="aggressor_buy_threshold must be greater than aggressor_sell_threshold"):
        Settings(aggressor_buy_threshold=0.30, aggressor_sell_threshold=0.70)


# ---------------------------------------------------------------------------
# Task 2: ClassifiedTrade model tests
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.data.tick_stream import TickUpdate


def make_tick(**overrides) -> TickUpdate:
    """Factory for TickUpdate with sensible defaults for unit tests."""
    defaults = dict(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc),
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


def test_classified_trade_constructs():
    """ClassifiedTrade builds correctly from all required fields."""
    tick = make_tick()
    trade = ClassifiedTrade(
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
        premium=50 * 2.45 * 100,
        signal_strength=1.5,
        volume_delta=50,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    assert trade.trade_type == TradeType.BLOCK
    assert trade.aggressor == Aggressor.BUY
    assert trade.premium == pytest.approx(12250.0)


def test_classified_trade_tick_excluded_from_serialization():
    """tick field is excluded from model_dump()."""
    tick = make_tick()
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=None,
        implied_vol=None, delta=None, trade_type=TradeType.UNKNOWN,
        aggressor=Aggressor.NEUTRAL, spread_position=None,
        effective_price=None, last_size=None, premium=None,
        signal_strength=None, volume_delta=0, window_ticks=1,
        timestamp=tick.timestamp, tick=tick,
    )
    dumped = trade.model_dump()
    assert "tick" not in dumped


# ---------------------------------------------------------------------------
# Task 3: Helper function tests
# ---------------------------------------------------------------------------

from src.analysis.flow_classifier import _all_same_aggressor, _sizes_within_tolerance


def test_all_same_aggressor_all_buy():
    entries = [(make_tick(), Aggressor.BUY)] * 3
    assert _all_same_aggressor(entries) is True

def test_all_same_aggressor_all_sell():
    entries = [(make_tick(), Aggressor.SELL)] * 3
    assert _all_same_aggressor(entries) is True

def test_all_same_aggressor_mixed():
    entries = [(make_tick(), Aggressor.BUY), (make_tick(), Aggressor.SELL), (make_tick(), Aggressor.BUY)]
    assert _all_same_aggressor(entries) is False

def test_all_same_aggressor_neutral_ignored():
    entries = [(make_tick(), Aggressor.BUY), (make_tick(), Aggressor.NEUTRAL), (make_tick(), Aggressor.BUY)]
    assert _all_same_aggressor(entries) is True

def test_all_same_aggressor_all_neutral():
    entries = [(make_tick(), Aggressor.NEUTRAL)] * 3
    assert _all_same_aggressor(entries) is False

def test_sizes_within_tolerance_uniform():
    entries = [(make_tick(last_size=100), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is True

def test_sizes_within_tolerance_within_20pct():
    entries = [(make_tick(last_size=100), Aggressor.BUY), (make_tick(last_size=110), Aggressor.BUY), (make_tick(last_size=115), Aggressor.BUY)]
    assert _sizes_within_tolerance(entries, 0.20) is True

def test_sizes_within_tolerance_outside_20pct():
    entries = [(make_tick(last_size=100), Aggressor.BUY), (make_tick(last_size=110), Aggressor.BUY), (make_tick(last_size=200), Aggressor.BUY)]
    assert _sizes_within_tolerance(entries, 0.20) is False

def test_sizes_within_tolerance_zero_median():
    entries = [(make_tick(last_size=0), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is False

def test_sizes_within_tolerance_none_sizes_skipped():
    entries = [(make_tick(last_size=None), Aggressor.BUY), (make_tick(last_size=100), Aggressor.BUY), (make_tick(last_size=105), Aggressor.BUY)]
    assert _sizes_within_tolerance(entries, 0.20) is True

def test_sizes_within_tolerance_all_none():
    entries = [(make_tick(last_size=None), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is False


# ---------------------------------------------------------------------------
# Task 4: FlowClassifier tests
# ---------------------------------------------------------------------------

from src.analysis.flow_classifier import FlowClassifier


@pytest.fixture
def flow_settings() -> Settings:
    return Settings(
        min_premium=100.0,
        min_block_size=500,
        sweep_window_seconds=2.0,
        sweep_min_legs=3,
        split_window_seconds=5.0,
        split_min_legs=3,
        split_size_tolerance=0.20,
        classifier_window_seconds=30.0,
        aggressor_buy_threshold=0.70,
        aggressor_sell_threshold=0.30,
    )


@pytest.fixture
def classifier(flow_settings) -> FlowClassifier:
    return FlowClassifier(flow_settings)


def test_classify_aggressor_buy(classifier):
    tick = make_tick(bid=2.00, ask=2.50, last=2.45, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.BUY
    assert result.spread_position == pytest.approx(0.90)

def test_classify_aggressor_sell(classifier):
    tick = make_tick(bid=2.00, ask=2.50, last=2.05, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.SELL

def test_classify_aggressor_neutral(classifier):
    tick = make_tick(bid=2.00, ask=2.50, last=2.25, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL

def test_classify_aggressor_neutral_locked_market(classifier):
    tick = make_tick(bid=2.00, ask=2.00, last=2.00, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL
    assert result.spread_position is None

def test_classify_aggressor_above_ask(classifier):
    tick = make_tick(bid=2.00, ask=2.50, last=2.70, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.BUY
    assert result.spread_position == pytest.approx(1.40)

def test_classify_aggressor_neutral_when_no_bid_ask(classifier):
    tick = make_tick(bid=None, ask=2.50, last=2.45, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL
    assert result.spread_position is None

def test_classify_returns_none_when_last_size_none(classifier):
    assert classifier.classify(make_tick(last_size=None, volume=50)) is None

def test_classify_returns_none_when_last_none(classifier):
    assert classifier.classify(make_tick(last=None, last_size=50, volume=50)) is None

def test_classify_returns_none_when_volume_none(classifier):
    assert classifier.classify(make_tick(volume=None, last_size=50)) is None

def test_classify_returns_none_when_volume_delta_zero(classifier):
    tick = make_tick(volume=100, last_size=50)
    classifier.classify(tick)
    assert classifier.classify(make_tick(volume=100, last_size=50)) is None

def test_classify_returns_none_below_min_premium(classifier):
    tick = make_tick(last_size=1, last=0.50, bid=0.45, ask=0.55, volume=1)
    assert classifier.classify(tick) is None

def test_classify_returns_none_no_effective_price(classifier):
    tick = make_tick(bid=None, ask=None, last=None, last_size=50, volume=50)
    assert classifier.classify(tick) is None

def test_classify_volume_delta_computed_correctly(classifier):
    classifier.classify(make_tick(volume=100, last_size=100))
    result = classifier.classify(make_tick(volume=150, last_size=50))
    assert result is not None
    assert result.volume_delta == 50

def test_classify_session_reset_uses_last_size(classifier):
    classifier.classify(make_tick(volume=5000, last_size=50))
    result = classifier.classify(make_tick(volume=10, last_size=30))
    assert result is not None
    assert result.volume_delta == 30

def test_classify_block(classifier):
    tick = make_tick(last_size=600, volume=600, bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.BLOCK
    assert result.window_ticks == 1

def test_classify_unknown_small_single_print(classifier):
    tick = make_tick(last_size=10, volume=10, bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.UNKNOWN

def test_classify_sweep(classifier):
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)
    result = None
    for i, offset_ms in enumerate([0, 500, 1000]):
        ts = base_time + timedelta(milliseconds=offset_ms)
        tick = make_tick(last_size=50, volume=50*(i+1), bid=2.00, ask=2.50, last=2.45, timestamp=ts)
        result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.SWEEP
    assert result.window_ticks == 3

def test_classify_sweep_requires_same_aggressor(classifier):
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)
    result = None
    for i, (offset_ms, last_price) in enumerate([(0, 2.45), (500, 2.05), (1000, 2.45)]):
        ts = base_time + timedelta(milliseconds=offset_ms)
        tick = make_tick(last_size=50, volume=50*(i+1), bid=2.00, ask=2.50, last=last_price, timestamp=ts)
        result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type != TradeType.SWEEP

def test_classify_split(classifier):
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)
    result = None
    for i, (offset_ms, last_price, size) in enumerate([(0, 2.45, 100), (3000, 2.05, 100), (4000, 2.25, 100)]):
        ts = base_time + timedelta(milliseconds=offset_ms)
        tick = make_tick(last_size=size, volume=size*(i+1), bid=2.00, ask=2.50, last=last_price, timestamp=ts)
        result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.SPLIT
    assert result.window_ticks == 3

def test_classify_signal_strength_none_when_no_oi(classifier):
    result = classifier.classify(make_tick(last_size=50, volume=50, open_interest=None))
    assert result is not None
    assert result.signal_strength is None

def test_classify_signal_strength_positive(classifier):
    result = classifier.classify(make_tick(last_size=50, volume=50, open_interest=1000, bid=2.00, ask=2.50, last=2.45))
    assert result is not None
    assert result.signal_strength is not None
    assert result.signal_strength > 0

def test_classify_signal_strength_capped_at_10x_oi(classifier):
    from math import log1p
    result = classifier.classify(make_tick(last_size=50, volume=50, open_interest=1, bid=2.00, ask=2.50, last=2.45))
    assert result is not None
    expected_max = log1p(result.premium / 100.0) * 10.0
    assert result.signal_strength == pytest.approx(expected_max)

def test_classify_effective_price_uses_last_when_in_spread(classifier):
    result = classifier.classify(make_tick(bid=2.00, ask=2.50, last=2.45, last_size=50, volume=50))
    assert result is not None
    assert result.effective_price == pytest.approx(2.45)

def test_classify_effective_price_falls_back_to_mid(classifier):
    result = classifier.classify(make_tick(bid=2.00, ask=2.50, last=2.70, last_size=50, volume=50))
    assert result is not None
    assert result.effective_price == pytest.approx(2.25)

def test_purge_stale_removes_old_entries(classifier):
    # Use timestamps well in the past (2020) so they are unambiguously stale
    # on any machine regardless of local timezone or system clock.
    classifier.classify(make_tick(con_id=111, timestamp=datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc), volume=50, last_size=50))
    classifier.classify(make_tick(con_id=222, timestamp=datetime(2020, 1, 1, 14, 30, 0, tzinfo=timezone.utc), volume=50, last_size=50))
    assert 111 in classifier._last_volume
    assert 222 in classifier._last_volume
    purged = classifier.purge_stale(max_age_seconds=1.0)
    assert purged == 2

def test_purge_stale_returns_zero_when_nothing_stale(classifier):
    classifier.classify(make_tick(volume=50, last_size=50))
    assert classifier.purge_stale(max_age_seconds=86400.0) == 0
