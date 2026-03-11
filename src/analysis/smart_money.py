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

    LARGE_BLOCK = "large_block"
    # TradeType.BLOCK + premium >= unusual_premium_threshold.
    # Catches: single very large block — concentrated institutional capital.


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_CONFIDENCE_WEIGHTS: dict[SmartMoneyReason, float] = {
    SmartMoneyReason.SWEEP_AGGRESSOR: 0.40,
    SmartMoneyReason.BIG_OTM_BET:     0.45,
    SmartMoneyReason.NEAR_EXPIRY_OTM: 0.35,
    SmartMoneyReason.UNUSUAL_VOLUME:  0.35,
    SmartMoneyReason.LARGE_BLOCK:     0.30,
}

_PRIORITY: list[SmartMoneyReason] = [
    SmartMoneyReason.SWEEP_AGGRESSOR,
    SmartMoneyReason.BIG_OTM_BET,
    SmartMoneyReason.NEAR_EXPIRY_OTM,
    SmartMoneyReason.UNUSUAL_VOLUME,
    SmartMoneyReason.LARGE_BLOCK,
]

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

        # 3. NEAR_EXPIRY_OTM — time-sensitive leveraged speculation
        if (
            trade.days_to_expiry <= s.near_expiry_days
            and trade.moneyness == Moneyness.OTM
            and trade.aggressor == Aggressor.BUY
        ):
            reasons.append(SmartMoneyReason.NEAR_EXPIRY_OTM)

        # 4. UNUSUAL_VOLUME — volume far exceeds institutional block baseline
        if trade.volume_delta >= s.unusual_volume_multiplier * s.min_block_size:
            reasons.append(SmartMoneyReason.UNUSUAL_VOLUME)

        # 5. LARGE_BLOCK — single very large concentrated position
        if (
            trade.trade_type == TradeType.BLOCK
            and trade.premium is not None
            and trade.premium >= s.unusual_premium_threshold
        ):
            reasons.append(SmartMoneyReason.LARGE_BLOCK)

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
