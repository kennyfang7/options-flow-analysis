# Smart Money Detector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/analysis/smart_money.py` — a heuristic engine that scores `EnrichedTrade` objects for institutional (smart money) characteristics, emitting a `SmartMoneySignal` with a confidence score and reason codes.

**Architecture:** `SmartMoneyDetector.score(trade: EnrichedTrade) → SmartMoneySignal | None` applies five independent threshold checks (SWEEP_AGGRESSOR, BIG_OTM_BET, NEAR_EXPIRY_OTM, UNUSUAL_VOLUME, LARGE_BLOCK), sums per-reason confidence weights (capped at 1.0), and returns a signal only when the total confidence meets a configurable minimum. The module is stateless — no cache needed — so `purge_stale()` is a no-op included only for orchestration-layer interface consistency. Pipeline position: FlowClassifier → GreeksEngine → SmartMoneyDetector (consumes `EnrichedTrade`).

**Tech Stack:** Python 3.11+, pydantic v2 (`BaseModel`), loguru, existing project types (`EnrichedTrade`, `Moneyness`, `TradeType`, `Aggressor`, `Settings`).

---

## Context for the Implementer

### Key project conventions (read before writing a single line)
- `from __future__ import annotations` at the top of every file.
- All imports at module level — **never** inside `TYPE_CHECKING` blocks unless the type is only used in annotations.
- `float | None` checks: always `is not None`, never truthiness (`or 0.0`) — `0.0` is a valid value.
- Google-style docstrings on all public classes and methods.
- `loguru` for logging (`from loguru import logger`).
- `Field(exclude=True)` to keep heavyweight objects out of serialization (same as `ClassifiedTrade.tick` and `UnusualSignal.trade`).

### Relevant existing files (read these before writing)
- `config/settings.py` — add two new settings after the Sentiment Aggregator block, before Alert Endpoints.
- `src/analysis/greeks_engine.py` — defines `EnrichedTrade`, `Moneyness`.
- `src/analysis/flow_classifier.py` — defines `TradeType`, `Aggressor`, `ClassifiedTrade`.
- `src/analysis/unusual_detector.py` — reference implementation for output-model + detector pattern.
- `tests/test_sentiment.py` — reference for `make_trade()` / `make_aggregator()` helpers.

### Five heuristic checks (what counts as "smart money")
| Reason | Condition |
|---|---|
| `SWEEP_AGGRESSOR` | `trade_type == SWEEP` AND `aggressor != NEUTRAL` |
| `BIG_OTM_BET` | `moneyness == OTM` AND `aggressor == BUY` AND `premium >= otm_premium_threshold` |
| `NEAR_EXPIRY_OTM` | `days_to_expiry <= near_expiry_days` AND `moneyness == OTM` AND `aggressor == BUY` |
| `UNUSUAL_VOLUME` | `volume_delta >= unusual_volume_multiplier * min_block_size` |
| `LARGE_BLOCK` | `trade_type == BLOCK` AND `premium >= unusual_premium_threshold` |

### Confidence weights (module-level constant `_CONFIDENCE_WEIGHTS`)
```python
_CONFIDENCE_WEIGHTS: dict[SmartMoneyReason, float] = {
    SmartMoneyReason.SWEEP_AGGRESSOR:  0.40,
    SmartMoneyReason.BIG_OTM_BET:      0.45,
    SmartMoneyReason.NEAR_EXPIRY_OTM:  0.35,
    SmartMoneyReason.UNUSUAL_VOLUME:   0.35,
    SmartMoneyReason.LARGE_BLOCK:      0.30,
}
```

### Priority order for `top_reason` (highest priority first)
```python
_PRIORITY = [
    SmartMoneyReason.SWEEP_AGGRESSOR,
    SmartMoneyReason.BIG_OTM_BET,
    SmartMoneyReason.NEAR_EXPIRY_OTM,
    SmartMoneyReason.UNUSUAL_VOLUME,
    SmartMoneyReason.LARGE_BLOCK,
]
```

### None-check rules specific to score()
- `trade.premium is not None` — required for BIG_OTM_BET and LARGE_BLOCK checks (premium can be None).
- `trade.moneyness == Moneyness.UNKNOWN` — neither BIG_OTM_BET nor NEAR_EXPIRY_OTM fire (UNKNOWN != OTM).
- `trade.days_to_expiry` is always an `int` (from EnrichedTrade) — never None, safe to compare directly.

