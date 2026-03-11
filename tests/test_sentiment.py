from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest


def make_snapshot(**kwargs):
    from src.analysis.sentiment import SentimentSnapshot
    defaults = dict(
        symbol="SPY",
        window_seconds=3600.0,
        computed_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade_count=10,
        call_volume=500,
        put_volume=300,
        call_premium=100_000.0,
        put_premium=60_000.0,
        call_count=6,
        put_count=4,
        put_call_volume_ratio=0.6,
        put_call_premium_ratio=0.6,
        net_premium=40_000.0,
        avg_call_iv=None,
        avg_put_iv=None,
        iv_skew=None,
        net_delta_exposure=None,
        net_gamma_exposure=None,
        bullish_premium=80_000.0,
        bearish_premium=40_000.0,
        directional_bias=None,
    )
    defaults.update(kwargs)
    return SentimentSnapshot(**defaults)


def test_sentiment_snapshot_construction():
    snap = make_snapshot()
    assert snap.symbol == "SPY"
    assert snap.net_premium == 40_000.0


def test_sentiment_snapshot_optional_fields_none():
    snap = make_snapshot()
    assert snap.avg_call_iv is None
    assert snap.iv_skew is None
    assert snap.net_delta_exposure is None


def test_sentiment_snapshot_put_call_ratio_none_when_no_calls():
    snap = make_snapshot(put_call_volume_ratio=None, call_volume=0)
    assert snap.put_call_volume_ratio is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trade(
    symbol="SPY",
    right="C",
    aggressor_str="buy",
    premium=10_000.0,
    volume_delta=100,
    delta=0.5,
    implied_vol=0.25,
    gamma=None,
    underlying_price=500.0,
    moneyness_str="otm",
    timestamp=None,
):
    """Build a minimal EnrichedTrade for testing SentimentAggregator.

    Constructs a real TickUpdate and EnrichedTrade via pydantic.
    Note: tick.theta and tick.vega are None (not passed); EnrichedTrade
    theta/vega are explicitly set to None as well. This is intentional —
    sentiment tests do not exercise the Greeks fallback path.
    """
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.data.tick_stream import TickUpdate

    ts = timestamp or datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc)

    aggressor_map = {"buy": Aggressor.BUY, "sell": Aggressor.SELL, "neutral": Aggressor.NEUTRAL}
    moneyness_map = {
        "itm": Moneyness.ITM, "atm": Moneyness.ATM,
        "otm": Moneyness.OTM, "unknown": Moneyness.UNKNOWN,
    }

    tick = TickUpdate(
        symbol=symbol, con_id=99001, expiry="20260620", strike=520.0, right=right,
        timestamp=ts, bid=1.0, ask=1.5, last=1.45,
        volume=volume_delta, open_interest=1000, last_size=volume_delta,
        underlying_price=underlying_price, implied_vol=implied_vol,
        delta=delta, gamma=gamma,
    )

    return EnrichedTrade(
        symbol=symbol,
        con_id=99001,
        expiry="20260620",
        right=right,
        strike=520.0,
        underlying_price=underlying_price,
        implied_vol=implied_vol,
        delta=delta,
        trade_type=TradeType.BLOCK,
        aggressor=aggressor_map[aggressor_str],
        spread_position=0.8 if aggressor_str == "buy" else 0.2,
        effective_price=premium / (volume_delta * 100),
        last_size=volume_delta,
        premium=premium,
        signal_strength=3.0,
        volume_delta=volume_delta,
        window_ticks=1,
        timestamp=ts,
        tick=tick,
        gamma=gamma,
        theta=None,
        vega=None,
        days_to_expiry=90,
        moneyness=moneyness_map[moneyness_str],
        iv_source="ibkr",
    )


def make_aggregator(window_seconds=3600.0):
    from src.analysis.sentiment import SentimentAggregator
    from config.settings import Settings
    s = Settings(
        min_premium=100.0,
        unusual_premium_threshold=250_000.0,
        sentiment_window_seconds=window_seconds,
    )
    return SentimentAggregator(s)


# ---------------------------------------------------------------------------
# update() + snapshot() — core metrics
# ---------------------------------------------------------------------------

def test_snapshot_returns_none_for_unknown_symbol():
    agg = make_aggregator()
    assert agg.snapshot("AAPL") is None


