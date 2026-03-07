# Flow Classifier Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/analysis/flow_classifier.py` — a synchronous, stateful classifier that labels live `TickUpdate` objects as SWEEP, SPLIT, BLOCK, or UNKNOWN trades and emits `ClassifiedTrade` objects.

**Architecture:** In-memory per-contract deque stores `(TickUpdate, Aggressor)` tuples. Volume deduplication via `_last_volume` dict prevents double-counting IBKR snapshots. `classify()` is synchronous with no IO. The module also adds a `ClassifiedTrade` ORM model and `insert_classified_trade` query to the storage layer.

**Tech Stack:** Python 3.11+, pydantic v2, pydantic-settings, loguru, SQLAlchemy async, pytest + pytest-asyncio

---

## Reference Files

Before starting, read these files to understand existing patterns:
- `docs/plans/2026-03-07-flow-classifier-design.md` — full design spec
- `src/data/tick_stream.py` — `TickUpdate` model (the input type)
- `config/settings.py` — existing settings (do NOT duplicate `min_block_size`, `min_premium`)
- `src/storage/models.py` — SQLAlchemy ORM pattern to follow
- `src/storage/queries.py` — async query pattern to follow
- `tests/conftest.py` — existing fixtures (`mock_settings`, `async_db_session`)

---

## Task 1: Extend Settings with Flow Classifier Thresholds

**Files:**
- Modify: `config/settings.py`
- Test: `tests/test_flow_classifier.py`

**Step 1: Write the failing tests**

Add to `tests/test_flow_classifier.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_settings_flow_classifier_defaults():
    """New flow classifier fields load with correct defaults."""
    s = Settings()
    assert s.sweep_window_seconds == 2.0
    assert s.sweep_min_legs == 3
    assert s.split_window_seconds == 5.0
    assert s.split_min_legs == 3
    assert s.split_size_tolerance == 0.20
    assert s.classifier_window_seconds == 30.0
    assert s.aggressor_buy_threshold == 0.70
    assert s.aggressor_sell_threshold == 0.30


def test_settings_min_premium_must_be_positive():
    """Settings raises ValidationError when min_premium <= 0."""
    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=0.0)

    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=-1.0)


def test_settings_min_premium_positive_is_valid():
    """Settings accepts any positive min_premium."""
    s = Settings(min_premium=1.0)
    assert s.min_premium == 1.0
```

**Step 2: Run to verify they fail**

```bash
cd "C:\Coding Projects\options-flow-analysis"
pytest tests/test_flow_classifier.py -v
```

Expected: FAIL — `Settings` has no `sweep_window_seconds` attribute.

**Step 3: Add new fields to `config/settings.py`**

Add after the `min_premium` field and before the Alert Endpoints section:

```python
    # Flow Classifier
    sweep_window_seconds: float = Field(
        default=2.0, description="Seconds window for sweep detection"
    )
    sweep_min_legs: int = Field(
        default=3, description="Minimum prints in sweep window to qualify as sweep"
    )
    split_window_seconds: float = Field(
        default=5.0, description="Seconds window for split detection"
    )
    split_min_legs: int = Field(
        default=3, description="Minimum prints in split window to qualify as split"
    )
    split_size_tolerance: float = Field(
        default=0.20, description="Max deviation from median size to qualify as split (0.20 = ±20%)"
    )
    classifier_window_seconds: float = Field(
        default=30.0, description="Max age of ticks kept in classifier in-memory window"
    )
    aggressor_buy_threshold: float = Field(
        default=0.70, description="Spread position >= this → BUY aggressor"
    )
    aggressor_sell_threshold: float = Field(
        default=0.30, description="Spread position <= this → SELL aggressor"
    )
```

Also add the validator. Add this import at the top of `config/settings.py`:

```python
from pydantic import Field, field_validator
```

Add the validator method inside the `Settings` class (after all field definitions, before the closing):

```python
    @field_validator("min_premium")
    @classmethod
    def min_premium_must_be_positive(cls, v: float) -> float:
        """Ensure min_premium > 0 to prevent division-by-zero in signal_strength."""
        if v <= 0:
            raise ValueError("min_premium must be greater than 0")
        return v
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_flow_classifier.py -v
```

Expected: 3 PASSED.

**Step 5: Commit**

```bash
git add config/settings.py tests/test_flow_classifier.py
git commit -m "feat: add flow classifier settings and min_premium validator"
```

---

## Task 2: Add Enums and ClassifiedTrade Model

**Files:**
- Modify: `src/analysis/flow_classifier.py`
- Test: `tests/test_flow_classifier.py`

**Step 1: Write the failing tests**

Add to `tests/test_flow_classifier.py`:

