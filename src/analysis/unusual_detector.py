from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, Field

from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType

if TYPE_CHECKING:
    from config.settings import Settings


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UnusualReason(str, Enum):
    """Reason codes explaining why a trade was flagged as unusual.

    Multiple reasons may fire for a single trade. Use top_reason for
    the highest-priority signal when only one label is needed.
    """

    PREMIUM_SIZE = "premium_size"
    # trade.premium >= unusual_premium_threshold (default $250k)
    # Catches: absolute dollar size indicating institutional capital.

    OI_RATIO = "oi_ratio"
    # volume_delta / open_interest >= unusual_oi_ratio_threshold (default 0.50)
    # Catches: one print consuming >= 50% of all existing open positions.

    SIGNAL_STRENGTH = "signal_strength"
    # trade.signal_strength >= unusual_signal_threshold (default 5.0)
    # Catches: trades scoring high on combined premium + OI-relative volume.

    OTM_PREMIUM = "otm_premium"
    # |delta| <= otm_delta_threshold AND premium >= otm_premium_threshold
    # Catches: expensive bets on far OTM contracts — the smart money tell.


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class UnusualSignal(BaseModel):
    """Result of flagging a ClassifiedTrade as unusually significant.

    Emitted by UnusualDetector.detect(). The caller (orchestration layer)
    decides whether to persist, alert, or pass to downstream analysis.

    Attributes:
        symbol: Underlying ticker symbol.
        con_id: IBKR contract ID.
        expiry: Expiration date in YYYYMMDD format.
        right: "C" for call, "P" for put.
        strike: Strike price.
        trade_type: Classified pattern from FlowClassifier.
        aggressor: Directional side from FlowClassifier.
        premium: Dollar value of the triggering trade.
        volume_delta: New contracts traded since last tick.
        signal_strength: Composite score from FlowClassifier.
        delta: Option delta from triggering tick.
        underlying_price: Underlying price at tick receipt.
        implied_vol: Implied volatility from triggering tick.
        effective_price: Price used for premium computation.
        reasons: All UnusualReason conditions that fired (>=1 guaranteed).
            Insertion order matches check order; use top_reason for priority.
        top_reason: Highest-priority reason that fired.
            Priority: PREMIUM_SIZE > OI_RATIO > SIGNAL_STRENGTH > OTM_PREMIUM.
        flagged_at: When detect() was called. Distinct from trade.timestamp
            (when the tick was received) to preserve semantic clarity.
        trade: Full ClassifiedTrade in-memory for downstream access.
            Excluded from serialization — not written to DB.
    """

    # Identity (flattened for serialization — same pattern as ClassifiedTrade.tick)
    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    trade_type: TradeType
    aggressor: Aggressor
    premium: float | None
    volume_delta: int
    signal_strength: float | None
    delta: float | None
    underlying_price: float | None
    implied_vol: float | None
    effective_price: float | None

    # Detection result
    reasons: list[UnusualReason]
    top_reason: UnusualReason
    flagged_at: datetime

    # Full trade in-memory; excluded from serialization
    trade: ClassifiedTrade = Field(exclude=True)
