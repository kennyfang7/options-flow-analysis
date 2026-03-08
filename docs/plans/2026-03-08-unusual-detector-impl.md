# Unusual Activity Detector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/analysis/unusual_detector.py` — an async, threshold-based detector that wraps each qualifying `ClassifiedTrade` in an `UnusualSignal` explaining *why* it was flagged.

**Architecture:** Stateless except for a lightweight OI cache (`dict[int, int]`) that persists the last-known open interest per contract across ticks, since IBKR sends OI infrequently as a separate tick type. `detect()` is async (no IO today, but future DB-backed baselines will require it). Emits `UnusualSignal` objects — persistence is the orchestration layer's responsibility.

**Tech Stack:** Python 3.11+, pydantic v2, pydantic-settings, loguru, SQLAlchemy async, pytest + pytest-asyncio

---

## Reference Files

Read before starting:
- `docs/plans/2026-03-08-unusual-detector-design.md` — full design spec
- `src/analysis/flow_classifier.py` — `ClassifiedTrade`, `TradeType`, `Aggressor` (input types)
- `config/settings.py` — existing settings structure; add after line 72 (end of Flow Classifier section)
- `src/storage/models.py` — ORM pattern to follow
- `src/storage/queries.py` — async query pattern to follow
- `tests/conftest.py` — existing fixtures
- `tests/test_flow_classifier.py` — `make_tick()` factory pattern to replicate

---

## Task 1: Extend Settings with Unusual Detector Thresholds

**Files:**
- Modify: `config/settings.py` (after line 72, before `# Alert Endpoints`)
- Test: `tests/test_unusual_detector.py` (create new file)

**Step 1: Create `tests/test_unusual_detector.py` with the failing settings tests**

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_unusual_detector_settings_defaults():
    """All unusual detector settings load with correct defaults."""
    s = Settings()
    assert s.unusual_premium_threshold == 250_000.0
    assert s.unusual_oi_ratio_threshold == 0.50
    assert s.unusual_signal_threshold == 5.0
    assert s.otm_delta_threshold == 0.30
    assert s.otm_premium_threshold == 100_000.0


def test_unusual_premium_threshold_must_exceed_min_premium():
    """ValidationError when unusual_premium_threshold <= min_premium."""
    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=50.0)

    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=100.0)


def test_unusual_premium_threshold_valid_when_above_min_premium():
    """No error when unusual_premium_threshold > min_premium."""
    s = Settings(min_premium=100.0, unusual_premium_threshold=200.0)
    assert s.unusual_premium_threshold == 200.0


def test_oi_ratio_threshold_must_be_positive():
    """ValidationError when unusual_oi_ratio_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=0.0)

    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=-1.0)


def test_otm_delta_threshold_must_be_between_0_and_1():
    """ValidationError when otm_delta_threshold is 0 or 1."""
    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=0.0)

    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=1.0)


def test_unusual_signal_threshold_must_be_positive():
    """ValidationError when unusual_signal_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_signal_threshold must be greater than 0"):
        Settings(unusual_signal_threshold=0.0)
```

**Step 2: Run to verify they fail**

```bash
cd "C:\Coding Projects\options-flow-analysis"
pytest tests/test_unusual_detector.py -v
```

Expected: FAIL — `Settings` has no `unusual_premium_threshold` attribute.

**Step 3: Add the new fields and validators to `config/settings.py`**

Insert after `aggressor_sell_threshold` field (line 72) and before `# Alert Endpoints` (line 74):

```python
    # Unusual Activity Detector
    unusual_premium_threshold: float = Field(
        default=250_000.0,
        description="Minimum single-trade premium ($) to flag as PREMIUM_SIZE",
    )
    unusual_oi_ratio_threshold: float = Field(
        default=0.50,
        description="Minimum volume_delta/open_interest ratio to flag as OI_RATIO",
    )
    unusual_signal_threshold: float = Field(
        default=5.0,
        description="Minimum signal_strength score to flag as SIGNAL_STRENGTH",
    )
    otm_delta_threshold: float = Field(
        default=0.30,
        description="Maximum |delta| to consider a contract OTM for OTM_PREMIUM check",
    )
    otm_premium_threshold: float = Field(
        default=100_000.0,
        description="Minimum premium ($) for an OTM contract to flag as OTM_PREMIUM",
    )
```

