from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from loguru import logger
from pydantic import BaseModel, Field

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, TradeType
from src.analysis.greeks_engine import EnrichedTrade, Moneyness


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SmartMoneyReason(str, Enum):
    """Reason codes explaining why a trade was scored as smart money activity.

    Multiple reasons may fire for a single trade. Use top_reason for the
    highest-priority signal when only one label is needed.
    """

    SWEEP_AGGRESSOR = "sweep_aggressor"
    # TradeType.SWEEP + non-NEUTRAL aggressor.
    # Catches: institutional urgency — sweeping multiple exchanges to fill fast.

    BIG_OTM_BET = "big_otm_bet"
    # moneyness == OTM + aggressor == BUY + premium >= otm_premium_threshold.
    # Catches: expensive far-OTM directional bet — the classic smart money tell.

    NEAR_EXPIRY_OTM = "near_expiry_otm"
    # days_to_expiry <= near_expiry_days + moneyness == OTM + aggressor == BUY.
    # Catches: time-sensitive speculative leveraged bets (e.g. weekly options).

    UNUSUAL_VOLUME = "unusual_volume"
    # volume_delta >= unusual_volume_multiplier * min_block_size.
    # Catches: volume far exceeding a normal institutional block baseline.

    PRE_EARNINGS = "pre_earnings"
    # days_to_earnings is within settings.pre_earnings_days.
    # Catches: flow positioned just before an earnings catalyst.

    LARGE_BLOCK = "large_block"
    # TradeType.BLOCK + premium >= unusual_premium_threshold.
    # Catches: single very large block — concentrated institutional capital.

    MULTI_LEG_STRATEGY = "multi_leg_strategy"
    # trade_type == TradeType.MULTI_LEG — a leg of a detected spread/combo order.


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_CONFIDENCE_WEIGHTS: dict[SmartMoneyReason, float] = {
    SmartMoneyReason.SWEEP_AGGRESSOR:    0.40,
    SmartMoneyReason.BIG_OTM_BET:        0.45,
    SmartMoneyReason.PRE_EARNINGS:       0.30,
    SmartMoneyReason.NEAR_EXPIRY_OTM:    0.35,
    SmartMoneyReason.UNUSUAL_VOLUME:     0.35,
    SmartMoneyReason.LARGE_BLOCK:        0.30,
    SmartMoneyReason.MULTI_LEG_STRATEGY: 0.35,
}

_PRIORITY: list[SmartMoneyReason] = [
    SmartMoneyReason.SWEEP_AGGRESSOR,
    SmartMoneyReason.BIG_OTM_BET,
    SmartMoneyReason.MULTI_LEG_STRATEGY,
    SmartMoneyReason.PRE_EARNINGS,
    SmartMoneyReason.NEAR_EXPIRY_OTM,
    SmartMoneyReason.UNUSUAL_VOLUME,
    SmartMoneyReason.LARGE_BLOCK,
]
assert set(_PRIORITY) == set(SmartMoneyReason), (
    "_PRIORITY must contain an entry for every SmartMoneyReason"
)