```python
from datetime import datetime, timezone

from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.data.tick_stream import TickUpdate


# ---------------------------------------------------------------------------
# Test fixture helpers
# ---------------------------------------------------------------------------

def make_tick(**overrides) -> TickUpdate:
    """Factory for TickUpdate with sensible defaults for unit tests."""
    defaults = dict(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc),
        bid=2.00,
        ask=2.50,
        last=2.45,
        volume=100,
        open_interest=1000,
        last_size=50,
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.45,
    )
    defaults.update(overrides)
    return TickUpdate(**defaults)


# ---------------------------------------------------------------------------
# ClassifiedTrade model tests
# ---------------------------------------------------------------------------

def test_classified_trade_constructs():
    """ClassifiedTrade builds correctly from all required fields."""
    tick = make_tick()
    trade = ClassifiedTrade(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        right=tick.right,
        strike=tick.strike,
        underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol,
        delta=tick.delta,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        spread_position=0.9,
        effective_price=2.45,
        last_size=50,
        premium=50 * 2.45 * 100,
        signal_strength=1.5,
        volume_delta=50,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    assert trade.trade_type == TradeType.BLOCK
    assert trade.aggressor == Aggressor.BUY
    assert trade.premium == pytest.approx(12250.0)


def test_classified_trade_tick_excluded_from_serialization():
    """tick field is excluded from model_dump() and model_dump_json()."""
    tick = make_tick()
    trade = ClassifiedTrade(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        right=tick.right,
        strike=tick.strike,
        underlying_price=None,
        implied_vol=None,
        delta=None,
        trade_type=TradeType.UNKNOWN,
        aggressor=Aggressor.NEUTRAL,
        spread_position=None,
        effective_price=None,
        last_size=None,
        premium=None,
        signal_strength=None,
        volume_delta=0,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    dumped = trade.model_dump()
    assert "tick" not in dumped
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_flow_classifier.py::test_classified_trade_constructs tests/test_flow_classifier.py::test_classified_trade_tick_excluded_from_serialization -v
```

Expected: FAIL — `cannot import name 'ClassifiedTrade'`.

**Step 3: Implement enums and ClassifiedTrade in `src/analysis/flow_classifier.py`**

```python
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
    MULTI_LEG = "multi_leg"   # placeholder — detection not implemented
    UNKNOWN = "unknown"


class Aggressor(str, Enum):
    """Directional side of a trade based on spread position."""

    BUY = "buy"       # spread_position >= aggressor_buy_threshold (default 0.70)
    SELL = "sell"     # spread_position <= aggressor_sell_threshold (default 0.30)
    NEUTRAL = "neutral"


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class ClassifiedTrade(BaseModel):
    """Result of classifying a single TickUpdate as a trade event.

    Emitted by FlowClassifier.classify(). The caller (orchestration layer)
    decides whether to persist, alert, or pass to downstream analysis modules.

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
        spread_position: Unclamped (last - bid) / (ask - bid). Values >1.0
            mean paid above ask; <0.0 mean hit below bid. Treat as
            probabilistic — stale quotes can produce out-of-range values.
            None when bid/ask/last unavailable or ask == bid.
        effective_price: Price used for premium computation. Equal to
            tick.last if bid <= last <= ask, otherwise tick.mid as fallback.
        last_size: Size of the triggering print in contracts.
        premium: last_size × effective_price × 100 (dollar value).
        signal_strength: log1p(premium / min_premium) × min(volume_delta /
            max(open_interest, 1), 10.0). None when open_interest unavailable.
        volume_delta: Increase in cumulative session volume since last tick.
            Approximated as last_size on first sight or session reset.
        window_ticks: Ticks in the detection window used for classification.
            len(sweep_window) for SWEEP, len(split_window) for SPLIT, 1 otherwise.
        timestamp: When the trade occurred (= tick.timestamp).
        tick: Full raw TickUpdate. Available in-memory; excluded from serialization.
    """

    # Identity
    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    underlying_price: float | None

    # Greeks
    implied_vol: float | None
    delta: float | None

    # Classification
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

    # Raw tick in-memory only
    tick: TickUpdate = Field(exclude=True)
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_flow_classifier.py::test_classified_trade_constructs tests/test_flow_classifier.py::test_classified_trade_tick_excluded_from_serialization -v
```

Expected: 2 PASSED.

**Step 5: Commit**

```bash
git add src/analysis/flow_classifier.py tests/test_flow_classifier.py
git commit -m "feat: add TradeType, Aggressor enums and ClassifiedTrade model"
```

---

## Task 3: Implement Helper Functions

**Files:**
- Modify: `src/analysis/flow_classifier.py`
- Test: `tests/test_flow_classifier.py`

**Step 1: Write the failing tests**

Add to `tests/test_flow_classifier.py`:

```python
from src.analysis.flow_classifier import _all_same_aggressor, _sizes_within_tolerance


# ---------------------------------------------------------------------------
# Helper: _all_same_aggressor
# ---------------------------------------------------------------------------

def test_all_same_aggressor_all_buy():
    entries = [(make_tick(), Aggressor.BUY)] * 3
    assert _all_same_aggressor(entries) is True


def test_all_same_aggressor_all_sell():
    entries = [(make_tick(), Aggressor.SELL)] * 3
    assert _all_same_aggressor(entries) is True


def test_all_same_aggressor_mixed():
    entries = [
        (make_tick(), Aggressor.BUY),
        (make_tick(), Aggressor.SELL),
        (make_tick(), Aggressor.BUY),
    ]
    assert _all_same_aggressor(entries) is False


def test_all_same_aggressor_neutral_ignored():
    """NEUTRAL entries do not count toward the aggressor set."""
    entries = [
        (make_tick(), Aggressor.BUY),
        (make_tick(), Aggressor.NEUTRAL),
        (make_tick(), Aggressor.BUY),
    ]
    assert _all_same_aggressor(entries) is True


def test_all_same_aggressor_all_neutral():
    """All-neutral is not a consistent aggressor direction."""
    entries = [(make_tick(), Aggressor.NEUTRAL)] * 3
    assert _all_same_aggressor(entries) is False


# ---------------------------------------------------------------------------
# Helper: _sizes_within_tolerance
# ---------------------------------------------------------------------------

def test_sizes_within_tolerance_uniform():
    """All equal sizes → within any tolerance."""
    entries = [(make_tick(last_size=100), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is True


def test_sizes_within_tolerance_within_20pct():
    """Sizes 100, 110, 115 → max deviation from median (110) is ~4.5% → pass."""
    entries = [
        (make_tick(last_size=100), Aggressor.BUY),
        (make_tick(last_size=110), Aggressor.BUY),
        (make_tick(last_size=115), Aggressor.BUY),
    ]
    assert _sizes_within_tolerance(entries, 0.20) is True


def test_sizes_within_tolerance_outside_20pct():
    """Sizes 100, 110, 200 → 200 is 82% above median (110) → fail."""
    entries = [
        (make_tick(last_size=100), Aggressor.BUY),
        (make_tick(last_size=110), Aggressor.BUY),
        (make_tick(last_size=200), Aggressor.BUY),
    ]
    assert _sizes_within_tolerance(entries, 0.20) is False


def test_sizes_within_tolerance_zero_median():
    """Zero median → return False (avoid division by zero)."""
    entries = [(make_tick(last_size=0), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is False


def test_sizes_within_tolerance_none_sizes_skipped():
    """None last_size entries are excluded; remaining must pass."""
    entries = [
        (make_tick(last_size=None), Aggressor.BUY),
        (make_tick(last_size=100), Aggressor.BUY),
        (make_tick(last_size=105), Aggressor.BUY),
    ]
    assert _sizes_within_tolerance(entries, 0.20) is True


def test_sizes_within_tolerance_all_none():
    """All None sizes → no data → return False."""
    entries = [(make_tick(last_size=None), Aggressor.BUY)] * 3
    assert _sizes_within_tolerance(entries, 0.20) is False
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_flow_classifier.py -k "aggressor or tolerance" -v
```

Expected: FAIL — `cannot import name '_all_same_aggressor'`.

**Step 3: Implement helpers in `src/analysis/flow_classifier.py`**

Add after the `ClassifiedTrade` class:

```python
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
    median = sorted(sizes)[len(sizes) // 2]
    if median == 0:
        return False
    return all(abs(s - median) / median <= tol for s in sizes)
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_flow_classifier.py -k "aggressor or tolerance" -v
```

Expected: 11 PASSED.

**Step 5: Commit**

```bash
git add src/analysis/flow_classifier.py tests/test_flow_classifier.py
git commit -m "feat: add _all_same_aggressor and _sizes_within_tolerance helpers"
```

---

## Task 4: Implement FlowClassifier — Init, purge_stale, and Aggressor Detection

**Files:**
- Modify: `src/analysis/flow_classifier.py`
- Test: `tests/test_flow_classifier.py`

**Step 1: Write the failing tests**

Add to `tests/test_flow_classifier.py`:

```python
from config.settings import Settings
from src.analysis.flow_classifier import FlowClassifier


@pytest.fixture
def flow_settings() -> Settings:
    """Settings with low min_premium so test ticks qualify easily."""
    return Settings(
        min_premium=100.0,       # low so test ticks with small premiums pass
        min_block_size=500,
        sweep_window_seconds=2.0,
        sweep_min_legs=3,
        split_window_seconds=5.0,
        split_min_legs=3,
        split_size_tolerance=0.20,
        classifier_window_seconds=30.0,
        aggressor_buy_threshold=0.70,
        aggressor_sell_threshold=0.30,
    )


@pytest.fixture
def classifier(flow_settings) -> FlowClassifier:
    return FlowClassifier(flow_settings)


# ---------------------------------------------------------------------------
# FlowClassifier: aggressor detection via classify()
# ---------------------------------------------------------------------------

def test_classify_aggressor_buy(classifier):
    """last near ask → BUY aggressor."""
    # bid=2.00, ask=2.50, last=2.45 → position=(2.45-2.00)/(2.50-2.00)=0.90 → BUY
    tick = make_tick(bid=2.00, ask=2.50, last=2.45, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.BUY
    assert result.spread_position == pytest.approx(0.90)


def test_classify_aggressor_sell(classifier):
    """last near bid → SELL aggressor."""
    # bid=2.00, ask=2.50, last=2.05 → position=0.10 → SELL
    tick = make_tick(bid=2.00, ask=2.50, last=2.05, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.SELL


def test_classify_aggressor_neutral(classifier):
    """last in middle of spread → NEUTRAL."""
    # bid=2.00, ask=2.50, last=2.25 → position=0.50 → NEUTRAL
    tick = make_tick(bid=2.00, ask=2.50, last=2.25, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL


def test_classify_aggressor_neutral_locked_market(classifier):
    """ask == bid (locked market) → NEUTRAL, spread_position is None."""
    tick = make_tick(bid=2.00, ask=2.00, last=2.00, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL
    assert result.spread_position is None


def test_classify_aggressor_above_ask(classifier):
    """last above ask → spread_position > 1.0 → still BUY (extreme urgency)."""
    tick = make_tick(bid=2.00, ask=2.50, last=2.70, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.BUY
    assert result.spread_position == pytest.approx(1.40)


def test_classify_aggressor_neutral_when_no_bid_ask(classifier):
    """Missing bid or ask → NEUTRAL, spread_position is None."""
    tick = make_tick(bid=None, ask=2.50, last=2.45, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.aggressor == Aggressor.NEUTRAL
    assert result.spread_position is None


# ---------------------------------------------------------------------------
# FlowClassifier: purge_stale
# ---------------------------------------------------------------------------

def test_purge_stale_removes_old_entries(classifier):
    """purge_stale() evicts contracts not seen within max_age_seconds."""
    old_tick = make_tick(
        con_id=111,
        timestamp=datetime(2026, 3, 7, 10, 0, 0, tzinfo=timezone.utc),
        volume=50,
        last_size=50,
    )
    recent_tick = make_tick(
        con_id=222,
        timestamp=datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc),
        volume=50,
        last_size=50,
    )
    classifier.classify(old_tick)
    classifier.classify(recent_tick)

    # Both contracts are in state now
    assert 111 in classifier._last_volume
    assert 222 in classifier._last_volume

    # Purge with 1 hour max age relative to now — old_tick is hours ago, recent is too
    # Use a very small max_age to force both out, then check count
    purged = classifier.purge_stale(max_age_seconds=1.0)
    assert purged == 2


def test_purge_stale_returns_zero_when_nothing_stale(classifier):
    """purge_stale() returns 0 when all contracts are recent."""
    tick = make_tick(volume=50, last_size=50)
    classifier.classify(tick)
    # max_age of 1 hour — this tick is "now" so won't be purged
    # We can't easily control "now" without mocking, so use a very large max_age
    purged = classifier.purge_stale(max_age_seconds=86400.0)
    assert purged == 0
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_flow_classifier.py -k "classify_aggressor or purge_stale" -v
```

Expected: FAIL — `cannot import name 'FlowClassifier'`.

**Step 3: Implement FlowClassifier `__init__`, `purge_stale`, and the full `classify()` stub**

Add after the helpers in `src/analysis/flow_classifier.py`:

```python
# ---------------------------------------------------------------------------
# FlowClassifier
# ---------------------------------------------------------------------------


class FlowClassifier:
    """Stateful real-time trade classifier.

    Maintains a per-contract in-memory window of recent (TickUpdate, Aggressor)
    tuples. classify() is synchronous and performs no IO — safe to call on the
    hot path.

    State is lost on process restart. This is acceptable: the classifier is
    designed for real-time use, and the restart blind spot is negligible.

    The orchestration layer MUST call purge_stale() periodically (e.g. hourly)
    to evict state for contracts no longer being tracked.

    Example:
        settings = Settings()
        classifier = FlowClassifier(settings)

        async for tick in stream:
            result = classifier.classify(tick)
            if result:
                await insert_classified_trade(session, result)

    Args:
        settings: Application settings with classification thresholds.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize with application settings.

        Args:
            settings: Application settings instance.
        """
        self._settings = settings
        # per con_id: recent (tick, aggressor) tuples within classifier_window_seconds
        # maxlen=500 is a memory safety cap; time-based pruning handles correctness
        self._windows: dict[int, deque[tuple[TickUpdate, Aggressor]]] = {}
        # per con_id: last seen cumulative session volume (for deduplication)
        self._last_volume: dict[int, int] = {}

    def classify(self, tick: TickUpdate) -> ClassifiedTrade | None:
        """Classify a TickUpdate as a trade event.

        Returns None (tick silently skipped) when:
        - tick.last_size, tick.last, or tick.volume is None (not a trade print)
        - volume_delta == 0 (duplicate IBKR snapshot, no new trades)
        - effective_price cannot be computed (no valid last or mid)
        - premium < settings.min_premium (trade too small to be meaningful)

        Args:
            tick: TickUpdate from TickStream.queue.

        Returns:
            ClassifiedTrade if the tick represents a qualifying trade, else None.
        """
        s = self._settings
        con_id = tick.con_id

        # --- 1. Early exits: required fields ---
        if tick.last_size is None or tick.last is None or tick.volume is None:
            return None

        # --- 2. Volume deduplication + session reset detection ---
        if con_id not in self._last_volume or tick.volume < self._last_volume[con_id]:
            # First sight of con_id, or IBKR session volume reset (new trading day).
            # Use last_size as best approximation of volume_delta.
            # Known limitation: volume_delta may undercount if session was already
            # in progress when classifier started.
            logger.warning(
                "classify: volume reset for con_id={} ({}→{})",
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

        # --- 3. Aggressor + spread_position ---
        bid, ask, last = tick.bid, tick.ask, tick.last
        if bid is not None and ask is not None and last is not None:
            if ask == bid:
                # Locked market — cannot determine direction from price alone
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

        # --- 4. Update in-memory window ---
        if con_id not in self._windows:
            self._windows[con_id] = deque(maxlen=500)
        self._windows[con_id].append((tick, aggressor))

        # Prune stale entries older than classifier_window_seconds
        cutoff = tick.timestamp - timedelta(seconds=s.classifier_window_seconds)
        window = self._windows[con_id]
        while window and window[0][0].timestamp < cutoff:
            window.popleft()

        # --- 5. Effective price + premium gate ---
        if bid is not None and ask is not None and last is not None and bid <= last <= ask:
            effective_price: float | None = last
        elif tick.mid is not None:
            effective_price = tick.mid
        else:
            return None

        premium = tick.last_size * effective_price * 100
        if premium < s.min_premium:
            return None

        # --- 6. Classification ---
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
            if (len(recent_split) >= s.split_min_legs
                    and _sizes_within_tolerance(recent_split, s.split_size_tolerance)):
                trade_type = TradeType.SPLIT
                window_ticks = len(recent_split)

            elif tick.last_size >= s.min_block_size:
                trade_type = TradeType.BLOCK
                window_ticks = 1

            else:
                trade_type = TradeType.UNKNOWN
                window_ticks = 1

        # --- 7. Signal strength ---
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
        Without periodic purging, _windows and _last_volume accumulate entries
        for expired or unsubscribed contracts indefinitely.

        Args:
            max_age_seconds: Contracts with no ticks newer than this are evicted.

        Returns:
            Number of con_ids purged.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = [
            con_id for con_id, window in self._windows.items()
            if not window or window[-1][0].timestamp < cutoff
        ]
        for con_id in stale:
            del self._windows[con_id]
            self._last_volume.pop(con_id, None)
        if stale:
            logger.info("purge_stale: evicted {} stale contracts", len(stale))
        return len(stale)

    # ---------------------------------------------------------------------------
    # Multi-leg hook (future implementation)
    # ---------------------------------------------------------------------------
    # def _check_cross_contract(self, tick: TickUpdate) -> bool:
    #     """Detect multi-leg trades by correlating prints across contracts.
    #
    #     Implementation requires a cross-contract window keyed by
    #     (symbol, timestamp_bucket) to match related prints (call + put)
    #     arriving within a short time window on the same underlying.
    #
    #     Not implemented — deferred to a future iteration.
    #     Placeholder TradeType.MULTI_LEG exists in the enum.
    #     """
    #     raise NotImplementedError
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_flow_classifier.py -k "classify_aggressor or purge_stale" -v
```

