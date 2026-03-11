from __future__ import annotations

from collections import deque  # noqa: F401 — used by SentimentAggregator
from datetime import datetime, timedelta, timezone  # noqa: F401 — timedelta used by SentimentAggregator

from config.settings import Settings  # noqa: F401 — used by SentimentAggregator
from loguru import logger  # noqa: F401 — used by SentimentAggregator
from pydantic import BaseModel

from src.analysis.flow_classifier import Aggressor  # noqa: F401 — used by SentimentAggregator
from src.analysis.greeks_engine import EnrichedTrade, Moneyness  # noqa: F401 — used by SentimentAggregator


class SentimentSnapshot(BaseModel):
    """Rolling-window aggregate sentiment metrics for one underlying symbol.

    Emitted by SentimentAggregator.snapshot(). All fields cover only the
    trades seen in the configured rolling window (default 1 hour).

    Note on dollar sums: trades with premium=None contribute 0 to all
    dollar aggregates (call_premium, put_premium, bullish_premium, etc.).

    Note on IV skew: avg_call_iv and avg_put_iv are simple unweighted
    means across OTM trades in the window. This is a rough directional
    proxy, not a precise skew surface — IV varies by strike and expiry.

    Note on directional_bias vs net_premium: NEUTRAL-aggressor trades
    contribute to call_premium / put_premium (and thus net_premium) but
    NOT to bullish_premium / bearish_premium. A neutral-heavy session
    can show a non-zero net_premium alongside directional_bias=None.

    Attributes:
        symbol: Underlying ticker (e.g. "SPY").
        window_seconds: Lookback window used for this snapshot.
        computed_at: Wall-clock UTC time when snapshot() was called.
        trade_count: Total number of EnrichedTrade objects in the window.

        call_volume: Sum of volume_delta for call trades.
        put_volume: Sum of volume_delta for put trades.
        call_premium: Sum of premium dollars for call trades.
        put_premium: Sum of premium dollars for put trades.
        call_count: Number of call trade events in window.
        put_count: Number of put trade events in window.

        put_call_volume_ratio: put_volume / call_volume. None when call_volume == 0.
        put_call_premium_ratio: put_premium / call_premium. None when call_premium == 0.
        net_premium: call_premium - put_premium. Positive = bullish flow bias.

        avg_call_iv: Mean implied_vol of OTM call trades. None when unavailable.
        avg_put_iv: Mean implied_vol of OTM put trades. None when unavailable.
        iv_skew: avg_put_iv - avg_call_iv. Positive = elevated put demand.
            None when either average is unavailable.

        net_delta_exposure: Sum of (delta * aggressor_sign * volume_delta * 100).
            BUY=+1, SELL=-1, NEUTRAL excluded. None when all deltas are missing.
        net_gamma_exposure: Dealer net gamma exposure:
            sum(-gamma * aggressor_sign * volume_delta * 100 * underlying).
            Positive = dealers long gamma (price-stabilizing).
            None when all gammas or underlyings are missing.

        bullish_premium: Call BUY + Put SELL premium (long upside bets).
        bearish_premium: Put BUY + Call SELL premium (long downside bets).
        directional_bias: (bullish - bearish) / (bullish + bearish).
            Ranges [-1, 1]. Positive = bullish. None when no directional flow.
    """

    symbol: str
    window_seconds: float
    computed_at: datetime
    trade_count: int

    # Volume / premium breakdown
    call_volume: int
    put_volume: int
    call_premium: float
    put_premium: float
    call_count: int
    put_count: int

    # Ratio metrics
    put_call_volume_ratio: float | None
    put_call_premium_ratio: float | None
    net_premium: float

    # IV skew
    avg_call_iv: float | None
    avg_put_iv: float | None
    iv_skew: float | None

    # Exposure
    net_delta_exposure: float | None
    net_gamma_exposure: float | None

    # Directional flow
    bullish_premium: float
    bearish_premium: float
    directional_bias: float | None