assert set(_CONFIDENCE_WEIGHTS) == set(SmartMoneyReason), (
    "_CONFIDENCE_WEIGHTS must contain an entry for every SmartMoneyReason"
)


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class SmartMoneySignal(BaseModel):
    """Result of scoring an EnrichedTrade as potential smart money activity.

    Emitted by SmartMoneyDetector.score(). The caller (orchestration layer)
    decides whether to persist, alert, or aggregate further.

    Attributes:
        symbol: Underlying ticker symbol.
        con_id: IBKR contract ID.
        expiry: Expiration date in YYYYMMDD format.
        right: "C" for call, "P" for put.
        strike: Strike price.
        trade_type: Classified pattern from FlowClassifier.
        aggressor: Directional side from FlowClassifier.
        premium: Dollar value of the trade. None when price unavailable.
        volume_delta: New contracts traded since last tick.
        delta: Option delta from EnrichedTrade (IBKR or BS fallback).
        days_to_expiry: Calendar days until expiry at enrich() call time.
        moneyness: Price-based ITM/ATM/OTM classification from GreeksEngine.
        implied_vol: Implied volatility (IBKR or BS fallback).
        iv_source: Origin of implied_vol: "ibkr", "black_scholes", or "unavailable".
        underlying_price: Underlying price at tick receipt.
        reasons: All SmartMoneyReason conditions that fired (>=1 guaranteed).
            Insertion order matches check order; use top_reason for priority.
        top_reason: Highest-priority reason that fired (see _PRIORITY).
        confidence: Sum of per-reason weights, capped at 1.0. Higher = stronger signal.
        detected_at: When score() was called.
        trade: Full EnrichedTrade in-memory for downstream access.
            Excluded from serialization — not written to DB.
    """

    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    trade_type: TradeType
    aggressor: Aggressor
    premium: float | None
    volume_delta: int
    delta: float | None
    days_to_expiry: int
    moneyness: Moneyness
    implied_vol: float | None
    iv_source: str
    underlying_price: float | None

    days_to_earnings: int | None = None

    reasons: list[SmartMoneyReason]
    top_reason: SmartMoneyReason
    confidence: float
    detected_at: datetime

    trade: EnrichedTrade = Field(exclude=True)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class SmartMoneyDetector:
    """Heuristic scorer for institutional (smart money) options activity.

    Applies five independent threshold checks to each EnrichedTrade. Confidence
    is the sum of per-reason weights (capped at 1.0). A SmartMoneySignal is
    emitted only when confidence >= smart_money_min_confidence.

    SmartMoneyDetector is stateless — it holds no per-contract cache. The
    purge_stale() method is a no-op included only for interface consistency
    with FlowClassifier, UnusualDetector, and SentimentAggregator.

    Pipeline position:
        FlowClassifier → GreeksEngine → SmartMoneyDetector

    Example:
        detector = SmartMoneyDetector(settings)
        sig = detector.score(enriched_trade)
        if sig:
            logger.info("Smart money: {} conf={:.2f}", sig.symbol, sig.confidence)

    Args:
        settings: Application settings with detection thresholds.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the detector with application settings.

        Args:
            settings: Application settings with detection thresholds.
        """
        self._settings = settings

    def score(self, trade: EnrichedTrade) -> SmartMoneySignal | None:
        """Score an EnrichedTrade for smart money characteristics.

        Evaluates five independent threshold conditions. Returns a
        SmartMoneySignal when total confidence >= smart_money_min_confidence,
        otherwise None.

        Args:
            trade: EnrichedTrade from GreeksEngine.enrich().

        Returns:
            SmartMoneySignal if confidence threshold met, else None.
        """
        s = self._settings
        reasons: list[SmartMoneyReason] = []

        # 1. SWEEP_AGGRESSOR — institutional urgency signal
        if trade.trade_type == TradeType.SWEEP and trade.aggressor != Aggressor.NEUTRAL:
            reasons.append(SmartMoneyReason.SWEEP_AGGRESSOR)

        # 2. BIG_OTM_BET — expensive far-OTM directional bet
        if (
            trade.moneyness == Moneyness.OTM
            and trade.aggressor == Aggressor.BUY
            and trade.premium is not None
            and trade.premium >= s.otm_premium_threshold
        ):
            reasons.append(SmartMoneyReason.BIG_OTM_BET)

        # 3. PRE_EARNINGS — flow positioned just before an earnings catalyst
        if (
            trade.days_to_earnings is not None
            and 0 <= trade.days_to_earnings <= s.pre_earnings_days
        ):
            reasons.append(SmartMoneyReason.PRE_EARNINGS)

        # 4. NEAR_EXPIRY_OTM — time-sensitive leveraged speculation
        if (
            trade.days_to_expiry <= s.near_expiry_days
            and trade.moneyness == Moneyness.OTM
            and trade.aggressor == Aggressor.BUY
        ):
            reasons.append(SmartMoneyReason.NEAR_EXPIRY_OTM)

        # 5. UNUSUAL_VOLUME — volume far exceeds institutional block baseline
        if trade.volume_delta >= s.unusual_volume_multiplier * s.min_block_size:
            reasons.append(SmartMoneyReason.UNUSUAL_VOLUME)

        # 6. LARGE_BLOCK — single very large concentrated position
        if (
            trade.trade_type == TradeType.BLOCK
            and trade.premium is not None
            and trade.premium >= s.unusual_premium_threshold
        ):
            reasons.append(SmartMoneyReason.LARGE_BLOCK)

        # 7. MULTI_LEG_STRATEGY — leg of a detected spread/combo order
        if trade.trade_type == TradeType.MULTI_LEG:
            reasons.append(SmartMoneyReason.MULTI_LEG_STRATEGY)

        if not reasons:
            return None

        confidence = min(1.0, sum(_CONFIDENCE_WEIGHTS[r] for r in reasons))
        if confidence < s.smart_money_min_confidence:
            return None

        top_reason = next(r for r in _PRIORITY if r in reasons)

        logger.info(
            "smart_money: {} {} | top={} conf={:.2f} reasons={} premium=${}",
            trade.symbol,
            trade.trade_type.value,
            top_reason.value,
            confidence,
            [r.value for r in reasons],
            f"{trade.premium:,.0f}" if trade.premium is not None else "N/A",
        )

        return SmartMoneySignal(
            symbol=trade.symbol,
            con_id=trade.con_id,
            expiry=trade.expiry,
            right=trade.right,
            strike=trade.strike,
            trade_type=trade.trade_type,
            aggressor=trade.aggressor,
            premium=trade.premium,
            volume_delta=trade.volume_delta,
            delta=trade.delta,
            days_to_expiry=trade.days_to_expiry,
            moneyness=trade.moneyness,
            implied_vol=trade.implied_vol,
            iv_source=trade.iv_source,
            underlying_price=trade.underlying_price,
            days_to_earnings=trade.days_to_earnings,
            reasons=reasons,
            top_reason=top_reason,
            confidence=confidence,
            detected_at=datetime.now(timezone.utc),
            trade=trade,
        )

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """No-op — SmartMoneyDetector is stateless.

        Included for interface consistency with FlowClassifier,
        UnusualDetector, and SentimentAggregator, which all expose
        purge_stale() for the orchestration layer to call hourly.

        Args:
            max_age_seconds: Accepted for interface consistency; ignored.

        Returns:
            Always 0.
        """
        return 0