Then add three validators after the existing `aggressor_thresholds_are_ordered` validator:

```python
    @model_validator(mode="after")
    def unusual_premium_above_min_premium(self) -> Settings:
        """Ensure unusual_premium_threshold > min_premium to avoid dead PREMIUM_SIZE condition."""
        if self.unusual_premium_threshold <= self.min_premium:
            raise ValueError(
                f"unusual_premium_threshold ({self.unusual_premium_threshold}) "
                f"must exceed min_premium ({self.min_premium})"
            )
        return self

    @field_validator("unusual_oi_ratio_threshold")
    @classmethod
    def oi_ratio_threshold_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("unusual_oi_ratio_threshold must be greater than 0")
        return v

    @field_validator("otm_delta_threshold")
    @classmethod
    def otm_delta_threshold_must_be_in_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("otm_delta_threshold must be between 0 and 1 (exclusive)")
        return v

    @field_validator("unusual_signal_threshold")
    @classmethod
    def unusual_signal_threshold_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("unusual_signal_threshold must be greater than 0")
        return v
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_unusual_detector.py -v
```

Expected: 7 PASSED.

**Step 5: Run full suite to ensure no regressions**

```bash
pytest tests/ -m "not integration" --tb=short
```

Expected: all non-integration tests pass.

**Step 6: Commit**

```bash
git add config/settings.py tests/test_unusual_detector.py
git commit -m "feat: add unusual detector settings and validators"
```

---

## Task 2: Add UnusualReason Enum and UnusualSignal Model

**Files:**
- Create: `src/analysis/unusual_detector.py`
- Test: `tests/test_unusual_detector.py`

**Step 1: Add the failing model tests to `tests/test_unusual_detector.py`**

```python
from datetime import datetime, timezone

from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.analysis.unusual_detector import UnusualReason, UnusualSignal
from src.data.tick_stream import TickUpdate


def make_tick(**overrides) -> TickUpdate:
    """Factory for TickUpdate with sensible defaults for unit tests."""
    defaults = dict(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
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


def make_trade(tick: TickUpdate | None = None, **overrides) -> ClassifiedTrade:
    """Factory for ClassifiedTrade with sensible defaults for unit tests."""
    if tick is None:
        tick = make_tick()
    defaults = dict(
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
        premium=12_250.0,   # 50 * 2.45 * 100
        signal_strength=1.0,
        volume_delta=50,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


# ---------------------------------------------------------------------------
# UnusualSignal model tests
# ---------------------------------------------------------------------------

def test_unusual_signal_constructs():
    """UnusualSignal builds from all required fields."""
    trade = make_trade()
    signal = UnusualSignal(
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
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        trade=trade,
    )
    assert signal.top_reason == UnusualReason.PREMIUM_SIZE
    assert signal.symbol == "SPY"


def test_unusual_signal_trade_excluded_from_serialization():
    """trade field is excluded from model_dump()."""
    trade = make_trade()
    signal = UnusualSignal(
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
        reasons=[UnusualReason.OI_RATIO],
        top_reason=UnusualReason.OI_RATIO,
        flagged_at=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        trade=trade,
    )
    dumped = signal.model_dump()
    assert "trade" not in dumped
    assert "symbol" in dumped
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_unusual_detector.py::test_unusual_signal_constructs tests/test_unusual_detector.py::test_unusual_signal_trade_excluded_from_serialization -v
```

Expected: FAIL — `cannot import name 'UnusualSignal'`.

**Step 3: Create `src/analysis/unusual_detector.py`**

