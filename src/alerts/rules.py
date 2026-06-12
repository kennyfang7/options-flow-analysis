from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from loguru import logger
from pydantic import BaseModel

from config.settings import Settings
from src.analysis.flow_classifier import ClassifiedTrade, MultiLegStrategy, TradeType
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
# MultiLegBuffer
# ---------------------------------------------------------------------------


class MultiLegBuffer:
    """Groups MULTI_LEG legs by strategy_group; flushes completed strategies.

    Allows the orchestration layer to emit one Alert per strategy instance
    rather than one per leg.

    Example:
        buffer = MultiLegBuffer(settings.multi_leg_window_seconds)
        # on each qualifying tick:
        if trade and trade.trade_type == TradeType.MULTI_LEG:
            buffer.add(trade)
        # periodically:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.multi_leg_window_seconds)
        for group in buffer.flush_completed(cutoff):
            alert = rules.evaluate_multi_leg_strategy(group)

    Args:
        window_seconds: multi_leg_window_seconds from settings.
    """

    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._groups: dict[str, list[ClassifiedTrade]] = {}

    def add(self, trade: ClassifiedTrade) -> None:
        """Buffer a MULTI_LEG leg. Non-MULTI_LEG / group-less trades are silently ignored.

        Args:
            trade: ClassifiedTrade to buffer.
        """
        if trade.trade_type != TradeType.MULTI_LEG or trade.strategy_group is None:
            return
        if trade.strategy_group not in self._groups:
            self._groups[trade.strategy_group] = []
        self._groups[trade.strategy_group].append(trade)

    def flush_completed(self, cutoff: datetime) -> list[list[ClassifiedTrade]]:
        """Return groups whose most-recent leg arrived before cutoff, removing them.

        Args:
            cutoff: Groups with max(leg.timestamp) < cutoff are considered complete.

        Returns:
            List of strategy groups; each group is a list of ClassifiedTrade legs.
        """
        done_keys = [
            k for k, trades in self._groups.items()
            if max(t.timestamp for t in trades) < cutoff
        ]
        return [self._groups.pop(k) for k in done_keys]


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
        dte: int | None = getattr(signal.trade, "days_to_earnings", None)
        if dte is not None:
            if dte == 0:
                body_lines.append("⚡ Earnings TODAY")
            elif dte <= self._settings.pre_earnings_days:
                body_lines.append(f"⚡ Earnings in {dte}d")
            else:
                body_lines.append(f"📅 Earnings in {dte}d")

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
        if signal.days_to_earnings is not None:
            if signal.days_to_earnings == 0:
                body_lines.append("⚡ Earnings TODAY")
            elif signal.days_to_earnings <= self._settings.pre_earnings_days:
                body_lines.append(f"⚡ Earnings in {signal.days_to_earnings}d")
            else:
                body_lines.append(f"📅 Earnings in {signal.days_to_earnings}d")

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

    def evaluate_multi_leg_strategy(self, trades: list[ClassifiedTrade]) -> Alert:
        """Generate one consolidated Alert for a completed multi-leg strategy.

        Level is determined by strategy_net_premium:
            >= unusual_premium_threshold → HIGH
            >= 5 × min_premium          → MEDIUM
            else                        → LOW

        Args:
            trades: All detected legs (from MultiLegBuffer.flush_completed()).

        Returns:
            Alert describing the full strategy.

        Raises:
            ValueError: If trades is empty.
        """
        if not trades:
            raise ValueError("evaluate_multi_leg_strategy requires at least one trade")

        lead          = max(trades, key=lambda t: t.timestamp)
        strategy_type = lead.multi_leg_strategy or MultiLegStrategy.COMBO
        net_prem      = lead.strategy_net_premium or sum(t.premium or 0.0 for t in trades)
        n_legs        = lead.window_ticks

        if net_prem >= self._settings.unusual_premium_threshold:
            level = AlertLevel.HIGH
        elif net_prem >= self._settings.min_premium * 5:
            level = AlertLevel.MEDIUM
        else:
            level = AlertLevel.LOW

        strategy_label = strategy_type.value.replace("_", " ").title()
        title = f"{lead.symbol} {strategy_label.upper()} {level.value.upper()}"

        sorted_legs = sorted(trades, key=lambda t: t.strike)
        strikes_str = "  ".join(f"${t.strike:.0f}{t.right}" for t in sorted_legs)
        body_lines = [
            f"Strategy: {strategy_label} ({n_legs} legs)",
            f"Net premium: {_fmt_premium(net_prem)}",
            f"Legs: {strikes_str}",
            f"Expiry: {lead.expiry}",
            f"Underlying: {_fmt_premium(lead.underlying_price)}",
        ]

        logger.debug(
            "evaluate_multi_leg_strategy: {} {} {} legs net={}",
            lead.symbol, strategy_type.value, n_legs, _fmt_premium(net_prem),
        )

        return Alert(
            symbol=lead.symbol,
            level=level,
            title=title,
            body="\n".join(body_lines),
            source="multi_leg",
            emitted_at=datetime.now(timezone.utc),
            metadata={
                "symbol":        lead.symbol,
                "strategy_type": strategy_type.value,
                "n_legs":        n_legs,
                "net_premium":   net_prem,
                "expiry":        lead.expiry,
            },
        )