if __name__ == "__main__":
    from datetime import date as _date, timedelta

    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate

    settings = Settings(
        min_premium=100.0,
        min_block_size=500,
        unusual_volume_multiplier=3.0,
        unusual_premium_threshold=250_000.0,
        otm_premium_threshold=100_000.0,
        near_expiry_days=7,
        smart_money_min_confidence=0.30,
        risk_free_rate=0.05,
        sweep_window_seconds=10.0,  # total span from tick 0 to tick 2 is 4s; default 2.0s would drop tick 0
    )
    classifier = FlowClassifier(settings)
    engine = GreeksEngine(settings)
    detector = SmartMoneyDetector(settings)

    future_expiry = (_date.today() + timedelta(days=90)).strftime("%Y%m%d")
    near_expiry = (_date.today() + timedelta(days=4)).strftime("%Y%m%d")
    base_time = datetime(2026, 3, 11, 14, 30, 0, tzinfo=timezone.utc)

    # (label, expiry, strike, right, bid, ask, last, volume, oi, last_size, underlying, iv, delta)
    scenarios = [
        # [1-3] Sweep of 3 rapid OTM call buys — third tick should be SWEEP_AGGRESSOR
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 100, 1000, 100, 500.0, 0.30, 0.20),
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 200, 1000, 100, 500.0, 0.30, 0.20),
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 300, 1000, 100, 500.0, 0.30, 0.20),
        # [4] Near-expiry OTM buy — NEAR_EXPIRY_OTM expected
        ("near_expiry_otm", near_expiry,   580.0, "C", 0.50, 0.80, 0.78, 500, 800,  500, 500.0, 0.55, 0.10),
        # [5] Large block — LARGE_BLOCK expected (2500 * 1.55 * 100 = $375k)
        ("large_block",     future_expiry, 495.0, "C", 1.40, 1.60, 1.55, 2500, 5000, 2500, 500.0, 0.25, 0.52),
        # [6] Small retail trade — expect None (below all thresholds)
        ("retail_small",    future_expiry, 510.0, "C", 0.50, 0.70, 0.65, 50,  2000,  50,  500.0, 0.22, 0.35),
    ]

    results: list[tuple[str, SmartMoneySignal | None]] = []
    con_ids = [90000, 90000, 90000, 90001, 90002, 90003]  # sweep ticks share con_id
    for i, (label, expiry, strike, right, bid, ask, last, vol, oi, last_size, underlying, iv, delta) in enumerate(scenarios):
        tick = TickUpdate(
            symbol="SPY", con_id=con_ids[i], expiry=expiry,
            strike=strike, right=right,
            timestamp=base_time + timedelta(seconds=i * 2),
            bid=bid, ask=ask, last=last,
            volume=vol, open_interest=oi, last_size=last_size,
            underlying_price=underlying, implied_vol=iv, delta=delta,
        )
        trade = classifier.classify(tick)
        if trade:
            enriched = engine.enrich(trade)
            sig = detector.score(enriched)
            results.append((label, sig))
            logger.info(
                "[{}] type={} moneyness={} dte={} | smart_money={} top={} conf={}",
                label,
                enriched.trade_type.value,
                enriched.moneyness.value,
                enriched.days_to_expiry,
                "FLAGGED" if sig else "none",
                sig.top_reason.value if sig else "-",
                f"{sig.confidence:.2f}" if sig else "-",
            )
        else:
            results.append((label, None))
            logger.info("[{}] → trade below min_premium threshold", label)

    evicted = detector.purge_stale()
    logger.info("purge_stale evicted {} (always 0 — stateless)", evicted)
    flagged = sum(1 for _, s in results if s is not None)
    logger.success(
        "Smoke test complete. {} scenarios processed → {} flagged as smart money.",
        len(results), flagged,
    )
