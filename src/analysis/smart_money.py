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
# Detector (stub — full logic in Task 2)
# ---------------------------------------------------------------------------


class SmartMoneyDetector:
    """Scores EnrichedTrade objects against heuristic smart money criteria.

    Synchronous — no IO on the hot path. Stateless beyond settings.

    Example:
        detector = SmartMoneyDetector(settings)
        signal = detector.score(enriched_trade)
        if signal:
            persist_or_alert(signal)

    Args:
        settings: Application settings with smart money thresholds.
    """

    def __init__(self, settings: Settings) -> None:
        """Store settings for threshold comparisons in score().

        Args:
            settings: Application settings with smart money thresholds.
        """
        self._settings = settings