```python
from __future__ import annotations

from collections import defaultdict
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
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_unusual_detector.py::test_unusual_signal_constructs tests/test_unusual_detector.py::test_unusual_signal_trade_excluded_from_serialization -v
```

Expected: 2 PASSED.

**Step 5: Commit**

```bash
git add src/analysis/unusual_detector.py tests/test_unusual_detector.py
git commit -m "feat: add UnusualReason enum and UnusualSignal model"
```

---

## Task 3: Implement UnusualDetector Class

**Files:**
- Modify: `src/analysis/unusual_detector.py`
- Test: `tests/test_unusual_detector.py`

**Step 1: Add the failing detector tests to `tests/test_unusual_detector.py`**

```python
from config.settings import Settings
from src.analysis.unusual_detector import UnusualDetector


@pytest.fixture
def unusual_settings() -> Settings:
    """Settings with low thresholds so test trades qualify easily."""
    return Settings(
        min_premium=100.0,
        min_block_size=500,
        unusual_premium_threshold=500.0,     # > min_premium=100
        unusual_oi_ratio_threshold=0.50,
        unusual_signal_threshold=5.0,
        otm_delta_threshold=0.30,
        otm_premium_threshold=200.0,
    )


@pytest.fixture
def detector(unusual_settings) -> UnusualDetector:
    return UnusualDetector(unusual_settings)


# ---------------------------------------------------------------------------
# detect(): returns None when no conditions fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_returns_none_when_no_conditions_fire(detector):
    """Trade below all thresholds → None."""
    # premium=122.50 < 500, volume_delta/OI=50/1000=0.05 < 0.50,
    # signal_strength=1.0 < 5.0, delta=0.45 > 0.30 (not OTM)
    trade = make_trade()
    assert await detector.detect(trade) is None


@pytest.mark.asyncio
async def test_detect_returns_none_for_multi_leg(detector):
    """MULTI_LEG trades are skipped — detection semantics undefined."""
    trade = make_trade(trade_type=TradeType.MULTI_LEG)
    assert await detector.detect(trade) is None


@pytest.mark.asyncio
async def test_detect_returns_none_when_oi_cache_empty_and_no_oi_on_tick(detector):
    """OI_RATIO cannot fire when cache is empty and tick has no OI."""
    tick = make_tick(open_interest=None)
    # volume_delta/OI cannot be computed — OI_RATIO silently skipped
    # Other conditions also don't fire (premium=12250 < 500 threshold... wait)
    # Actually premium=12250 > 500, so PREMIUM_SIZE fires.
    # Use a trade with premium below threshold to isolate the OI check.
    trade = make_trade(tick=tick, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None  # no OI cache, no other conditions


# ---------------------------------------------------------------------------
# detect(): PREMIUM_SIZE condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_premium_size_fires(detector):
    """premium >= unusual_premium_threshold → PREMIUM_SIZE."""
    # unusual_premium_threshold=500.0; set premium=600.0
    trade = make_trade(premium=600.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.PREMIUM_SIZE in result.reasons
    assert result.top_reason == UnusualReason.PREMIUM_SIZE


@pytest.mark.asyncio
async def test_detect_premium_size_does_not_fire_below_threshold(detector):
    """premium < unusual_premium_threshold → PREMIUM_SIZE does not fire."""
    trade = make_trade(premium=400.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_premium_size_does_not_fire_when_premium_none(detector):
    """premium=None → PREMIUM_SIZE silently skipped."""
    trade = make_trade(premium=None, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): OI_RATIO condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_oi_ratio_fires_from_live_tick(detector):
    """volume_delta / tick.open_interest >= threshold → OI_RATIO."""
    # OI=100, volume_delta=60 → ratio=0.60 >= 0.50
    tick = make_tick(open_interest=100)
    trade = make_trade(tick=tick, volume_delta=60, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OI_RATIO in result.reasons


@pytest.mark.asyncio
async def test_detect_oi_ratio_uses_cached_oi(detector):
    """OI from prior tick is used even when current tick has OI=None."""
    # First tick populates cache with OI=100
    tick1 = make_tick(open_interest=100)
    trade1 = make_trade(tick=tick1, volume_delta=10, premium=150.0, signal_strength=1.0, delta=0.45)
    await detector.detect(trade1)

    # Second tick has OI=None but cache still has 100
    tick2 = make_tick(open_interest=None)
    trade2 = make_trade(tick=tick2, volume_delta=60, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade2)
    assert result is not None
    assert UnusualReason.OI_RATIO in result.reasons


@pytest.mark.asyncio
async def test_detect_oi_ratio_does_not_fire_below_threshold(detector):
    """volume_delta / OI < threshold → OI_RATIO does not fire."""
    # OI=1000, volume_delta=10 → ratio=0.01 < 0.50
    tick = make_tick(open_interest=1000)
    trade = make_trade(tick=tick, volume_delta=10, premium=150.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): SIGNAL_STRENGTH condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_signal_strength_fires(detector):
    """signal_strength >= unusual_signal_threshold → SIGNAL_STRENGTH."""
    trade = make_trade(signal_strength=6.0, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.SIGNAL_STRENGTH in result.reasons


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_below_threshold(detector):
    """signal_strength < threshold → SIGNAL_STRENGTH does not fire."""
    trade = make_trade(signal_strength=4.9, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_when_none(detector):
    """signal_strength=None → SIGNAL_STRENGTH silently skipped (no open_interest)."""
    trade = make_trade(signal_strength=None, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_signal_strength_does_not_fire_at_zero(detector):
    """signal_strength=0.0 must not pass threshold=5.0 (truthiness trap guard)."""
    trade = make_trade(signal_strength=0.0, premium=150.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): OTM_PREMIUM condition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_otm_premium_fires(detector):
    """|delta|=0.20 (OTM) and premium=300.0 >= otm_premium_threshold=200.0 → OTM_PREMIUM."""
    trade = make_trade(delta=0.20, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OTM_PREMIUM in result.reasons


@pytest.mark.asyncio
async def test_detect_otm_premium_fires_for_put(detector):
    """Put delta is negative; abs() used correctly."""
    trade = make_trade(delta=-0.20, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.OTM_PREMIUM in result.reasons


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_when_itm(detector):
    """|delta|=0.70 (ITM) → OTM_PREMIUM does not fire even with large premium."""
    trade = make_trade(delta=0.70, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_below_otm_threshold(detector):
    """|delta|=0.20 (OTM) but premium=150.0 < otm_premium_threshold=200.0."""
    trade = make_trade(delta=0.20, premium=150.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


@pytest.mark.asyncio
async def test_detect_otm_premium_does_not_fire_when_delta_none(detector):
    """delta=None → OTM check silently skipped."""
    trade = make_trade(delta=None, premium=300.0, signal_strength=1.0)
    result = await detector.detect(trade)
    assert result is None


# ---------------------------------------------------------------------------
# detect(): multiple reasons + top_reason priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_multiple_reasons(detector):
    """Trade qualifying on >1 condition produces all reasons."""
    # PREMIUM_SIZE: premium=600 >= 500
    # SIGNAL_STRENGTH: signal_strength=6.0 >= 5.0
    trade = make_trade(premium=600.0, signal_strength=6.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert UnusualReason.PREMIUM_SIZE in result.reasons
    assert UnusualReason.SIGNAL_STRENGTH in result.reasons


@pytest.mark.asyncio
async def test_detect_top_reason_priority(detector):
    """PREMIUM_SIZE outranks OI_RATIO; OI_RATIO outranks SIGNAL_STRENGTH."""
    tick = make_tick(open_interest=100)
    # PREMIUM_SIZE: 600 >= 500, OI_RATIO: 60/100=0.60 >= 0.50, SIGNAL_STRENGTH: 6.0 >= 5.0
    trade = make_trade(
        tick=tick, premium=600.0, volume_delta=60, signal_strength=6.0, delta=0.45
    )
    result = await detector.detect(trade)
    assert result is not None
    assert result.top_reason == UnusualReason.PREMIUM_SIZE

    # Now remove PREMIUM_SIZE — OI_RATIO should be top
    trade2 = make_trade(tick=tick, premium=150.0, volume_delta=60, signal_strength=6.0, delta=0.45)
    result2 = await detector.detect(trade2)
    assert result2 is not None
    assert result2.top_reason == UnusualReason.OI_RATIO


# ---------------------------------------------------------------------------
# detect(): signal fields on result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_signal_has_correct_fields(detector):
    """UnusualSignal contains correct identity and trade fields."""
    trade = make_trade(premium=600.0, signal_strength=1.0, delta=0.45)
    result = await detector.detect(trade)
    assert result is not None
    assert result.symbol == "SPY"
    assert result.con_id == 12345
    assert result.premium == pytest.approx(600.0)
    assert result.trade is trade  # same object reference


@pytest.mark.asyncio
async def test_detect_flagged_at_is_set(detector):
    """flagged_at is a timezone-aware datetime set during detect()."""
    trade = make_trade(premium=600.0)
    result = await detector.detect(trade)
    assert result is not None
    assert result.flagged_at.tzinfo is not None


# ---------------------------------------------------------------------------
# purge_stale
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_stale_evicts_old_entries(detector):
    """purge_stale() evicts con_ids not seen within max_age_seconds."""
    old_tick = make_tick(con_id=111, open_interest=500,
                         timestamp=datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc))
    trade = make_trade(
        tick=old_tick, con_id=111, premium=600.0,
        timestamp=datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    await detector.detect(trade)
    assert 111 in detector._oi_cache

    purged = detector.purge_stale(max_age_seconds=1.0)
    assert purged == 1
    assert 111 not in detector._oi_cache


@pytest.mark.asyncio
async def test_purge_stale_keeps_recent_entries(detector):
    """purge_stale() does not evict recently-seen con_ids."""
    trade = make_trade(premium=600.0)
    await detector.detect(trade)
    purged = detector.purge_stale(max_age_seconds=86400.0)
    assert purged == 0
    assert 12345 in detector._oi_cache
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_unusual_detector.py -k "detect or purge" -v
```