def test_snapshot_returns_none_after_all_trades_expire():
    """snapshot() returns None when all window entries are pruned."""
    agg = make_aggregator(window_seconds=60.0)
    # Use datetime.now() minus a large offset so the trade is always in the
    # past regardless of the machine's system clock (avoids hardcoded 2026 dates
    # that can be "future" on machines whose clock hasn't advanced far enough).
    old_ts = datetime.now(timezone.utc) - timedelta(hours=4)
    agg.update(make_trade(timestamp=old_ts))
    snap = agg.snapshot("SPY")
    assert snap is None


def test_single_call_buy_snapshot():
    agg = make_aggregator()
    agg.update(make_trade(right="C", aggressor_str="buy", premium=10_000.0, volume_delta=100))
    snap = agg.snapshot("SPY")
    assert snap is not None
    assert snap.symbol == "SPY"
    assert snap.trade_count == 1
    assert snap.call_count == 1
    assert snap.put_count == 0
    assert snap.call_volume == 100
    assert snap.put_volume == 0
    assert snap.call_premium == pytest.approx(10_000.0)
    assert snap.put_premium == pytest.approx(0.0)
    assert snap.net_premium == pytest.approx(10_000.0)


def test_put_call_volume_ratio():
    agg = make_aggregator()
    agg.update(make_trade(right="C", volume_delta=100, premium=10_000.0))
    agg.update(make_trade(right="P", volume_delta=50, premium=5_000.0))
    snap = agg.snapshot("SPY")
    assert snap.put_call_volume_ratio == pytest.approx(0.5, abs=1e-6)


def test_put_call_premium_ratio():
    agg = make_aggregator()
    agg.update(make_trade(right="C", premium=20_000.0, volume_delta=100))
    agg.update(make_trade(right="P", premium=10_000.0, volume_delta=100))
    snap = agg.snapshot("SPY")
    assert snap.put_call_premium_ratio == pytest.approx(0.5, abs=1e-6)


def test_put_call_volume_ratio_none_when_no_calls():
    agg = make_aggregator()
    agg.update(make_trade(right="P", volume_delta=100, premium=10_000.0))
    snap = agg.snapshot("SPY")
    assert snap.put_call_volume_ratio is None
    assert snap.put_call_premium_ratio is None


def test_net_premium_mixed():
    agg = make_aggregator()
    agg.update(make_trade(right="C", premium=30_000.0, volume_delta=100))
    agg.update(make_trade(right="P", premium=10_000.0, volume_delta=100))
    snap = agg.snapshot("SPY")
    assert snap.net_premium == pytest.approx(20_000.0)


def test_trades_outside_window_excluded():
    """Only trades within the rolling window contribute to the snapshot."""
    agg = make_aggregator(window_seconds=60.0)
    # Pin timestamps so test is deterministic regardless of CI timing
    anchor = datetime(2026, 3, 11, 14, 30, 0, tzinfo=timezone.utc)
    old = anchor - timedelta(seconds=120)   # 2 minutes before anchor → outside 60s window
    fresh = anchor                          # at anchor → inside window
    agg.update(make_trade(timestamp=old, right="P", premium=50_000.0, volume_delta=200))
    agg.update(make_trade(timestamp=fresh, right="C", premium=10_000.0, volume_delta=100))
    # Verify via internal window state: _prune() uses trade.timestamp (not now),
    # so when the fresh trade (at anchor) is added, _prune(symbol, anchor) runs
    # and removes any trade older than anchor - 60s = old. This is timestamp-relative,
    # not wall-clock relative. snapshot() is not called here because it prunes against
    # now(), which would also remove the pinned-past fresh trade in CI.
    # Verify via the internal window state:
    window = list(agg._windows.get("SPY", []))
    old_still_present = any(t.timestamp == old for t in window)
    assert not old_still_present


def test_different_symbols_isolated():
    """Trades for one symbol do not pollute another symbol's snapshot."""
    agg = make_aggregator()
    agg.update(make_trade(symbol="SPY", right="C", premium=10_000.0))
    agg.update(make_trade(symbol="AAPL", right="P", premium=20_000.0))
    spy_snap = agg.snapshot("SPY")
    aapl_snap = agg.snapshot("AAPL")
    assert spy_snap is not None and spy_snap.put_count == 0
    assert aapl_snap is not None and aapl_snap.call_count == 0