---

## Task 1: SmartMoneyReason enum + SmartMoneySignal model + Settings fields

**Files:**
- Modify: `config/settings.py` (add two fields after the Sentiment Aggregator block)
- Create: `src/analysis/smart_money.py` (enum + model only — no detector yet)
- Create: `tests/test_smart_money.py` (3 construction tests)

---

### Step 1: Write the failing tests

Create `tests/test_smart_money.py`:

```python
from __future__ import annotations
from datetime import datetime, timezone

import pytest


def make_signal(**kwargs):
    """Build a minimal SmartMoneySignal for construction tests."""
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.analysis.greeks_engine import Moneyness
    from tests.test_smart_money import _make_trade  # defined in Step 2 of Task 2

    trade = _make_trade()
    defaults = dict(
        symbol="SPY",
        con_id=99001,
        expiry="20260620",
        right="C",
        strike=520.0,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        premium=300_000.0,
        volume_delta=100,
        delta=0.25,
        days_to_expiry=5,
        moneyness=Moneyness.OTM,
        implied_vol=0.30,
        iv_source="ibkr",
        underlying_price=500.0,
        reasons=[SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.BIG_OTM_BET,
        confidence=0.45,
        detected_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade=trade,
    )
    defaults.update(kwargs)
    return SmartMoneySignal(**defaults)


def test_smart_money_signal_construction():
    sig = make_signal()
    assert sig.symbol == "SPY"
    assert sig.confidence == pytest.approx(0.45)
    assert sig.top_reason.value == "big_otm_bet"


def test_smart_money_signal_model_dump_excludes_trade():
    sig = make_signal()
    data = sig.model_dump()
    assert "trade" not in data
    assert "symbol" in data


def test_smart_money_signal_reasons_list():
    from src.analysis.smart_money import SmartMoneyReason
    sig = make_signal(
        reasons=[SmartMoneyReason.SWEEP_AGGRESSOR, SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.SWEEP_AGGRESSOR,
        confidence=0.85,
    )
    assert len(sig.reasons) == 2
    assert sig.top_reason.value == "sweep_aggressor"
```

**Note:** `make_signal()` calls `_make_trade()` which doesn't exist yet — that's intentional. The test will fail with `ImportError`. You'll add `_make_trade()` in Task 2 Step 1. For now this just proves the model can't be constructed yet.

Actually, simplify: write the 3 tests WITHOUT `make_signal()` calling `_make_trade()`. Instead, build the trade inline using the same pattern as `test_sentiment.py`'s `make_trade()`. Here is the corrected version — write these exact tests:

```python
from __future__ import annotations
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (expanded in Task 2)
# ---------------------------------------------------------------------------

def _make_trade(
    trade_type_str="block",
    aggressor_str="buy",
    moneyness_str="otm",
    days_to_expiry=90,
    premium=10_000.0,
    volume_delta=100,
    delta=0.25,
    implied_vol=0.30,
    underlying_price=500.0,
    symbol="SPY",
    right="C",
):
    """Build a minimal EnrichedTrade for SmartMoneyDetector tests."""
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.data.tick_stream import TickUpdate

    trade_type_map = {
        "sweep": TradeType.SWEEP, "split": TradeType.SPLIT,
        "block": TradeType.BLOCK, "unknown": TradeType.UNKNOWN,
    }
    aggressor_map = {
        "buy": Aggressor.BUY, "sell": Aggressor.SELL, "neutral": Aggressor.NEUTRAL,
    }
    moneyness_map = {
        "itm": Moneyness.ITM, "atm": Moneyness.ATM,
        "otm": Moneyness.OTM, "unknown": Moneyness.UNKNOWN,
    }

    ts = datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc)
    tick = TickUpdate(
        symbol=symbol, con_id=99001, expiry="20260620", strike=520.0, right=right,
        timestamp=ts, bid=1.0, ask=1.5, last=1.45,
        volume=volume_delta, open_interest=1000, last_size=volume_delta,
        underlying_price=underlying_price, implied_vol=implied_vol, delta=delta,
    )
    return EnrichedTrade(
        symbol=symbol,
        con_id=99001,
        expiry="20260620",
        right=right,
        strike=520.0,
        underlying_price=underlying_price,
        implied_vol=implied_vol,
        delta=delta,
        trade_type=trade_type_map[trade_type_str],
        aggressor=aggressor_map[aggressor_str],
        spread_position=0.8 if aggressor_str == "buy" else 0.2,
        effective_price=premium / max(volume_delta * 100, 1) if premium is not None else None,
        last_size=volume_delta,
        premium=premium,
        signal_strength=3.0,
        volume_delta=volume_delta,
        window_ticks=3 if trade_type_str == "sweep" else 1,
        timestamp=ts,
        tick=tick,
        gamma=0.005,
        theta=None,
        vega=None,
        days_to_expiry=days_to_expiry,
        moneyness=moneyness_map[moneyness_str],
        iv_source="ibkr",
    )


def _make_detector(**setting_overrides):
    from src.analysis.smart_money import SmartMoneyDetector
    from config.settings import Settings
    s = Settings(
        min_premium=100.0,
        min_block_size=500,
        unusual_volume_multiplier=3.0,
        unusual_premium_threshold=250_000.0,
        otm_premium_threshold=100_000.0,
        near_expiry_days=7,
        smart_money_min_confidence=0.30,
        **setting_overrides,
    )
    return SmartMoneyDetector(s)


# ---------------------------------------------------------------------------
# Task 1: Model construction tests
# ---------------------------------------------------------------------------

def test_smart_money_signal_construction():
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.analysis.greeks_engine import Moneyness

    trade = _make_trade()
    sig = SmartMoneySignal(
        symbol="SPY",
        con_id=99001,
        expiry="20260620",
        right="C",
        strike=520.0,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        premium=300_000.0,
        volume_delta=100,
        delta=0.25,
        days_to_expiry=5,
        moneyness=Moneyness.OTM,
        implied_vol=0.30,
        iv_source="ibkr",
        underlying_price=500.0,
        reasons=[SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.BIG_OTM_BET,
        confidence=0.45,
        detected_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade=trade,
    )
    assert sig.symbol == "SPY"
    assert sig.confidence == pytest.approx(0.45)
    assert sig.top_reason.value == "big_otm_bet"


def test_smart_money_signal_model_dump_excludes_trade():
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.analysis.greeks_engine import Moneyness

    trade = _make_trade()
    sig = SmartMoneySignal(
        symbol="SPY", con_id=99001, expiry="20260620", right="C", strike=520.0,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        premium=300_000.0, volume_delta=100, delta=0.25,
        days_to_expiry=5, moneyness=Moneyness.OTM, implied_vol=0.30,
        iv_source="ibkr", underlying_price=500.0,
        reasons=[SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.BIG_OTM_BET,
        confidence=0.45,
        detected_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade=trade,
    )
    data = sig.model_dump()
    assert "trade" not in data
    assert "symbol" in data
    assert "confidence" in data


def test_smart_money_reason_enum_values():
    from src.analysis.smart_money import SmartMoneyReason
    assert SmartMoneyReason.SWEEP_AGGRESSOR.value == "sweep_aggressor"
    assert SmartMoneyReason.BIG_OTM_BET.value == "big_otm_bet"
    assert SmartMoneyReason.NEAR_EXPIRY_OTM.value == "near_expiry_otm"
    assert SmartMoneyReason.UNUSUAL_VOLUME.value == "unusual_volume"
    assert SmartMoneyReason.LARGE_BLOCK.value == "large_block"
```

### Step 2: Run the tests to verify they fail

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m pytest tests/test_smart_money.py -v 2>&1 | head -30
```

Expected: all 3 tests fail with `ModuleNotFoundError: No module named 'src.analysis.smart_money'`

### Step 3: Add two settings fields to `config/settings.py`

Insert after the `# Sentiment Aggregator` block (after line 108, before `# Alert Endpoints`):

```python
    # Smart Money Detector
    near_expiry_days: int = Field(
        default=7,
        description="Days-to-expiry threshold for NEAR_EXPIRY_OTM smart money signal",
        ge=1,
        le=90,
    )
    smart_money_min_confidence: float = Field(
        default=0.30,
        description="Minimum confidence score [0, 1] to emit a SmartMoneySignal. 0.0 emits all signals.",
        ge=0,
        le=1.0,
    )
```

### Step 4: Create `src/analysis/smart_money.py` with enum + model

```python
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
        reasons: All SmartMoneyReason conditions that fired (≥1 guaranteed).
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
```

### Step 5: Run the tests to verify they pass

```bash
python -m pytest tests/test_smart_money.py -v
```

Expected: all 3 tests PASS.

### Step 6: Verify existing tests still pass