Expected: FAIL — `cannot import name 'UnusualDetector'`.

**Step 3: Implement `UnusualDetector` in `src/analysis/unusual_detector.py`**

Add after the `UnusualSignal` class:

```python
# ---------------------------------------------------------------------------
# UnusualDetector
# ---------------------------------------------------------------------------

_PRIORITY = [
    UnusualReason.PREMIUM_SIZE,
    UnusualReason.OI_RATIO,
    UnusualReason.SIGNAL_STRENGTH,
    UnusualReason.OTM_PREMIUM,
]


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

        MULTI_LEG trades are skipped — premium and delta semantics differ
        for multi-leg strategies. Revisit when MULTI_LEG detection is built.

        Args:
            trade: ClassifiedTrade from FlowClassifier.classify().

        Returns:
            UnusualSignal if one or more conditions fired, else None.
        """
        s = self._settings
        con_id = trade.con_id

        # Skip MULTI_LEG — detection not yet implemented
        if trade.trade_type == TradeType.MULTI_LEG:
            return None

        # Update OI cache and last-seen timestamp
        self._last_seen[con_id] = datetime.now(timezone.utc)
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
```

Also add to the imports at the top of the file:

```python
from datetime import datetime, timedelta, timezone
```

**Step 4: Run all detector tests**

