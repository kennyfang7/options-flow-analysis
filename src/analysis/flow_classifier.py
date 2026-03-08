from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import log1p
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, Field

from src.data.tick_stream import TickUpdate

if TYPE_CHECKING:
    from config.settings import Settings


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TradeType(str, Enum):
    """Classification label for a detected trade pattern."""

    SWEEP = "sweep"
    SPLIT = "split"
    BLOCK = "block"
    MULTI_LEG = "multi_leg"  # placeholder — detection not implemented
    UNKNOWN = "unknown"


class Aggressor(str, Enum):
    """Directional side of a trade based on spread position."""

    BUY = "buy"
    SELL = "sell"
    NEUTRAL = "neutral"


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


class ClassifiedTrade(BaseModel):
    """Result of classifying a single TickUpdate as a trade event.

    Emitted by FlowClassifier.classify(). The caller decides whether to
    persist, alert, or pass downstream.

    Attributes:
        symbol: Underlying ticker symbol.
        con_id: IBKR contract ID.
        expiry: Expiration date in YYYYMMDD format.
        right: "C" for call, "P" for put.
        strike: Strike price.
        underlying_price: Underlying price at tick receipt.
        implied_vol: Implied volatility from triggering tick.
        delta: Delta greek from triggering tick.
        trade_type: Classified pattern (SWEEP, SPLIT, BLOCK, UNKNOWN).
        aggressor: Directional side (BUY, SELL, NEUTRAL).
        spread_position: Unclamped (last - bid) / (ask - bid). >1.0 means
            paid above ask; <0.0 means hit below bid. Treat as probabilistic.
            None when bid/ask/last unavailable or ask == bid.
        effective_price: Price used for premium. tick.last if bid<=last<=ask,
            otherwise tick.mid fallback.
        last_size: Size of the triggering print in contracts.
        premium: last_size x effective_price x 100 (dollar value).
        signal_strength: log1p(premium / min_premium) x min(volume_delta /
            max(open_interest, 1), 10.0). None when open_interest unavailable.
        volume_delta: Volume increase since last tick. Approximated as
            last_size on first sight or session reset.
        window_ticks: Ticks in detection window used for classification.
            len(sweep_window) for SWEEP, len(split_window) for SPLIT, 1 otherwise.
        timestamp: When the trade occurred (= tick.timestamp).
        tick: Full raw TickUpdate. Excluded from serialization.
    """

    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    underlying_price: float | None
    implied_vol: float | None
    delta: float | None
    trade_type: TradeType
    aggressor: Aggressor
    spread_position: float | None
    effective_price: float | None
    last_size: int | None
    premium: float | None
    signal_strength: float | None
    volume_delta: int
    window_ticks: int
    timestamp: datetime
    tick: TickUpdate = Field(exclude=True)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _all_same_aggressor(entries: list[tuple[TickUpdate, Aggressor]]) -> bool:
    """Return True if all non-NEUTRAL entries share the same aggressor direction.

    Args:
        entries: List of (tick, aggressor) tuples from the classifier window.

    Returns:
        True if all non-neutral aggressors are identical (all BUY or all SELL).
        False if empty, all-neutral, or mixed.
    """
    aggressors = {agg for _, agg in entries if agg != Aggressor.NEUTRAL}
    return len(aggressors) == 1


