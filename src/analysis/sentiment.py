from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from config.settings import Settings
from loguru import logger
from pydantic import BaseModel

from src.analysis.flow_classifier import Aggressor, TradeType
from src.analysis.greeks_engine import EnrichedTrade, Moneyness


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


_AGGRESSOR_SIGN: dict[Aggressor, float] = {
    Aggressor.BUY: 1.0,
    Aggressor.SELL: -1.0,
    Aggressor.NEUTRAL: 0.0,
}


class SentimentAggregator:
    """Rolling-window sentiment aggregator for options flow.

    Maintains a per-symbol deque of EnrichedTrade objects. Trades older
    than `sentiment_window_seconds` are automatically pruned on each
    update() or snapshot() call.

    **Timestamp ordering:** update() prunes against trade.timestamp. Trades
    must arrive in non-decreasing timestamp order. Out-of-order ticks will
    survive until the next snapshot() call (which always prunes against now).

    update() is synchronous and performs no IO. snapshot() computes
    metrics on demand from the live window.

    The orchestration layer should call purge_stale() hourly to free
    memory for symbols that have stopped receiving flow.

    Note: purge_stale() evicts per symbol (string keys), while
    FlowClassifier.purge_stale() and UnusualDetector.purge_stale() evict
    per con_id (int keys). Their return values count different unit types.

    Example:
        agg = SentimentAggregator(settings)
        agg.update(enriched_trade)
        snap = agg.snapshot("SPY")
        if snap:
            logger.info("SPY P/C ratio: {}", snap.put_call_volume_ratio)

    Args:
        settings: Application settings (uses sentiment_window_seconds).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._windows: dict[str, deque[EnrichedTrade]] = {}

    def update(self, trade: EnrichedTrade) -> None:
        """Add an EnrichedTrade to the rolling window and prune expired entries.

        Prunes using trade.timestamp as reference. Assumes trades arrive in
        non-decreasing timestamp order.

        Args:
            trade: EnrichedTrade from GreeksEngine.enrich().
        """
        symbol = trade.symbol
        if symbol not in self._windows:
            self._windows[symbol] = deque()
        self._windows[symbol].append(trade)
        self._prune(symbol, trade.timestamp)

    def _prune(self, symbol: str, reference_time: datetime) -> None:
        """Remove trades older than sentiment_window_seconds from the deque.

        Args:
            symbol: Symbol whose window to prune.
            reference_time: Timestamp to prune against. Typically trade.timestamp
                from update() or datetime.now(timezone.utc) from snapshot().
        """
        cutoff = reference_time - timedelta(seconds=self._settings.sentiment_window_seconds)
        window = self._windows[symbol]
        while window and window[0].timestamp < cutoff:
            window.popleft()

    def snapshot(self, symbol: str) -> SentimentSnapshot | None:
        """Compute current sentiment metrics for a symbol.

        Prunes expired entries against datetime.now() before computing.
        Returns None if the symbol has no trades in the current window.

        Args:
            symbol: Underlying ticker to aggregate (e.g. "SPY").

        Returns:
            SentimentSnapshot with all metrics populated, or None if no data.
        """
        if symbol not in self._windows:
            return None

        now = datetime.now(timezone.utc)
        self._prune(symbol, now)

        window = list(self._windows[symbol])
        if not window:
            return None

        # --- Volume / premium breakdown ---
        call_volume = 0
        put_volume = 0
        call_premium = 0.0
        put_premium = 0.0
        call_count = 0
        put_count = 0

        for t in window:
            prem = t.premium if t.premium is not None else 0.0  # premium=None treated as 0
            vol = t.volume_delta
            if t.right == "C":
                call_volume += vol
                call_premium += prem
                call_count += 1
            else:
                put_volume += vol
                put_premium += prem
                put_count += 1

        # --- Ratios ---
        put_call_volume_ratio = (put_volume / call_volume) if call_volume > 0 else None
        put_call_premium_ratio = (put_premium / call_premium) if call_premium > 0 else None
        net_premium = call_premium - put_premium

        # --- IV skew (OTM-only, unweighted mean — rough proxy) ---
        otm_call_ivs = [
            t.implied_vol for t in window
            if t.right == "C"
            and t.moneyness == Moneyness.OTM
            and t.implied_vol is not None
        ]
        otm_put_ivs = [
            t.implied_vol for t in window
            if t.right == "P"
            and t.moneyness == Moneyness.OTM
            and t.implied_vol is not None
        ]
        avg_call_iv = sum(otm_call_ivs) / len(otm_call_ivs) if otm_call_ivs else None
        avg_put_iv = sum(otm_put_ivs) / len(otm_put_ivs) if otm_put_ivs else None
        iv_skew = (
            (avg_put_iv - avg_call_iv)
            if avg_call_iv is not None and avg_put_iv is not None
            else None
        )

        # --- Delta / gamma exposure ---
        delta_contributions: list[float] = []
        gamma_contributions: list[float] = []
        for t in window:
            sign = (0.0 if t.trade_type == TradeType.MULTI_LEG
                    else _AGGRESSOR_SIGN[t.aggressor])
            if sign == 0.0:
                continue
            if t.delta is not None:
                delta_contributions.append(t.delta * sign * t.volume_delta * 100)
            if t.gamma is not None and t.underlying_price is not None:
                # Dealer is short gamma when client buys (sign → -sign for dealer)
                gamma_contributions.append(
                    -t.gamma * sign * t.volume_delta * 100 * t.underlying_price
                )

        net_delta_exposure = sum(delta_contributions) if delta_contributions else None
        net_gamma_exposure = sum(gamma_contributions) if gamma_contributions else None

        # --- Directional bias ---
        # Bullish: call BUY + put SELL. Bearish: put BUY + call SELL.
        # NEUTRAL trades contribute 0 to both (may cause directional_bias=None
        # even when net_premium is non-zero — see class docstring).
        bullish_premium = sum(
            (t.premium if t.premium is not None else 0.0) for t in window
            if t.trade_type != TradeType.MULTI_LEG
            and (
                (t.right == "C" and t.aggressor == Aggressor.BUY)
                or (t.right == "P" and t.aggressor == Aggressor.SELL)
            )
        )
        bearish_premium = sum(
            (t.premium if t.premium is not None else 0.0) for t in window
            if t.trade_type != TradeType.MULTI_LEG
            and (
                (t.right == "P" and t.aggressor == Aggressor.BUY)
                or (t.right == "C" and t.aggressor == Aggressor.SELL)
            )
        )
        total_directional = bullish_premium + bearish_premium
        directional_bias = (
            (bullish_premium - bearish_premium) / total_directional
            if total_directional > 0
            else None
        )

        return SentimentSnapshot(
            symbol=symbol,
            window_seconds=self._settings.sentiment_window_seconds,
            computed_at=now,
            trade_count=len(window),
            call_volume=call_volume,
            put_volume=put_volume,
            call_premium=call_premium,
            put_premium=put_premium,
            call_count=call_count,
            put_count=put_count,
            put_call_volume_ratio=put_call_volume_ratio,
            put_call_premium_ratio=put_call_premium_ratio,
            net_premium=net_premium,
            avg_call_iv=avg_call_iv,
            avg_put_iv=avg_put_iv,
            iv_skew=iv_skew,
            net_delta_exposure=net_delta_exposure,
            net_gamma_exposure=net_gamma_exposure,
            bullish_premium=bullish_premium,
            bearish_premium=bearish_premium,
            directional_bias=directional_bias,
        )

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Evict symbols whose most recent trade is older than max_age_seconds.

        Called hourly by the orchestration layer to prevent unbounded memory
        growth for symbols no longer receiving options flow.

        Note: returns the count of symbols (string keys) evicted, unlike
        FlowClassifier.purge_stale() and UnusualDetector.purge_stale() which
        return counts of con_ids (int keys). Do not sum these values together
        expecting a single unified unit.

        Args:
            max_age_seconds: Symbols with no trades newer than this are evicted.

        Returns:
            Number of symbols removed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = [
            sym for sym, window in self._windows.items()
            if not window or window[-1].timestamp < cutoff
        ]
        for sym in stale:
            del self._windows[sym]
        if stale:
            logger.info("sentiment: purged {} stale symbols", len(stale))
        return len(stale)


if __name__ == "__main__":
    from datetime import date as _date
    from datetime import datetime, timedelta, timezone

    from config.settings import Settings
    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate

    settings = Settings(
        min_premium=100.0,
        unusual_premium_threshold=50_000.0,
        unusual_oi_ratio_threshold=0.50,
        unusual_signal_threshold=5.0,
        otm_delta_threshold=0.30,
        otm_premium_threshold=30_000.0,
        risk_free_rate=0.05,
        sentiment_window_seconds=3600.0,
    )
    classifier = FlowClassifier(settings)
    engine = GreeksEngine(settings)
    agg = SentimentAggregator(settings)

    future_expiry = (_date.today() + timedelta(days=90)).strftime("%Y%m%d")
    base_time = datetime(2026, 3, 11, 14, 30, 0, tzinfo=timezone.utc)

    # 6 trades: mixed calls/puts, aggressors, IV levels
    trade_specs = [
        ("SPY", "C", 500.0, 0.25, 0.5,   100, 10_000.0),   # Call BUY  (bullish)
        ("SPY", "P", 480.0, 0.35, -0.2,  200,  8_000.0),   # Put BUY   (bearish, OTM)
        ("SPY", "C", 510.0, 0.22, 0.4,   150,  6_000.0),   # Call SELL (bearish)
        ("SPY", "P", 490.0, 0.32, -0.3,  100,  5_000.0),   # Put SELL  (bullish)
        ("SPY", "C", 505.0, 0.28, 0.6,   300, 15_000.0),   # Call BUY  (bullish)
        ("SPY", "P", 475.0, 0.40, -0.15, 250, 12_000.0),   # Put BUY   (bearish, deep OTM)
    ]

    for i, (sym, right, strike, iv, delta, vol, prem) in enumerate(trade_specs):
        price = prem / (vol * 100)
        tick = TickUpdate(
            symbol=sym, con_id=90000 + i, expiry=future_expiry,
            strike=strike, right=right,
            timestamp=base_time + timedelta(seconds=i * 10),
            bid=price - 0.10, ask=price + 0.10, last=price,
            volume=vol * (i + 1), open_interest=2000, last_size=vol,
            underlying_price=500.0, implied_vol=iv, delta=delta,
            gamma=0.005,
        )
        trade = classifier.classify(tick)
        if trade:
            enriched = engine.enrich(trade)
            agg.update(enriched)

    snap = agg.snapshot("SPY")
    if snap:
        logger.info("=== Sentiment Snapshot for SPY ===")
        logger.info("  trades in window : {}", snap.trade_count)
        logger.info("  calls={} puts={}", snap.call_count, snap.put_count)
        logger.info("  P/C volume ratio : {}", f"{snap.put_call_volume_ratio:.2f}" if snap.put_call_volume_ratio is not None else "N/A")
        logger.info("  P/C premium ratio: {}", f"{snap.put_call_premium_ratio:.2f}" if snap.put_call_premium_ratio is not None else "N/A")
        logger.info("  net_premium      : ${:,.0f}", snap.net_premium)
        logger.info("  iv_skew          : {}", f"{snap.iv_skew:.4f}" if snap.iv_skew is not None else "N/A")
        logger.info("  directional_bias : {}", f"{snap.directional_bias:.3f}" if snap.directional_bias is not None else "N/A")
        logger.info("  net_delta_exp    : {}", f"{snap.net_delta_exposure:,.0f}" if snap.net_delta_exposure is not None else "N/A")
        logger.info("  net_gamma_exp    : {}", f"{snap.net_gamma_exposure:,.0f}" if snap.net_gamma_exposure is not None else "N/A")
    else:
        logger.warning("No snapshot — no qualifying trades produced by classifier.")

    evicted = agg.purge_stale(max_age_seconds=3600.0)
    logger.info("purge_stale evicted {} symbols", evicted)
    logger.success("Sentiment smoke test complete.")