```bash
pytest tests/test_unusual_detector.py -v
```

Expected: All tests PASSED.

**Step 5: Run full suite**

```bash
pytest tests/ -m "not integration" --tb=short
```

Expected: all non-integration tests pass.

**Step 6: Commit**

```bash
git add src/analysis/unusual_detector.py tests/test_unusual_detector.py
git commit -m "feat: implement UnusualDetector with detect() and purge_stale()"
```

---

## Task 4: Add UnusualSignalRecord ORM Model to Storage

**Files:**
- Modify: `src/storage/models.py`
- Test: `tests/test_storage.py`

**Step 1: Add the failing test to `tests/test_storage.py`**

```python
@pytest.mark.asyncio
async def test_unusual_signal_record_insert(async_db_session):
    """UnusualSignalRecord inserts and reads back correctly."""
    import json
    from datetime import datetime
    from src.storage.models import UnusualSignalRecord

    record = UnusualSignalRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20260320",
        strike=500.0,
        right="C",
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.20,
        effective_price=2.45,
        trade_type="block",
        aggressor="buy",
        premium=600.0,
        volume_delta=60,
        signal_strength=1.0,
        top_reason="premium_size",
        reasons=json.dumps(["premium_size"]),
        classified_at=datetime(2026, 3, 8, 14, 30, 0),
        flagged_at=datetime(2026, 3, 8, 14, 30, 1),
    )
    async_db_session.add(record)
    await async_db_session.flush()
    assert record.id is not None
    assert record.top_reason == "premium_size"
    assert json.loads(record.reasons) == ["premium_size"]
```