def _sizes_within_tolerance(
    entries: list[tuple[TickUpdate, Aggressor]], tol: float
) -> bool:
    """Return True if all last_size values are within ±tol of the median.

    Uses median (not mean) for robustness against outliers.

    Args:
        entries: List of (tick, aggressor) tuples.
        tol: Maximum allowed fractional deviation from median (e.g. 0.20 = ±20%).

    Returns:
        True if all non-None sizes are within tolerance of the median.
        False if no sizes available or median is zero.
    """
    sizes = [t.last_size for t, _ in entries if t.last_size is not None]
    if not sizes:
        return False
    sorted_sizes = sorted(sizes)
    n = len(sorted_sizes)
    if n % 2 == 1:
        median: float = sorted_sizes[n // 2]
    else:
        median = (sorted_sizes[(n - 1) // 2] + sorted_sizes[n // 2]) / 2.0
    if median == 0:
        return False
    return all(abs(s - median) / median <= tol for s in sizes)


# ---------------------------------------------------------------------------
# FlowClassifier
# ---------------------------------------------------------------------------


class FlowClassifier:
    """Stateful real-time trade classifier.

    Maintains a per-contract in-memory window of recent (TickUpdate, Aggressor)
    tuples. classify() is synchronous and performs no IO.

    The orchestration layer MUST call purge_stale() periodically (e.g. hourly).

    Example:
        classifier = FlowClassifier(settings)
        result = classifier.classify(tick)
        if result:
            await insert_classified_trade(session, result)

    Args:
        settings: Application settings with classification thresholds.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._windows: dict[int, deque[tuple[TickUpdate, Aggressor]]] = {}
        self._last_volume: dict[int, int] = {}

    def classify(self, tick: TickUpdate) -> ClassifiedTrade | None:
        """Classify a TickUpdate as a trade event.

        Returns None when: last_size/last/volume is None, volume_delta==0,
        effective_price unavailable, or premium < min_premium.

        Args:
            tick: TickUpdate from TickStream.queue.

        Returns:
            ClassifiedTrade if tick represents a qualifying trade, else None.
        """
        s = self._settings
        con_id = tick.con_id

        # 1. Early exits — required fields must be present
        if tick.last_size is None or tick.last is None or tick.volume is None:
            return None

        # 2. Volume deduplication + session reset detection
        if con_id not in self._last_volume or tick.volume < self._last_volume[con_id]:
            logger.warning(
                "classify: volume reset for con_id={} ({}->{})",
                con_id,
                self._last_volume.get(con_id, "unseen"),
                tick.volume,
            )
            self._last_volume[con_id] = tick.volume
            volume_delta = tick.last_size
        else:
            volume_delta = tick.volume - self._last_volume[con_id]
            self._last_volume[con_id] = tick.volume

        if volume_delta == 0:
            return None

        # 3. Aggressor + spread_position
        bid, ask, last = tick.bid, tick.ask, tick.last
        if bid is not None and ask is not None and last is not None:
            if ask == bid:
                spread_position: float | None = None
                aggressor = Aggressor.NEUTRAL
            else:
                spread_position = (last - bid) / (ask - bid)  # intentionally unclamped
                if spread_position >= s.aggressor_buy_threshold:
                    aggressor = Aggressor.BUY
                elif spread_position <= s.aggressor_sell_threshold:
                    aggressor = Aggressor.SELL
                else:
                    aggressor = Aggressor.NEUTRAL
        else:
            spread_position = None
            aggressor = Aggressor.NEUTRAL

        # 4. Update in-memory window
        if con_id not in self._windows:
            self._windows[con_id] = deque(maxlen=500)
        self._windows[con_id].append((tick, aggressor))

        cutoff = tick.timestamp - timedelta(seconds=s.classifier_window_seconds)
        window = self._windows[con_id]
        while window and window[0][0].timestamp < cutoff:
            window.popleft()

        # 5. Effective price + premium gate
        # Priority: last (when inside spread) > mid > last (unconstrained fallback).
        # last is guaranteed non-None by the early exit above.
        if bid is not None and ask is not None and bid <= last <= ask:
            effective_price: float | None = last
        elif tick.mid is not None:
            effective_price = tick.mid
        else:
            # bid or ask missing — use last directly as best available price.
            # WARNING: tick.last may be stale (previous session). Premium
            # computed here could be inaccurate. Logged for observability.
            logger.debug(
                "classify: using raw last={} as effective_price for con_id={} "
                "(bid={} ask={} unavailable)",
                last, con_id, bid, ask,
            )
            effective_price = last

        premium = tick.last_size * effective_price * 100
        if premium < s.min_premium:
            return None

        # 6. Classification: sweep -> split -> block -> unknown
        now = tick.timestamp

        recent_sweep = [
            (t, agg) for t, agg in window
            if (now - t.timestamp).total_seconds() <= s.sweep_window_seconds
        ]
        if len(recent_sweep) >= s.sweep_min_legs and _all_same_aggressor(recent_sweep):
            trade_type = TradeType.SWEEP
            window_ticks = len(recent_sweep)
        else:
            recent_split = [
                (t, agg) for t, agg in window
                if (now - t.timestamp).total_seconds() <= s.split_window_seconds
            ]
            if (
                len(recent_split) >= s.split_min_legs
                and _sizes_within_tolerance(recent_split, s.split_size_tolerance)
            ):
                trade_type = TradeType.SPLIT
                window_ticks = len(recent_split)
            elif tick.last_size >= s.min_block_size:
                trade_type = TradeType.BLOCK
                window_ticks = 1
            else:
                trade_type = TradeType.UNKNOWN
                window_ticks = 1

        # 7. Signal strength
        if tick.open_interest is None:
            signal_strength: float | None = None
        else:
            oi_ratio = min(volume_delta / max(tick.open_interest, 1), 10.0)
            signal_strength = log1p(premium / s.min_premium) * oi_ratio

        return ClassifiedTrade(
            symbol=tick.symbol,
            con_id=tick.con_id,
            expiry=tick.expiry,
            right=tick.right,
            strike=tick.strike,
            underlying_price=tick.underlying_price,
            implied_vol=tick.implied_vol,
            delta=tick.delta,
            trade_type=trade_type,
            aggressor=aggressor,
            spread_position=spread_position,
            effective_price=effective_price,
            last_size=tick.last_size,
            premium=premium,
            signal_strength=signal_strength,
            volume_delta=volume_delta,
            window_ticks=window_ticks,
            timestamp=tick.timestamp,
            tick=tick,
        )

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Evict state for contracts not seen in max_age_seconds.

        Must be called by the orchestration layer (e.g. once per hour).

        Args:
            max_age_seconds: Contracts with no ticks newer than this are evicted.

        Returns:
            Number of con_ids purged.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = [
            con_id for con_id, w in self._windows.items()
            if not w or w[-1][0].timestamp < cutoff
        ]
        for con_id in stale:
            del self._windows[con_id]
            self._last_volume.pop(con_id, None)
        if stale:
            logger.info("purge_stale: evicted {} stale contracts", len(stale))
        return len(stale)

    # Multi-leg hook (future implementation)
    # def _check_cross_contract(self, tick: TickUpdate) -> bool:
    #     """Detect multi-leg trades by correlating prints across contracts.
    #     Requires cross-contract window keyed by (symbol, timestamp_bucket).
    #     Not implemented — deferred. Placeholder TradeType.MULTI_LEG exists.
    #     """
    #     raise NotImplementedError


if __name__ == "__main__":
    from config.settings import Settings

    settings = Settings(min_premium=100.0)
    classifier = FlowClassifier(settings)

    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)

    # Simulate a sweep: 3 rapid BUY prints on the same contract
    for i in range(3):
        tick = TickUpdate(
            symbol="SPY", con_id=99999, expiry="20260320", strike=500.0, right="C",
            timestamp=base_time + timedelta(milliseconds=i * 400),
            bid=2.00, ask=2.50, last=2.45,
            volume=50 * (i + 1), open_interest=1000, last_size=50,
            underlying_price=500.0, implied_vol=0.25, delta=0.45,
        )
        result = classifier.classify(tick)
        if result:
            logger.info(
                "[tick {}] {} | type={} aggressor={} premium=${:.0f} signal={:.2f}",
                i + 1, result.symbol, result.trade_type.value,
                result.aggressor.value, result.premium or 0,
                result.signal_strength or 0,
            )

    logger.success("Smoke test complete.")