```bash
python -m pytest --tb=short -q
```

Expected: all existing tests pass (216 total previously).

### Step 7: Commit

```bash
git add config/settings.py src/analysis/smart_money.py tests/test_smart_money.py
git commit -m "feat: add SmartMoneyReason enum, SmartMoneySignal model, and Settings fields"
```

---

## Task 2: SmartMoneyDetector — score() + all five checks + purge_stale

**Files:**
- Modify: `src/analysis/smart_money.py` (add `SmartMoneyDetector` class)
- Modify: `tests/test_smart_money.py` (add ~20 tests)

---

### Step 1: Write the failing tests

Add these test functions to `tests/test_smart_money.py` (after the Task 1 tests):

```python
# ---------------------------------------------------------------------------
# Task 2: SmartMoneyDetector — individual checks
# ---------------------------------------------------------------------------

# --- SWEEP_AGGRESSOR ---

def test_sweep_aggressor_fires_on_sweep_buy():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.SWEEP_AGGRESSOR in sig.reasons


def test_sweep_aggressor_fires_on_sweep_sell():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="sell")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.SWEEP_AGGRESSOR in sig.reasons


def test_sweep_aggressor_skips_neutral_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # neutral sweep alone has no qualifying reasons → None
    trade = _make_trade(trade_type_str="sweep", aggressor_str="neutral")
    sig = det.score(trade)
    # SWEEP_AGGRESSOR must NOT be in reasons (may still be None overall)
    if sig is not None:
        assert SmartMoneyReason.SWEEP_AGGRESSOR not in sig.reasons


def test_sweep_aggressor_skips_non_sweep_trade_type():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="block", aggressor_str="buy")
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.SWEEP_AGGRESSOR not in sig.reasons


# --- BIG_OTM_BET ---

def test_big_otm_bet_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # premium=150_000 >= otm_premium_threshold=100_000, OTM, BUY
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.BIG_OTM_BET in sig.reasons


def test_big_otm_bet_skips_itm():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="itm", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_sell_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="sell",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_small_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # premium=10_000 < otm_premium_threshold=100_000
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="buy",
        premium=10_000.0, volume_delta=100,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_unknown_moneyness():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="unknown", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_none_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # premium=None → BIG_OTM_BET must not fire (premium is not None guard)
    trade = _make_trade(moneyness_str="otm", aggressor_str="buy", premium=None, volume_delta=100)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


# --- NEAR_EXPIRY_OTM ---

def test_near_expiry_otm_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # dte=5 <= near_expiry_days=7, OTM, BUY
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.NEAR_EXPIRY_OTM in sig.reasons


def test_near_expiry_otm_fires_at_boundary():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # dte exactly equals near_expiry_days=7 → fires (<=)
    trade = _make_trade(
        days_to_expiry=7, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.NEAR_EXPIRY_OTM in sig.reasons


def test_near_expiry_otm_skips_over_threshold():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # dte=8 > near_expiry_days=7 → does not fire
    trade = _make_trade(
        days_to_expiry=8, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_itm():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="itm", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_unknown_moneyness():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="unknown", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_sell_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # SELL aggressor on near-expiry OTM must not trigger NEAR_EXPIRY_OTM
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="otm", aggressor_str="sell",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


# --- UNUSUAL_VOLUME ---

def test_unusual_volume_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # unusual_volume_multiplier=3.0, min_block_size=500 → threshold=1500
    trade = _make_trade(volume_delta=1500, aggressor_str="neutral")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.UNUSUAL_VOLUME in sig.reasons


def test_unusual_volume_skips_below_threshold():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(volume_delta=1499, aggressor_str="neutral")
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.UNUSUAL_VOLUME not in sig.reasons


# --- LARGE_BLOCK ---

def test_large_block_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # trade_type=block, premium=300_000 >= unusual_premium_threshold=250_000
    trade = _make_trade(
        trade_type_str="block", premium=300_000.0, volume_delta=2000,
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.LARGE_BLOCK in sig.reasons


def test_large_block_skips_sweep_type():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # SWEEP with high premium does NOT trigger LARGE_BLOCK (must be BLOCK type)
    trade = _make_trade(
        trade_type_str="sweep", aggressor_str="neutral",
        premium=300_000.0, volume_delta=2000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


def test_large_block_skips_small_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # premium=10_000 < unusual_premium_threshold=250_000
    trade = _make_trade(trade_type_str="block", premium=10_000.0, volume_delta=100)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


def test_large_block_skips_none_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # premium=None → LARGE_BLOCK must not fire
    trade = _make_trade(trade_type_str="block", premium=None, volume_delta=2000)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


# --- Returns None when no reasons fire ---

def test_no_reasons_returns_none():
    det = _make_detector()
    # Small OTM BUY block — below all thresholds
    trade = _make_trade(
        trade_type_str="block",
        aggressor_str="buy",
        moneyness_str="otm",
        days_to_expiry=90,
        premium=10_000.0,
        volume_delta=100,
    )
    sig = det.score(trade)
    assert sig is None


# --- Confidence calculation ---

def test_confidence_single_reason_sweep_aggressor():
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy")
    sig = det.score(trade)
    assert sig is not None
    # SWEEP_AGGRESSOR weight = 0.40
    assert sig.confidence == pytest.approx(0.40)


def test_confidence_capped_at_one():
    det = _make_detector()
    # Trigger all 5 reasons: sweep+buy+otm+dte<=7+premium>=100k+volume>=1500+block
    # Use sweep type for SWEEP_AGGRESSOR; also large block won't trigger (sweep != block)
    # Combine: SWEEP+BUY+OTM+dte=5+premium=150k+volume=1500
    # That gives SWEEP_AGGRESSOR(0.40)+BIG_OTM_BET(0.45)+NEAR_EXPIRY_OTM(0.35)+UNUSUAL_VOLUME(0.35)=1.55→capped 1.0
    trade = _make_trade(
        trade_type_str="sweep", aggressor_str="buy", moneyness_str="otm",
        days_to_expiry=5, premium=150_000.0, volume_delta=1500,
    )
    sig = det.score(trade)
    assert sig is not None
    assert sig.confidence == pytest.approx(1.0)


def test_confidence_below_min_returns_none():
    # Set high min_confidence so LARGE_BLOCK alone (0.30) is below threshold.
    # volume_delta=200 keeps us below the UNUSUAL_VOLUME threshold (1500),
    # so only LARGE_BLOCK fires: confidence=0.30 < min_confidence=0.45 → None.
    det = _make_detector(smart_money_min_confidence=0.45)
    trade = _make_trade(
        trade_type_str="block", aggressor_str="neutral",
        premium=300_000.0, volume_delta=200,
    )
    sig = det.score(trade)
    # LARGE_BLOCK (0.30) < min_confidence (0.45) → None
    assert sig is None


# --- top_reason priority ---

def test_top_reason_sweep_beats_large_block():
    from src.analysis.smart_money import SmartMoneyReason
    # Trigger both SWEEP_AGGRESSOR and UNUSUAL_VOLUME — SWEEP_AGGRESSOR has higher priority
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy", volume_delta=1500)
    sig = det.score(trade)
    assert sig is not None
    assert sig.top_reason == SmartMoneyReason.SWEEP_AGGRESSOR


# --- purge_stale ---

def test_purge_stale_always_returns_zero():
    det = _make_detector()
    assert det.purge_stale() == 0
    assert det.purge_stale(max_age_seconds=60.0) == 0
```