**Step 2: Run to verify it fails**

```bash
pytest tests/test_storage.py::test_unusual_signal_record_insert -v
```

Expected: FAIL — `cannot import name 'UnusualSignalRecord'`.

**Step 3: Add `UnusualSignalRecord` to `src/storage/models.py`**

Add after the `ClassifiedTradeRecord` class:

```python
class UnusualSignalRecord(Base):
    """One row per UnusualSignal emitted by UnusualDetector.

    Persisted by the orchestration layer via insert_unusual_signal().
    trade_type, aggressor, top_reason stored as plain strings (enum values)
    for SQLite compatibility.
    reasons stored as a JSON array string, e.g. '["premium_size","oi_ratio"]'.

    classified_at = trade.timestamp (when the originating trade occurred).
    flagged_at = when UnusualDetector.detect() was called.
    No FK to classified_trades — consistent with project pattern (avoids
    persistence ordering constraint; join on (con_id, classified_at) instead).
    """

    __tablename__ = "unusual_signals"
    __table_args__ = (
        Index("ix_unusual_signals_symbol_flagged_at", "symbol", "flagged_at"),
        Index("ix_unusual_signals_con_id_flagged_at", "con_id", "flagged_at"),
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
    effective_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    trade_type: Mapped[str] = mapped_column(String, nullable=False)     # TradeType.value
    aggressor: Mapped[str] = mapped_column(String, nullable=False)       # Aggressor.value
    premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)

    top_reason: Mapped[str] = mapped_column(String, nullable=False)     # UnusualReason.value
    reasons: Mapped[str] = mapped_column(String, nullable=False)        # JSON array
    classified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    flagged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

**Step 4: Run the test**

```bash
pytest tests/test_storage.py::test_unusual_signal_record_insert -v
```

Expected: PASS.

**Step 5: Run all storage tests**

```bash
pytest tests/test_storage.py -v
```

Expected: all PASSED.

**Step 6: Commit**

```bash
git add src/storage/models.py tests/test_storage.py
git commit -m "feat: add UnusualSignalRecord ORM model to storage"
```

---

## Task 5: Add insert_unusual_signal Query

**Files:**
- Modify: `src/storage/queries.py`
- Modify: `src/storage/__init__.py`
- Test: `tests/test_storage.py`

**Step 1: Add the failing tests to `tests/test_storage.py`**

```python
@pytest.mark.asyncio
async def test_insert_unusual_signal_returns_id(async_db_session):
    """insert_unusual_signal returns a positive integer PK."""
    import json
    from datetime import datetime, timezone
    from src.storage import insert_unusual_signal
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.20,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.9, effective_price=2.45, last_size=600,
        premium=600.0, signal_strength=1.0, volume_delta=60,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    signal = UnusualSignal(
        symbol=trade.symbol, con_id=trade.con_id, expiry=trade.expiry,
        right=trade.right, strike=trade.strike, trade_type=trade.trade_type,
        aggressor=trade.aggressor, premium=trade.premium,
        volume_delta=trade.volume_delta, signal_strength=trade.signal_strength,
        delta=trade.delta, underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol, effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime(2026, 3, 8, 14, 30, 1, tzinfo=timezone.utc),
        trade=trade,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)
    assert isinstance(signal_id, int)
    assert signal_id > 0


