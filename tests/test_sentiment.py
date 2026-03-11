from __future__ import annotations
from datetime import datetime, timezone


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