### Step 2: Run the tests to confirm they fail

```bash
python -m pytest tests/test_smart_money.py -v 2>&1 | head -40
```

Expected: Task 2 tests fail (`SmartMoneyDetector not found`). Task 1 tests still pass.

### Step 3: Add `SmartMoneyDetector` to `src/analysis/smart_money.py`

Append this class after `SmartMoneySignal` (before the `if __name__ == "__main__"` block you'll add in Task 3):

```python
# ---------------------------------------------------------------------------
# SmartMoneyDetector
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

        Returns:
            Always 0.
        """
        return 0
```

### Step 4: Run the tests to verify they pass

```bash
python -m pytest tests/test_smart_money.py -v
```

Expected: all tests PASS. Count should be 3 (Task 1) + ~32 (Task 2) = ~35 tests.

### Step 5: Run the full test suite

```bash
python -m pytest --tb=short -q
```

Expected: all previously passing tests still pass, plus the new ones.

### Step 6: Commit

```bash
git add src/analysis/smart_money.py tests/test_smart_money.py
git commit -m "feat: implement SmartMoneyDetector with five heuristic checks and confidence scoring"
```

---

## Task 3: Smoke test block

**Files:**
- Modify: `src/analysis/smart_money.py` (add `if __name__ == "__main__"` block)

---

### Step 1: Add the smoke test block

Append to the bottom of `src/analysis/smart_money.py`:

```python
if __name__ == "__main__":
    from datetime import date as _date, timedelta

    from config.settings import Settings
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
    )
    classifier = FlowClassifier(settings)
    engine = GreeksEngine(settings)
    detector = SmartMoneyDetector(settings)

    future_expiry = (_date.today() + timedelta(days=90)).strftime("%Y%m%d")
    near_expiry = (_date.today() + timedelta(days=4)).strftime("%Y%m%d")
    base_time = datetime(2026, 3, 11, 14, 30, 0, tzinfo=timezone.utc)

    # Scenario definitions: (description, expiry, strike, right, bid, ask, last, volume, oi, last_size, underlying, iv, delta)
    scenarios = [
        # [1] Sweep of 3 rapid OTM call buys — SWEEP_AGGRESSOR expected
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 100, 1000, 100, 500.0, 0.30, 0.20),
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 200, 1000, 100, 500.0, 0.30, 0.20),
        ("sweep_buy_otm",   future_expiry, 560.0, "C", 1.00, 1.50, 1.48, 300, 1000, 100, 500.0, 0.30, 0.20),
        # [2] Near-expiry OTM buy — NEAR_EXPIRY_OTM expected
        ("near_expiry_otm", near_expiry,   580.0, "C", 0.50, 0.80, 0.78, 500, 800,  500, 500.0, 0.55, 0.10),
        # [3] Large block — LARGE_BLOCK expected (2500 contracts * ~1.4 * 100 = $350k+)
        ("large_block",     future_expiry, 495.0, "C", 1.40, 1.60, 1.55, 2500, 5000, 2500, 500.0, 0.25, 0.52),
        # [4] Small retail trade — should return None
        ("retail_small",    future_expiry, 510.0, "C", 0.50, 0.70, 0.65, 50,  2000,  50,  500.0, 0.22, 0.35),
    ]

    results: list[tuple[str, SmartMoneySignal | None]] = []
    for i, (label, expiry, strike, right, bid, ask, last, vol, oi, last_size, underlying, iv, delta) in enumerate(scenarios):
        tick = TickUpdate(
            symbol="SPY", con_id=90000 + i, expiry=expiry,
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
        "Smoke test complete. {} scenarios → {} flagged as smart money.",
        len(results), flagged,
    )
```

### Step 2: Run the smoke test

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m src.analysis.smart_money
```

Expected output (approximate):
```
INFO  | [sweep_buy_otm] type=unknown moneyness=otm dte=90 | smart_money=none top=- conf=-
INFO  | [sweep_buy_otm] type=unknown moneyness=otm dte=90 | smart_money=none top=- conf=-
INFO  | [sweep_buy_otm] type=sweep moneyness=otm dte=90 | smart_money=FLAGGED top=sweep_aggressor conf=0.40
INFO  | [near_expiry_otm] type=block moneyness=otm dte=4 | smart_money=FLAGGED top=near_expiry_otm conf=0.35
INFO  | [large_block] type=block ... | smart_money=FLAGGED top=large_block conf=0.30
INFO  | [retail_small] → trade below min_premium threshold
INFO  | purge_stale evicted 0 (always 0 — stateless)
SUCCESS | Smoke test complete. 6 scenarios → 3 flagged as smart money.
```

(Exact counts depend on classifier window behavior for the sweep — first two sweep ticks may not be flagged since the sweep window hasn't filled yet.)

### Step 3: Run the full test suite one final time

```bash
python -m pytest --tb=short -q
```

Expected: all tests pass (≥ 251 total: 216 existing + ~35 new).

### Step 4: Commit

```bash
git add src/analysis/smart_money.py
git commit -m "feat: add SmartMoneyDetector smoke test block"
```

---

## Done

After all tasks complete, the following are delivered:
- `config/settings.py` — two new fields: `near_expiry_days`, `smart_money_min_confidence`
- `src/analysis/smart_money.py` — `SmartMoneyReason` enum, `SmartMoneySignal` model, `SmartMoneyDetector` class, smoke test block
- `tests/test_smart_money.py` — ~35 tests covering model construction, all five heuristic checks (including `premium=None` guards for BIG_OTM_BET/LARGE_BLOCK, UNKNOWN moneyness, all aggressor boundaries for NEAR_EXPIRY_OTM), confidence calculation, min-confidence gate, priority ordering, and `purge_stale`