@pytest.mark.asyncio
async def test_insert_unusual_signal_persists_fields(async_db_session):
    """Persisted UnusualSignalRecord matches the source UnusualSignal."""
    import json
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_unusual_signal
    from src.storage.models import UnusualSignalRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=datetime(2026, 3, 8, 14, 30, 0, tzinfo=timezone.utc),
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.20,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.9, effective_price=2.45, last_size=600,
        premium=600.0, signal_strength=1.0, volume_delta=60,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    signal = UnusualSignal(
        symbol=trade.symbol, con_id=trade.con_id, expiry=trade.expiry,
        right=trade.right, strike=trade.strike, trade_type=trade.trade_type,
        aggressor=trade.aggressor, premium=trade.premium,
        volume_delta=trade.volume_delta, signal_strength=trade.signal_strength,
        delta=trade.delta, underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol, effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE, UnusualReason.OI_RATIO],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime(2026, 3, 8, 14, 30, 1, tzinfo=timezone.utc),
        trade=trade,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)

    result = await async_db_session.execute(
        select(UnusualSignalRecord).where(UnusualSignalRecord.id == signal_id)
    )
    record = result.scalar_one()

    assert record.symbol == "SPY"
    assert record.con_id == 12345
    assert record.trade_type == "block"
    assert record.aggressor == "buy"
    assert record.top_reason == "premium_size"
    assert json.loads(record.reasons) == ["premium_size", "oi_ratio"]
    assert record.premium == pytest.approx(600.0)
    assert record.volume_delta == 60
    assert record.classified_at == datetime(2026, 3, 8, 14, 30, 0)
    assert record.flagged_at == datetime(2026, 3, 8, 14, 30, 1)
```

**Step 2: Run to verify they fail**

```bash
pytest tests/test_storage.py::test_insert_unusual_signal_returns_id tests/test_storage.py::test_insert_unusual_signal_persists_fields -v
```

Expected: FAIL — `cannot import name 'insert_unusual_signal'`.

**Step 3: Add `insert_unusual_signal` to `src/storage/queries.py`**

Add the import at the top alongside the existing analysis import:

```python
from src.analysis.unusual_detector import UnusualSignal
from src.storage.models import ChainSnapshot, ClassifiedTradeRecord, OptionContractRecord, OptionTick, UnusualSignalRecord
```

Add the function after `insert_classified_trade`:

```python
async def insert_unusual_signal(
    session: AsyncSession, signal: UnusualSignal
) -> int:
    """Persist an UnusualSignal emitted by UnusualDetector.

    reasons is stored as a JSON array string for SQLite compatibility.
    classified_at and flagged_at are stored as naive UTC (tzinfo stripped)
    for SQLite compatibility — revisit when migrating to PostgreSQL.

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        signal: The UnusualSignal returned by UnusualDetector.detect().

    Returns:
        The auto-generated primary key of the new unusual_signals row.
    """
    import json

    record = UnusualSignalRecord(
        con_id=signal.con_id,
        symbol=signal.symbol,
        expiry=signal.expiry,
        strike=signal.strike,
        right=signal.right,
        underlying_price=signal.underlying_price,
        implied_vol=signal.implied_vol,
        delta=signal.delta,
        effective_price=signal.effective_price,
        trade_type=signal.trade_type.value,
        aggressor=signal.aggressor.value,
        premium=signal.premium,
        volume_delta=signal.volume_delta,
        signal_strength=signal.signal_strength,
        top_reason=signal.top_reason.value,
        reasons=json.dumps([r.value for r in signal.reasons]),
        classified_at=signal.trade.timestamp.replace(tzinfo=None),
        flagged_at=signal.flagged_at.replace(tzinfo=None),
    )
    session.add(record)
    await session.flush()
    return record.id
