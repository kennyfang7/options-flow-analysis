from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from loguru import logger
from pydantic import BaseModel

from config.settings import Settings
from src.analysis.smart_money import SmartMoneySignal
from src.analysis.unusual_detector import UnusualReason, UnusualSignal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AlertLevel(str, Enum):
    """Severity tier for a triggered alert.

    Determines Discord embed color and can be used by the orchestration
    layer to suppress lower-priority alerts during high-noise periods.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Level mapping for UnusualReason
# ---------------------------------------------------------------------------

_UNUSUAL_LEVEL: dict[UnusualReason, AlertLevel] = {
    UnusualReason.PREMIUM_SIZE: AlertLevel.HIGH,
    UnusualReason.OI_RATIO: AlertLevel.MEDIUM,
    UnusualReason.OTM_PREMIUM: AlertLevel.MEDIUM,
    UnusualReason.SIGNAL_STRENGTH: AlertLevel.LOW,
}

assert set(_UNUSUAL_LEVEL) == set(UnusualReason), (
    "_UNUSUAL_LEVEL must contain an entry for every UnusualReason"
)


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class Alert(BaseModel):
    """A single notification unit produced by AlertRules.

    Produced by AlertRules.evaluate_unusual() and evaluate_smart_money().
    Consumed by Notifier.send(). Can also be persisted by the orchestration
    layer for deduplication or rate-limiting.

    Attributes:
        symbol: Underlying ticker symbol.
        level: Severity tier (LOW / MEDIUM / HIGH).
        title: One-line heading for the notification.
        body: Multi-line detail text.
        source: Origin detector — "unusual" or "smart_money".
        emitted_at: UTC wall-clock time when the Alert was created.
        metadata: JSON-serializable dict of key signal fields for downstream
            use (persistence, deduplication). Always JSON-serializable.
    """

    symbol: str
    level: AlertLevel
    title: str
    body: str
    source: str
    emitted_at: datetime
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Private formatting helpers
# ---------------------------------------------------------------------------


def _fmt_premium(premium: float | None) -> str:
    """Format a premium value as a dollar string, or 'N/A' when unavailable."""
    return f"${premium:,.0f}" if premium is not None else "N/A"


def _fmt_pct(val: float | None) -> str:
    """Format a float as a percentage string, or 'N/A' when unavailable."""
    return f"{val:.1%}" if val is not None else "N/A"


def _fmt_float(val: float | None, decimals: int = 2) -> str:
    """Format a float to fixed decimals, or 'N/A' when unavailable."""
    return f"{val:.{decimals}f}" if val is not None else "N/A"


# ---------------------------------------------------------------------------
# AlertRules
# ---------------------------------------------------------------------------


class AlertRules:
    """Maps detector signals to Alert objects with severity and formatted messages.

    Converts UnusualSignal and SmartMoneySignal objects into Alert instances
    suitable for delivery by Notifier. Stateless — holds no per-symbol state.

    Level assignment:
        UnusualSignal: driven by top_reason
            PREMIUM_SIZE → HIGH
            OI_RATIO / OTM_PREMIUM → MEDIUM
            SIGNAL_STRENGTH → LOW

        SmartMoneySignal: driven by confidence score
            >= 0.70 → HIGH
            >= 0.50 → MEDIUM
            < 0.50  → LOW

    Args:
        settings: Application settings (passed for future threshold-based rules).
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize AlertRules with application settings.

        Args:
            settings: Application settings with detection thresholds.
        """
        self._settings = settings

    def evaluate_unusual(self, signal: UnusualSignal) -> Alert:
        """Convert an UnusualSignal to an Alert.

        Always returns an Alert (never None) — UnusualDetector already
        gates on configured thresholds before emitting a signal.

        Args:
            signal: UnusualSignal from UnusualDetector.detect().

        Returns:
            Alert with level, title, body, and metadata populated.
        """
        level = _UNUSUAL_LEVEL.get(signal.top_reason, AlertLevel.LOW)
        title = f"{signal.symbol} UNUSUAL {level.value.upper()}"

        body_lines = [
            (
                f"{signal.trade_type.value.upper()} {signal.aggressor.value.upper()} "
                f"| {signal.volume_delta:,} contracts | {_fmt_premium(signal.premium)}"
            ),
            f"Reasons: {', '.join(r.value for r in signal.reasons)}",
            (
                f"Strike: ${signal.strike:.0f} {signal.right} "
                f"| Expiry: {signal.expiry} "
                f"| Delta: {_fmt_float(signal.delta)}"
            ),
            (
                f"IV: {_fmt_pct(signal.implied_vol)} "
                f"| Underlying: {_fmt_premium(signal.underlying_price)}"
            ),
        ]

        premium_str = f"{signal.premium:,.0f}" if signal.premium is not None else "N/A"
        logger.debug(
            "evaluate_unusual: {} {} level={} premium={}",
            signal.symbol,
            signal.top_reason.value,
            level.value,
            premium_str,
        )

        return Alert(
            symbol=signal.symbol,
            level=level,
            title=title,
            body="\n".join(body_lines),
            source="unusual",
            emitted_at=datetime.now(timezone.utc),
            metadata={
                "symbol": signal.symbol,
                "trade_type": signal.trade_type.value,
                "aggressor": signal.aggressor.value,
                "premium": signal.premium,
                "volume_delta": signal.volume_delta,
                "top_reason": signal.top_reason.value,
            },
        )

    def evaluate_smart_money(self, signal: SmartMoneySignal) -> Alert:
        """Convert a SmartMoneySignal to an Alert.

        Always returns an Alert (never None) — SmartMoneyDetector already
        gates on smart_money_min_confidence before emitting a signal.

        Level is derived from confidence score:
            >= 0.70 → HIGH
            >= 0.50 → MEDIUM
            else    → LOW

        Args:
            signal: SmartMoneySignal from SmartMoneyDetector.score().

        Returns:
            Alert with level, title, body, and metadata populated.
        """
        if signal.confidence >= 0.70:
            level = AlertLevel.HIGH
        elif signal.confidence >= 0.50:
            level = AlertLevel.MEDIUM
        else:
            level = AlertLevel.LOW

        title = (
            f"{signal.symbol} SMART MONEY {level.value.upper()} "
            f"({signal.confidence:.0%})"
        )

        body_lines = [
            f"Top signal: {signal.top_reason.value}",
            (
                f"{signal.trade_type.value.upper()} {signal.aggressor.value.upper()} "
                f"| {signal.volume_delta:,} contracts | {_fmt_premium(signal.premium)}"
            ),
            (
                f"{signal.moneyness.value.upper()} {signal.right} "
                f"| Strike: ${signal.strike:.0f} "
                f"| DTE: {signal.days_to_expiry}d"
            ),
            (
                f"IV: {_fmt_pct(signal.implied_vol)} "
                f"| Delta: {_fmt_float(signal.delta)}"
            ),
            f"All signals: {', '.join(r.value for r in signal.reasons)}",
        ]

        logger.debug(
            "evaluate_smart_money: {} {} level={} conf={:.2f}",
            signal.symbol,
            signal.top_reason.value,
            level.value,
            signal.confidence,
        )

        return Alert(
            symbol=signal.symbol,
            level=level,
            title=title,
            body="\n".join(body_lines),
            source="smart_money",
            emitted_at=datetime.now(timezone.utc),
            metadata={
                "symbol": signal.symbol,
                "trade_type": signal.trade_type.value,
                "aggressor": signal.aggressor.value,
                "premium": signal.premium,
                "volume_delta": signal.volume_delta,
                "top_reason": signal.top_reason.value,
                "confidence": signal.confidence,
            },
        )