Expected: all aggressor and purge_stale tests PASSED.

**Step 5: Commit**

```bash
git add src/analysis/flow_classifier.py tests/test_flow_classifier.py
git commit -m "feat: implement FlowClassifier with classify() and purge_stale()"
```

---

## Task 5: Test Classification Logic (Sweep, Split, Block, Unknown)

**Files:**
- Test: `tests/test_flow_classifier.py`

**Step 1: Write the failing tests**

Add to `tests/test_flow_classifier.py`:

```python
# ---------------------------------------------------------------------------
# classify(): early exits
# ---------------------------------------------------------------------------

def test_classify_returns_none_when_last_size_none(classifier):
    tick = make_tick(last_size=None, volume=50)
    assert classifier.classify(tick) is None


def test_classify_returns_none_when_last_none(classifier):
    tick = make_tick(last=None, last_size=50, volume=50)
    assert classifier.classify(tick) is None


def test_classify_returns_none_when_volume_none(classifier):
    tick = make_tick(volume=None, last_size=50)
    assert classifier.classify(tick) is None


def test_classify_returns_none_when_volume_delta_zero(classifier):
    """Duplicate snapshot: volume unchanged → skip."""
    tick = make_tick(volume=100, last_size=50)
    classifier.classify(tick)       # seeds _last_volume[con_id] = 100
    tick2 = make_tick(volume=100, last_size=50)  # same volume
    assert classifier.classify(tick2) is None


def test_classify_returns_none_below_min_premium(classifier):
    """Premium below min_premium → skip. min_premium=100, need last_size*price*100 >= 100."""
    # last_size=1, last=0.50 → premium=50 < 100
    tick = make_tick(last_size=1, last=0.50, bid=0.45, ask=0.55, volume=1)
    assert classifier.classify(tick) is None


def test_classify_returns_none_no_effective_price(classifier):
    """No bid/ask and no mid → cannot compute effective price → skip."""
    tick = make_tick(bid=None, ask=None, last=None, last_size=50, volume=50)
    assert classifier.classify(tick) is None


# ---------------------------------------------------------------------------
# classify(): volume deduplication
# ---------------------------------------------------------------------------

def test_classify_volume_delta_computed_correctly(classifier):
    """volume_delta = tick.volume - previous volume."""
    tick1 = make_tick(volume=100, last_size=100)
    classifier.classify(tick1)  # seeds _last_volume = 100

    tick2 = make_tick(volume=150, last_size=50)
    result = classifier.classify(tick2)
    assert result is not None
    assert result.volume_delta == 50


def test_classify_session_reset_uses_last_size(classifier):
    """When volume drops (session reset), volume_delta falls back to last_size."""
    tick1 = make_tick(volume=5000, last_size=50)
    classifier.classify(tick1)

    tick2 = make_tick(volume=10, last_size=30)  # volume dropped → reset
    result = classifier.classify(tick2)
    assert result is not None
    assert result.volume_delta == 30   # last_size, not volume delta


# ---------------------------------------------------------------------------
# classify(): trade type — BLOCK
# ---------------------------------------------------------------------------

def test_classify_block(classifier):
    """Single large print ≥ min_block_size → BLOCK."""
    # min_block_size=500; last_size=600 qualifies
    tick = make_tick(last_size=600, volume=600, bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.BLOCK
    assert result.window_ticks == 1


def test_classify_block_uses_last_size_not_volume_delta(classifier):
    """Block check uses last_size, not volume_delta."""
    # Seed volume = 4500. New tick has volume=5100 (delta=600) but last_size=600 → BLOCK
    tick1 = make_tick(volume=4500, last_size=50)
    classifier.classify(tick1)

    tick2 = make_tick(volume=5100, last_size=600, bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick2)
    assert result is not None
    assert result.trade_type == TradeType.BLOCK


# ---------------------------------------------------------------------------
# classify(): trade type — UNKNOWN
# ---------------------------------------------------------------------------

def test_classify_unknown_small_single_print(classifier):
    """Single small print below block threshold → UNKNOWN."""
    tick = make_tick(last_size=10, volume=10, bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    assert result.trade_type == TradeType.UNKNOWN
    assert result.window_ticks == 1


# ---------------------------------------------------------------------------
# classify(): trade type — SWEEP
# ---------------------------------------------------------------------------

def test_classify_sweep(classifier):
    """3 rapid same-direction prints within sweep_window → SWEEP."""
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)

    for i, offset_ms in enumerate([0, 500, 1000]):
        ts = base_time.replace(microsecond=offset_ms * 1000)
        tick = make_tick(
            last_size=50,
            volume=50 * (i + 1),
            bid=2.00, ask=2.50, last=2.45,   # BUY aggressor
            timestamp=ts,
        )
        result = classifier.classify(tick)

    # Third tick should trigger SWEEP
    assert result is not None
    assert result.trade_type == TradeType.SWEEP
    assert result.window_ticks == 3


def test_classify_sweep_requires_same_aggressor(classifier):
    """Mixed aggressor direction → not a sweep."""
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)

    ticks_data = [
        (0,    2.45),   # BUY  (position=0.90)
        (500,  2.05),   # SELL (position=0.10)
        (1000, 2.45),   # BUY
    ]
    result = None
    for i, (offset_ms, last_price) in enumerate(ticks_data):
        ts = base_time.replace(microsecond=offset_ms * 1000)
        tick = make_tick(
            last_size=50,
            volume=50 * (i + 1),
            bid=2.00, ask=2.50, last=last_price,
            timestamp=ts,
        )
        result = classifier.classify(tick)

    assert result is not None
    assert result.trade_type != TradeType.SWEEP


# ---------------------------------------------------------------------------
# classify(): trade type — SPLIT
# ---------------------------------------------------------------------------

def test_classify_split(classifier):
    """3 uniform-sized prints within split_window but NOT within sweep_window → SPLIT."""
    base_time = datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc)

    # Use mixed aggressor so sweep is ruled out; uniform sizes for split
    ticks_data = [
        (0,    2.45, 100),   # BUY
        (3000, 2.05, 100),   # SELL (3s gap — outside 2s sweep window)
        (4000, 2.25, 100),   # NEUTRAL
    ]
    result = None
    for i, (offset_ms, last_price, size) in enumerate(ticks_data):
        ts = base_time + timedelta(milliseconds=offset_ms)
        tick = make_tick(
            last_size=size,
            volume=size * (i + 1),
            bid=2.00, ask=2.50, last=last_price,
            timestamp=ts,
        )
        result = classifier.classify(tick)

    assert result is not None
    assert result.trade_type == TradeType.SPLIT
    assert result.window_ticks == 3


# ---------------------------------------------------------------------------
# classify(): signal strength
# ---------------------------------------------------------------------------

def test_classify_signal_strength_none_when_no_oi(classifier):
    """signal_strength is None when open_interest is None."""
    tick = make_tick(last_size=50, volume=50, open_interest=None)
    result = classifier.classify(tick)
    assert result is not None
    assert result.signal_strength is None


def test_classify_signal_strength_positive(classifier):
    """signal_strength > 0 when premium > min_premium and OI is known."""
    # min_premium=100, premium=50*2.45*100=12250 → log1p(12250/100)=log1p(122.5)≈4.82
    tick = make_tick(last_size=50, volume=50, open_interest=1000,
                     bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    assert result.signal_strength is not None
    assert result.signal_strength > 0


def test_classify_signal_strength_capped_at_10x_oi(classifier):
    """OI multiplier is capped at 10.0 regardless of volume/OI ratio."""
    # volume_delta=50, open_interest=1 → raw ratio=50 → capped at 10.0
    tick = make_tick(last_size=50, volume=50, open_interest=1,
                     bid=2.00, ask=2.50, last=2.45)
    result = classifier.classify(tick)
    assert result is not None
    from math import log1p
    expected_max = log1p(result.premium / 100.0) * 10.0
    assert result.signal_strength == pytest.approx(expected_max)


# ---------------------------------------------------------------------------
# classify(): effective price fallback
# ---------------------------------------------------------------------------

def test_classify_effective_price_uses_last_when_in_spread(classifier):
    """effective_price = last when bid <= last <= ask."""
    tick = make_tick(bid=2.00, ask=2.50, last=2.45, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.effective_price == pytest.approx(2.45)


def test_classify_effective_price_falls_back_to_mid(classifier):
    """effective_price = mid when last is outside [bid, ask]."""
    # last=2.70 > ask=2.50 → falls back to mid=(2.00+2.50)/2=2.25
    tick = make_tick(bid=2.00, ask=2.50, last=2.70, last_size=50, volume=50)
    result = classifier.classify(tick)
    assert result is not None
    assert result.effective_price == pytest.approx(2.25)
```