```

**Step 4: Export from `src/storage/__init__.py`**

```python
from src.storage.db import get_session, init_db
from src.storage.models import (
    Base,
    ChainSnapshot,
    ClassifiedTradeRecord,
    OptionContractRecord,
    OptionTick,
    UnusualSignalRecord,
)
from src.storage.queries import (
    get_latest_snapshot,
    get_recent_ticks,
    insert_chain_snapshot,
    insert_classified_trade,
    insert_tick,
    insert_unusual_signal,
)

__all__ = [
    "Base",
    "ChainSnapshot",
    "ClassifiedTradeRecord",
    "OptionContractRecord",
    "OptionTick",
    "UnusualSignalRecord",
    "get_session",
    "init_db",
    "insert_chain_snapshot",
    "insert_classified_trade",
    "insert_tick",
    "insert_unusual_signal",
    "get_latest_snapshot",
    "get_recent_ticks",
]
```

**Step 5: Run all storage tests**

```bash
pytest tests/test_storage.py -v
```

Expected: all PASSED.

**Step 6: Run full suite**

```bash
pytest tests/ -m "not integration" --tb=short
```

Expected: all non-integration tests pass.

**Step 7: Commit**

```bash
git add src/storage/queries.py src/storage/__init__.py tests/test_storage.py
git commit -m "feat: add insert_unusual_signal query and storage export"
```

---

## Task 6: Smoke Test and Memory Update

**Files:**
- Modify: `src/analysis/unusual_detector.py`
- Modify: `C:\Users\kenny\.claude\projects\C--Coding-Projects-options-flow-analysis\memory\MEMORY.md`

**Step 1: Add `__main__` block to `src/analysis/unusual_detector.py`**

```python
if __name__ == "__main__":
    import asyncio
    from datetime import datetime, timezone
    from config.settings import Settings
    from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, FlowClassifier, TradeType
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
```

**Step 2: Run it**

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m src.analysis.unusual_detector
```

Expected output: 3 log lines. Tick 3 should show `type=sweep | signal=FLAGGED`. At minimum tick 1 should fire on `OI_RATIO` (volume_delta=100 / OI=200 = 0.50 >= threshold).

**Step 3: Update memory**

Edit `C:\Users\kenny\.claude\projects\C--Coding-Projects-options-flow-analysis\memory\MEMORY.md`:

```markdown
- Step 7: src/analysis/unusual_detector.py — DONE
```

Add to Key Patterns:
```markdown
- UnusualDetector: async detect(trade) → UnusualSignal | None; stateless except _oi_cache
- UnusualSignal: trade field excluded from serialization (Field(exclude=True)); reasons stored as JSON in DB
- insert_unusual_signal: maps signal.trade.timestamp → classified_at (naive UTC); flagged_at is when detect() ran
- OI cache: _oi_cache dict[int, int] + _last_seen dict[int, datetime]; purge_stale() evicts by age
- Condition priority: PREMIUM_SIZE > OI_RATIO > SIGNAL_STRENGTH > OTM_PREMIUM
- unusual_volume_multiplier setting intentionally unused — deferred to smart_money.py (step 10)
```

Update Next Step:
```markdown
## Next Step
Step 8: src/analysis/greeks_engine.py
```

**Step 4: Commit**

```bash
git add src/analysis/unusual_detector.py
git commit -m "feat: add unusual_detector smoke test entry point"
```

---

## Done
