from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger
from pydantic import BaseModel, Field

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType


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


# ---------------------------------------------------------------------------
# UnusualDetector
# ---------------------------------------------------------------------------

_PRIORITY = [
    UnusualReason.PREMIUM_SIZE,
    UnusualReason.OI_RATIO,
    UnusualReason.SIGNAL_STRENGTH,
    UnusualReason.OTM_PREMIUM,
]
assert set(_PRIORITY) == set(UnusualReason), (
    "_PRIORITY must contain an entry for every UnusualReason"
)


class UnusualDetector:
    """Threshold-based filter for unusual options activity.

    Maintains a lightweight OI cache (dict[int, int]) to persist the last-known
    open interest per contract. IBKR sends OI as a separate, infrequent tick type;
    without caching, the OI_RATIO check would be silently skipped on most ticks.

    detect() is async to accommodate future DB-backed statistical baselines.
    The current implementation performs no IO — safe to await on the hot path.

    The orchestration layer MUST call purge_stale() periodically (e.g. hourly)
    to evict state for contracts no longer being tracked.

    Note: The OI cache can be seeded at startup from the most recent ChainSnapshot
    via get_latest_snapshot(). This is the orchestration layer's responsibility.

    Example:
        settings = Settings()
        detector = UnusualDetector(settings)

        async for trade in classified_stream:
            signal = await detector.detect(trade)
            if signal:
                await insert_unusual_signal(session, signal)

    Args:
        settings: Application settings with detection thresholds.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._oi_cache: dict[int, int] = {}
        self._last_seen: dict[int, datetime] = {}

    async def detect(self, trade: ClassifiedTrade) -> UnusualSignal | None:
        """Evaluate a ClassifiedTrade against unusual activity thresholds.

        Updates the OI cache when trade.tick.open_interest is available.
        Evaluates four independent threshold conditions. Returns an UnusualSignal
        if any condition fires, otherwise None.

        Args:
            trade: ClassifiedTrade from FlowClassifier.classify().

        Returns:
            UnusualSignal if one or more conditions fired, else None.
        """
        s = self._settings
        con_id = trade.con_id

        # Update OI cache and last-seen timestamp.
        # Use trade.timestamp (tick receipt time) rather than wall-clock now so
        # that purge_stale() correctly evicts entries for old or replayed ticks.
        self._last_seen[con_id] = trade.timestamp
        if trade.tick.open_interest is not None:
            if con_id not in self._oi_cache:
                logger.debug(
                    "unusual_detector: OI cache populated for con_id={} oi={}",
                    con_id, trade.tick.open_interest,
                )
            self._oi_cache[con_id] = trade.tick.open_interest

        oi = self._oi_cache.get(con_id)
        reasons: list[UnusualReason] = []

        # 1. PREMIUM_SIZE — absolute dollar commitment
        if trade.premium is not None and trade.premium >= s.unusual_premium_threshold:
            reasons.append(UnusualReason.PREMIUM_SIZE)

        # 2. OI_RATIO — fraction of all open positions in one print
        if oi is not None and oi > 0 and trade.volume_delta > 0:
            if trade.volume_delta / oi >= s.unusual_oi_ratio_threshold:
                reasons.append(UnusualReason.OI_RATIO)

        # 3. SIGNAL_STRENGTH — composite score from flow classifier
        if trade.signal_strength is not None and trade.signal_strength >= s.unusual_signal_threshold:
            reasons.append(UnusualReason.SIGNAL_STRENGTH)

        # 4. OTM_PREMIUM — large bet on a far OTM contract
        # delta=None when IBKR has not yet populated Greeks — skip silently
        if (
            trade.delta is not None
            and abs(trade.delta) <= s.otm_delta_threshold
            and trade.premium is not None
            and trade.premium >= s.otm_premium_threshold
        ):
            reasons.append(UnusualReason.OTM_PREMIUM)

        if not reasons:
            return None

        top_reason = next(r for r in _PRIORITY if r in reasons)

        logger.info(
            "unusual_detector: {} {} | top={} reasons={} premium=${:.0f}",
            trade.symbol,
            trade.trade_type.value,
            top_reason.value,
            [r.value for r in reasons],
            trade.premium or 0,
        )

        return UnusualSignal(
            symbol=trade.symbol,
            con_id=trade.con_id,
            expiry=trade.expiry,
            right=trade.right,
            strike=trade.strike,
            trade_type=trade.trade_type,
            aggressor=trade.aggressor,
            premium=trade.premium,
            volume_delta=trade.volume_delta,
            signal_strength=trade.signal_strength,
            delta=trade.delta,
            underlying_price=trade.underlying_price,
            implied_vol=trade.implied_vol,
            effective_price=trade.effective_price,
            reasons=reasons,
            top_reason=top_reason,
            flagged_at=datetime.now(timezone.utc),
            trade=trade,
        )

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Evict OI cache entries for contracts not seen in max_age_seconds.

        Matches FlowClassifier.purge_stale() signature for a consistent
        orchestration layer call pattern across all analysis modules.

        Args:
            max_age_seconds: Contracts with no detect() calls newer than
                this threshold are evicted from both caches.

        Returns:
            Number of con_ids evicted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = [
            con_id for con_id, last_seen in self._last_seen.items()
            if last_seen < cutoff
        ]
        for con_id in stale:
            self._oi_cache.pop(con_id, None)
            del self._last_seen[con_id]
        if stale:
            logger.info("unusual_detector: purged {} stale OI cache entries", len(stale))
        return len(stale)


if __name__ == "__main__":
    import asyncio
    from config.settings import Settings
    from src.analysis.flow_classifier import FlowClassifier
    from src.data.tick_stream import TickUpdate

    async def main() -> None:
        settings = Settings(
            min_premium=100.0,
            unusual_premium_threshold=500.0,
            unusual_oi_ratio_threshold=0.50,
            unusual_signal_threshold=5.0,
            otm_delta_threshold=0.30,
            otm_premium_threshold=200.0,
        )
        classifier = FlowClassifier(settings)
        detector = UnusualDetector(settings)

        base_time = datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc)

        # Simulate a large OTM sweep: 3 rapid BUY prints, delta=0.20 (OTM), large premium
        results = []
        for i in range(3):
            tick = TickUpdate(
                symbol="SPY", con_id=99999, expiry="20260320", strike=550.0, right="C",
                timestamp=base_time + timedelta(milliseconds=i * 400),
                bid=1.00, ask=1.50, last=1.45,
                volume=100 * (i + 1), open_interest=200, last_size=100,
                underlying_price=500.0, implied_vol=0.40, delta=0.20,
            )
            trade = classifier.classify(tick)
            if trade:
                signal = await detector.detect(trade)
                results.append((trade, signal))
                logger.info(
                    "[tick {}] type={} | signal={} top_reason={}",
                    i + 1,
                    trade.trade_type.value,
                    "FLAGGED" if signal else "none",
                    signal.top_reason.value if signal else "-",
                )

        logger.success(
            "Smoke test complete. {} trades classified, {} flagged as unusual.",
            len(results),
            sum(1 for _, s in results if s is not None),
        )

    asyncio.run(main())