**Step 2: Run all tests**

```bash
pytest tests/test_flow_classifier.py -v
```

Expected: All tests PASSED (the full classify() was already implemented in Task 4).

**Step 3: Commit**

```bash
git add tests/test_flow_classifier.py
git commit -m "test: add comprehensive flow classifier unit tests"
```

---

## Task 6: Add ClassifiedTrade ORM Model to Storage

**Files:**
- Modify: `src/storage/models.py`
- Test: `tests/test_storage.py`

**Step 1: Write the failing test**

Add to `tests/test_storage.py`:

```python
from src.storage.models import ClassifiedTradeRecord
from src.analysis.flow_classifier import Aggressor, TradeType


@pytest.mark.asyncio
async def test_classified_trade_record_insert(async_db_session):
    """ClassifiedTradeRecord inserts and reads back correctly."""
    from datetime import datetime
    record = ClassifiedTradeRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20260320",
        strike=500.0,
        right="C",
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.45,
        trade_type=TradeType.BLOCK.value,
        aggressor=Aggressor.BUY.value,
        spread_position=0.90,
        effective_price=2.45,
        last_size=600,
        premium=147000.0,
        signal_strength=3.5,
        volume_delta=600,
        window_ticks=1,
        classified_at=datetime(2026, 3, 7, 14, 30, 0),
    )
    async_db_session.add(record)
    await async_db_session.flush()
    assert record.id is not None
    assert record.trade_type == "block"
    assert record.symbol == "SPY"
```

**Step 2: Run to verify it fails**

```bash
pytest tests/test_storage.py::test_classified_trade_record_insert -v
```

Expected: FAIL — `cannot import name 'ClassifiedTradeRecord'`.

**Step 3: Add ClassifiedTradeRecord to `src/storage/models.py`**

Add after the `OptionTick` class:

```python
class ClassifiedTradeRecord(Base):
    """One row per ClassifiedTrade emitted by FlowClassifier.

    Persisted by the orchestration layer via insert_classified_trade().
    The 'tick' field from ClassifiedTrade is intentionally omitted here
    — raw tick data lives in option_ticks; this table stores the derived result.

    Note: trade_type and aggressor are stored as plain strings (enum values)
    for SQLite compatibility. PostgreSQL migration can use native enums.
    """

    __tablename__ = "classified_trades"
    __table_args__ = (
        Index("ix_classified_trades_symbol_at", "symbol", "classified_at"),
        Index("ix_classified_trades_con_id_at", "con_id", "classified_at"),
        Index("ix_classified_trades_symbol_aggressor_at", "symbol", "aggressor", "classified_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)

    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    trade_type: Mapped[str] = mapped_column(String, nullable=False)      # TradeType.value
    aggressor: Mapped[str] = mapped_column(String, nullable=False)        # Aggressor.value
    spread_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    window_ticks: Mapped[int] = mapped_column(Integer, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # classified_at = tick.timestamp (when trade occurred, not when classified)
```

**Step 4: Run the test**

```bash
pytest tests/test_storage.py::test_classified_trade_record_insert -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/storage/models.py tests/test_storage.py
git commit -m "feat: add ClassifiedTradeRecord ORM model to storage"
```

---

## Task 7: Add insert_classified_trade Query

**Files:**
- Modify: `src/storage/queries.py`
- Modify: `src/storage/__init__.py`
- Test: `tests/test_storage.py`

**Step 1: Write the failing test**

Add to `tests/test_storage.py`:

```python
from src.storage import insert_classified_trade
from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
from src.data.tick_stream import TickUpdate
from datetime import datetime, timezone


def make_classified_trade() -> ClassifiedTrade:
    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=datetime(2026, 3, 7, 14, 30, 0, tzinfo=timezone.utc),
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.45,
    )
    from pydantic import Field
    return ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.90, effective_price=2.45, last_size=600,
        premium=147000.0, signal_strength=3.5, volume_delta=600,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )


@pytest.mark.asyncio
async def test_insert_classified_trade_returns_id(async_db_session):
    """insert_classified_trade returns an integer PK."""
    trade = make_classified_trade()
    trade_id = await insert_classified_trade(async_db_session, trade)
    assert isinstance(trade_id, int)
    assert trade_id > 0


@pytest.mark.asyncio
async def test_insert_classified_trade_persists_fields(async_db_session):
    """Persisted ClassifiedTradeRecord matches the source ClassifiedTrade."""
    from sqlalchemy import select
    from src.storage.models import ClassifiedTradeRecord

    trade = make_classified_trade()
    trade_id = await insert_classified_trade(async_db_session, trade)

    result = await async_db_session.execute(
        select(ClassifiedTradeRecord).where(ClassifiedTradeRecord.id == trade_id)
    )
    record = result.scalar_one()

    assert record.symbol == "SPY"
    assert record.con_id == 12345
    assert record.trade_type == "block"
    assert record.aggressor == "buy"
    assert record.premium == pytest.approx(147000.0)
    assert record.volume_delta == 600
    assert record.classified_at == datetime(2026, 3, 7, 14, 30, 0)
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_storage.py::test_insert_classified_trade_returns_id tests/test_storage.py::test_insert_classified_trade_persists_fields -v
```

Expected: FAIL — `cannot import name 'insert_classified_trade'`.

**Step 3: Add `insert_classified_trade` to `src/storage/queries.py`**

Add after `get_recent_ticks`:

```python
async def insert_classified_trade(
    session: AsyncSession, trade: ClassifiedTrade
) -> int:
    """Persist a ClassifiedTrade emitted by FlowClassifier.

    The 'tick' field on ClassifiedTrade is intentionally excluded —
    raw tick data is persisted separately via insert_tick().

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        trade: The ClassifiedTrade returned by FlowClassifier.classify().

    Returns:
        The auto-generated primary key of the new classified_trades row.
    """
    record = ClassifiedTradeRecord(
        con_id=trade.con_id,
        symbol=trade.symbol,
        expiry=trade.expiry,
        strike=trade.strike,
        right=trade.right,
        underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol,
        delta=trade.delta,
        trade_type=trade.trade_type.value,
        aggressor=trade.aggressor.value,
        spread_position=trade.spread_position,
        effective_price=trade.effective_price,
        last_size=trade.last_size,
        premium=trade.premium,
        signal_strength=trade.signal_strength,
        volume_delta=trade.volume_delta,
        window_ticks=trade.window_ticks,
        classified_at=trade.timestamp.replace(tzinfo=None),  # naive UTC for SQLite
    )
    session.add(record)
    await session.flush()
    return record.id
```

Also add the import for `ClassifiedTrade` and `ClassifiedTradeRecord` at the top of `queries.py`:

```python
from src.analysis.flow_classifier import ClassifiedTrade
from src.storage.models import ChainSnapshot, ClassifiedTradeRecord, OptionContractRecord, OptionTick
```

**Step 4: Export from `src/storage/__init__.py`**

Add `insert_classified_trade` to the exports in `src/storage/__init__.py`:

```python
from src.storage.queries import (
    get_latest_snapshot,
    get_recent_ticks,
    insert_chain_snapshot,
    insert_classified_trade,
    insert_tick,
)

__all__ = [
    "get_latest_snapshot",
    "get_recent_ticks",
    "insert_chain_snapshot",
    "insert_classified_trade",
    "insert_tick",
]
```

**Step 5: Run all storage tests**

```bash
pytest tests/test_storage.py -v
```

Expected: all PASSED.

**Step 6: Run the full test suite**

```bash
pytest tests/ -v -m "not integration"
```

Expected: all non-integration tests PASSED.

**Step 7: Commit**

```bash
git add src/storage/queries.py src/storage/__init__.py tests/test_storage.py
git commit -m "feat: add insert_classified_trade query and storage export"
```

---

## Task 8: Add Standalone Smoke Test and Update Memory

**Files:**
- Modify: `src/analysis/flow_classifier.py`
- Modify: `C:\Users\kenny\.claude\projects\C--Coding-Projects-options-flow-analysis\memory\MEMORY.md`

**Step 1: Add `__main__` block to `src/analysis/flow_classifier.py`**

```python
if __name__ == "__main__":
    from datetime import datetime, timezone
    from config.settings import Settings
    from src.data.tick_stream import TickUpdate

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
```

**Step 2: Run it**

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m src.analysis.flow_classifier
```

Expected: 3 log lines, final one shows `type=sweep`.

**Step 3: Update memory**

Update `MEMORY.md` to reflect Step 6 completion:

```markdown
- Step 6: src/analysis/flow_classifier.py — DONE
```

Add to Key Patterns:
```markdown
- FlowClassifier: synchronous classify(tick) → ClassifiedTrade | None; purge_stale() called hourly by orchestration layer
- ClassifiedTrade: tick field excluded from serialization (Field(exclude=True)); effective_price not raw last
- insert_classified_trade: in storage/queries.py; maps trade.timestamp → classified_at (naive UTC)
```

**Step 4: Final commit**

```bash
git add src/analysis/flow_classifier.py
git commit -m "feat: add flow_classifier smoke test entry point"
```

---

## Done

Run the full suite one final time to confirm clean state:

```bash
pytest tests/ -v -m "not integration"
```

All tests should pass. Step 6 of the module build order is complete.
