# Alerts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/alerts/rules.py` and `src/alerts/notifier.py` — a two-class alerting layer that converts `UnusualSignal` and `SmartMoneySignal` objects into formatted `Alert` messages and delivers them to Discord via webhook.

**Architecture:** `AlertRules` maps detector signals to `Alert` objects (with severity level and formatted title/body). `Notifier` delivers `Alert` objects to configured endpoints. Discord delivery uses `requests.post()` (already a project dependency) run in a thread via `asyncio.to_thread()` to avoid blocking the event loop. Email is stubbed — logs "not implemented" and returns. Both classes are stateless and depend only on `Settings`.

**Tech Stack:** Python 3.11+, pydantic v2 (`BaseModel`), `requests` (already in requirements.txt), `loguru`, `asyncio.to_thread`, existing project types (`UnusualSignal`, `SmartMoneySignal`, `Settings`).

---

## Context for the Implementer

### Key project conventions (read before writing a single line)
- `from __future__ import annotations` at the top of every file.
- All imports at module level — **never** inside functions, unless inside an `if __name__ == "__main__"` block.
- `float | None` checks: always `is not None`, never truthiness (`or 0.0`) — `0.0` is a valid value.
- Google-style docstrings on all public classes and methods.
- `loguru` for logging — `from loguru import logger`. Use `{}` placeholders, never f-strings in the format string position: `logger.info("x={}", x)` ✓, `logger.info(f"x={x}")` ✗.
- Tests: all imports inside helper functions (established pattern from `test_sentiment.py`, `test_smart_money.py`).
- Test timestamps: always `datetime.now(timezone.utc)` — never hardcoded `datetime(2026, ...)`.

### Relevant existing files (read these before writing)
- `src/analysis/unusual_detector.py` — `UnusualSignal`, `UnusualReason` (PREMIUM_SIZE, OI_RATIO, SIGNAL_STRENGTH, OTM_PREMIUM)
- `src/analysis/smart_money.py` — `SmartMoneySignal`, `SmartMoneyReason`
- `src/analysis/flow_classifier.py` — `ClassifiedTrade`, `TradeType`, `Aggressor`
- `src/analysis/greeks_engine.py` — `EnrichedTrade`, `Moneyness`
- `src/data/tick_stream.py` — `TickUpdate`
- `config/settings.py` — `discord_webhook_url: str`, `alert_email: str`
- `tests/test_smart_money.py` — reference for test helper patterns and deferred imports

### ClassifiedTrade fields (needed for building test helpers)
```python
symbol: str, con_id: int, expiry: str, right: str, strike: float
underlying_price: float | None, implied_vol: float | None, delta: float | None
trade_type: TradeType, aggressor: Aggressor
spread_position: float | None, effective_price: float | None, last_size: int | None
premium: float | None, signal_strength: float | None
volume_delta: int, window_ticks: int, timestamp: datetime
tick: TickUpdate = Field(exclude=True)
```

### TickUpdate fields (needed for building test helpers)
```python
symbol: str, con_id: int, expiry: str, strike: float, right: str
timestamp: datetime, bid: float | None, ask: float | None, last: float | None
volume: int | None, open_interest: int | None, last_size: int | None
underlying_price: float | None, implied_vol: float | None, delta: float | None, gamma: float | None
```

### EnrichedTrade fields beyond ClassifiedTrade
```python
gamma: float | None, theta: float | None, vega: float | None
days_to_expiry: int, moneyness: Moneyness, iv_source: str
```

---

## Task 1: AlertLevel enum + Alert model + AlertRules

**Files:**
- Modify: `src/alerts/rules.py` (currently empty)
- Create: `tests/test_alerts.py`

---

### Step 1: Write the failing tests

Create `tests/test_alerts.py` with the following content:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import requests as req_lib


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_tick():
    from src.data.tick_stream import TickUpdate
    return TickUpdate(
        symbol="SPY", con_id=12345, expiry="20261219", strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=2.0, ask=2.5, last=2.45,
        volume=100, open_interest=1000, last_size=50,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
    )


def _make_classified_trade(tick=None, **overrides):
    from src.analysis.flow_classifier import ClassifiedTrade, TradeType, Aggressor
    tick = tick or _make_tick()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.8, effective_price=2.45, last_size=500,
        premium=300_000.0, signal_strength=8.0,
        volume_delta=500, window_ticks=1, timestamp=ts, tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


def _make_unusual_signal(**overrides):
    from src.analysis.unusual_detector import UnusualSignal, UnusualReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    trade = _make_classified_trade()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        premium=300_000.0, volume_delta=500, signal_strength=8.0,
        delta=0.45, underlying_price=500.0, implied_vol=0.25,
        effective_price=2.45,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=ts,
        trade=trade,
    )
    defaults.update(overrides)
    return UnusualSignal(**defaults)


def _make_enriched_trade(**overrides):
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import TradeType, Aggressor
    tick = _make_tick()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
        trade_type=TradeType.SWEEP, aggressor=Aggressor.BUY,
        spread_position=0.8, effective_price=2.45, last_size=1500,
        premium=367_500.0, signal_strength=6.0,
        volume_delta=1500, window_ticks=3, timestamp=ts, tick=tick,
        gamma=0.01, theta=-0.05, vega=0.15,
        days_to_expiry=90, moneyness=Moneyness.OTM, iv_source="ibkr",
    )
    defaults.update(overrides)
    return EnrichedTrade(**defaults)


def _make_smart_money_signal(**overrides):
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    etrade = _make_enriched_trade(symbol=overrides.get("symbol", "SPY"))
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        trade_type=TradeType.SWEEP, aggressor=Aggressor.BUY,
        premium=367_500.0, volume_delta=1500,
        delta=0.45, days_to_expiry=90,
        moneyness=etrade.moneyness,
        implied_vol=0.25, iv_source="ibkr", underlying_price=500.0,
        reasons=[SmartMoneyReason.SWEEP_AGGRESSOR, SmartMoneyReason.UNUSUAL_VOLUME],
        top_reason=SmartMoneyReason.SWEEP_AGGRESSOR,
        confidence=0.75,
        detected_at=ts,
        trade=etrade,
    )
    defaults.update(overrides)
    return SmartMoneySignal(**defaults)


def _make_rules(**setting_overrides):
    from src.alerts.rules import AlertRules
    from config.settings import Settings
    base = dict(min_premium=100.0, unusual_premium_threshold=250_000.0)
    base.update(setting_overrides)
    return AlertRules(Settings(**base))


def _make_notifier(discord_webhook_url="", alert_email=""):
    from src.alerts.notifier import Notifier
    from config.settings import Settings
    return Notifier(Settings(
        min_premium=100.0,
        unusual_premium_threshold=250_000.0,
        discord_webhook_url=discord_webhook_url,
        alert_email=alert_email,
    ))


# ---------------------------------------------------------------------------
# AlertLevel
# ---------------------------------------------------------------------------

def test_alert_level_values():
    from src.alerts.rules import AlertLevel
    assert AlertLevel.LOW.value == "low"
    assert AlertLevel.MEDIUM.value == "medium"
    assert AlertLevel.HIGH.value == "high"


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

def test_alert_construction():
    from src.alerts.rules import Alert, AlertLevel
    alert = Alert(
        symbol="SPY",
        level=AlertLevel.HIGH,
        title="SPY UNUSUAL HIGH",
        body="BLOCK BUY | 500 contracts | $300,000",
        source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY", "premium": 300_000.0},
    )
    assert alert.symbol == "SPY"
    assert alert.level == AlertLevel.HIGH
    assert alert.source == "unusual"


def test_alert_metadata_is_json_serializable():
    from src.alerts.rules import Alert, AlertLevel
    alert = Alert(
        symbol="SPY", level=AlertLevel.MEDIUM,
        title="T", body="B", source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY", "premium": 300_000.0, "top_reason": "premium_size"},
    )
    # Must not raise
    serialized = json.dumps(alert.metadata)
    data = json.loads(serialized)
    assert data["symbol"] == "SPY"


# ---------------------------------------------------------------------------
# AlertRules.evaluate_unusual — level determination
# ---------------------------------------------------------------------------

def test_evaluate_unusual_premium_size_is_high():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.PREMIUM_SIZE,
        reasons=[UnusualReason.PREMIUM_SIZE],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_unusual_oi_ratio_is_medium():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.OI_RATIO,
        reasons=[UnusualReason.OI_RATIO],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_unusual_otm_premium_is_medium():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.OTM_PREMIUM,
        reasons=[UnusualReason.OTM_PREMIUM],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_unusual_signal_strength_is_low():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.SIGNAL_STRENGTH,
        reasons=[UnusualReason.SIGNAL_STRENGTH],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.LOW


# ---------------------------------------------------------------------------
# AlertRules.evaluate_unusual — title / body / source
# ---------------------------------------------------------------------------

def test_evaluate_unusual_title_contains_symbol_and_level():
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        symbol="AAPL",
        top_reason=UnusualReason.PREMIUM_SIZE,
        reasons=[UnusualReason.PREMIUM_SIZE],
    )
    alert = rules.evaluate_unusual(sig)
    assert "AAPL" in alert.title
    assert "HIGH" in alert.title


def test_evaluate_unusual_body_contains_premium():
    rules = _make_rules()
    sig = _make_unusual_signal(premium=300_000.0)
    alert = rules.evaluate_unusual(sig)
    assert "300,000" in alert.body or "300000" in alert.body


def test_evaluate_unusual_source_is_unusual():
    rules = _make_rules()
    sig = _make_unusual_signal()
    alert = rules.evaluate_unusual(sig)
    assert alert.source == "unusual"


def test_evaluate_unusual_metadata_has_expected_keys():
    rules = _make_rules()
    sig = _make_unusual_signal()
    alert = rules.evaluate_unusual(sig)
    for key in ("symbol", "trade_type", "aggressor", "premium", "volume_delta", "top_reason"):
        assert key in alert.metadata


# ---------------------------------------------------------------------------
# AlertRules.evaluate_smart_money — level determination
# ---------------------------------------------------------------------------

def test_evaluate_smart_money_high_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_smart_money_medium_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.55)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_smart_money_low_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.35)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.LOW


def test_evaluate_smart_money_boundary_070_is_high():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.70)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_smart_money_boundary_050_is_medium():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.50)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.MEDIUM


# ---------------------------------------------------------------------------
# AlertRules.evaluate_smart_money — title / body / source
# ---------------------------------------------------------------------------

def test_evaluate_smart_money_title_contains_symbol_and_confidence():
    rules = _make_rules()
    sig = _make_smart_money_signal(symbol="TSLA", confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert "TSLA" in alert.title
    assert "75%" in alert.title or "0.75" in alert.title


def test_evaluate_smart_money_body_contains_top_reason():
    from src.analysis.smart_money import SmartMoneyReason
    rules = _make_rules()
    sig = _make_smart_money_signal(
        top_reason=SmartMoneyReason.SWEEP_AGGRESSOR,
    )
    alert = rules.evaluate_smart_money(sig)
    assert "sweep_aggressor" in alert.body


def test_evaluate_smart_money_source_is_smart_money():
    rules = _make_rules()
    sig = _make_smart_money_signal()
    alert = rules.evaluate_smart_money(sig)
    assert alert.source == "smart_money"


def test_evaluate_smart_money_metadata_has_confidence():
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert "confidence" in alert.metadata
    assert alert.metadata["confidence"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Notifier — discord skips when url empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_skips_discord_when_url_empty():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        notifier = _make_notifier(discord_webhook_url="")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Notifier — discord posts when url is set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_posts_to_discord_when_url_set():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="SPY UNUSUAL HIGH", body="details",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_notifier_discord_payload_has_embeds():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="SPY UNUSUAL HIGH", body="details",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["title"] == "SPY UNUSUAL HIGH"


@pytest.mark.asyncio
async def test_notifier_discord_high_level_uses_red_color():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        _, kwargs = mock_post.call_args
        embed = kwargs["json"]["embeds"][0]
        assert embed["color"] == 0xFF0000  # red for HIGH


@pytest.mark.asyncio
async def test_notifier_handles_discord_non_ok_status_without_raising():
    """Non-2xx responses must be logged, not raised."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = "Bad Request"
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.MEDIUM, title="T", body="B",
            source="smart_money", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        # Must not raise
        await notifier.send(alert)


@pytest.mark.asyncio
async def test_notifier_handles_discord_request_exception_without_raising():
    """Network errors must be logged, not raised."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post", side_effect=req_lib.RequestException("timeout")):
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.LOW, title="T", body="B",
            source="smart_money", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        # Must not raise
        await notifier.send(alert)


@pytest.mark.asyncio
async def test_notifier_skips_email_when_empty():
    """No-op email must not raise and must not call requests.post."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        notifier = _make_notifier(discord_webhook_url="", alert_email="")
        alert = Alert(
            symbol="SPY", level=AlertLevel.LOW, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_not_called()
```

### Step 2: Run tests to verify they fail

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m pytest tests/test_alerts.py --tb=short -q 2>&1 | head -20
```

Expected: all tests fail with `ImportError: cannot import name 'AlertLevel' from 'src.alerts.rules'`.

### Step 3: Implement `src/alerts/rules.py`

Replace the empty file with:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from loguru import logger
from pydantic import BaseModel

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, TradeType
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
```

### Step 4: Run Task 1 tests to verify they pass

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m pytest tests/test_alerts.py -k "not notifier" --tb=short -q
```

Expected: all non-notifier tests pass (approximately 19 tests).

### Step 5: Run the full test suite to verify no regressions

```bash
python -m pytest --tb=short -q
```

Expected: all previously passing tests still pass, plus the new ones.

### Step 6: Commit

```bash
git add src/alerts/rules.py tests/test_alerts.py
git commit -m "feat: add AlertLevel, Alert model, and AlertRules with unusual/smart_money evaluation"
```

---

## Task 2: Notifier (Discord via requests)

**Files:**
- Modify: `src/alerts/notifier.py` (currently empty)

---

### Step 1: Implement `src/alerts/notifier.py`

Replace the empty file with:

```python
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import requests
from loguru import logger

from config.settings import Settings
from src.alerts.rules import Alert, AlertLevel


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMBED_COLORS: dict[AlertLevel, int] = {
    AlertLevel.HIGH:   0xFF0000,  # red
    AlertLevel.MEDIUM: 0xFF8C00,  # dark orange
    AlertLevel.LOW:    0xFFD700,  # gold
}


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class Notifier:
    """Delivers Alert objects to configured notification endpoints.

    Currently supports Discord webhooks (via HTTP POST with requests).
    Email alerting is a no-op stub — logs and returns immediately.

    Both send paths silently skip when the relevant setting is empty,
    so the class is safe to instantiate regardless of configuration.
    Delivery failures are logged at ERROR level; they do NOT raise.

    Args:
        settings: Application settings (discord_webhook_url, alert_email).

    Example:
        notifier = Notifier(settings)
        await notifier.send(alert)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, alert: Alert) -> None:
        """Deliver an alert to all configured endpoints.

        Runs the Discord POST in a thread via asyncio.to_thread to avoid
        blocking the event loop. Email is stubbed.

        Args:
            alert: Alert from AlertRules.evaluate_unusual() or
                evaluate_smart_money().
        """
        await asyncio.to_thread(self._send_discord, alert)
        self._send_email(alert)

    def _send_discord(self, alert: Alert) -> None:
        """POST alert as a Discord embed to the configured webhook URL.

        Skips silently when discord_webhook_url is empty.
        Logs ERROR on non-2xx responses or network exceptions — does not raise.

        Args:
            alert: Alert to deliver.
        """
        url = self._settings.discord_webhook_url
        if not url:
            logger.debug("notifier: discord_webhook_url not set — skipping")
            return

        payload = {
            "username": "Options Flow",
            "embeds": [
                {
                    "title": alert.title,
                    "description": alert.body,
                    "color": _EMBED_COLORS[alert.level],
                    "timestamp": alert.emitted_at.isoformat(),
                }
            ],
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                logger.info("notifier: discord sent — {}", alert.title)
            else:
                logger.error(
                    "notifier: discord HTTP {} — {}",
                    resp.status_code,
                    resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.error("notifier: discord exception — {}", exc)

    def _send_email(self, alert: Alert) -> None:
        """Email stub — not implemented.

        Logs an info message when alert_email is configured, then returns.
        Full SMTP implementation deferred to a future iteration.

        Args:
            alert: Alert to (not yet) deliver by email.
        """
        if not self._settings.alert_email:
            return
        logger.info(
            "notifier: email to {} not implemented — configure Discord for now",
            self._settings.alert_email,
        )
```

### Step 2: Run the notifier tests

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m pytest tests/test_alerts.py -k "notifier" --tb=short -q
```

Expected: all 8 notifier tests pass.

### Step 3: Run the full test suite

```bash
python -m pytest --tb=short -q
```

Expected: all previously passing tests still pass, plus the new notifier tests.

### Step 4: Commit

```bash
git add src/alerts/notifier.py
git commit -m "feat: implement Notifier with Discord webhook delivery and email stub"
```

---

## Task 3: `__init__.py` exports + smoke test block

**Files:**
- Modify: `src/alerts/__init__.py`
- Modify: `src/alerts/notifier.py` (append `if __name__ == "__main__"` block)

---

### Step 1: Update `src/alerts/__init__.py`

Replace the empty file with:

```python
from __future__ import annotations

from src.alerts.rules import Alert, AlertLevel, AlertRules
from src.alerts.notifier import Notifier

__all__ = ["Alert", "AlertLevel", "AlertRules", "Notifier"]
```

### Step 2: Append smoke test block to `src/alerts/notifier.py`

Append to the bottom of `src/alerts/notifier.py`:

```python
if __name__ == "__main__":
    from datetime import date as _date, timedelta

    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.analysis.smart_money import SmartMoneyDetector
    from src.analysis.unusual_detector import UnusualDetector
    from src.alerts.rules import AlertRules
    from src.data.tick_stream import TickUpdate

    async def _main() -> None:
        settings = Settings(
            min_premium=100.0,
            min_block_size=500,
            unusual_volume_multiplier=3.0,
            unusual_premium_threshold=250_000.0,
            otm_premium_threshold=100_000.0,
            near_expiry_days=7,
            smart_money_min_confidence=0.30,
            risk_free_rate=0.05,
            # Leave discord_webhook_url empty so smoke test doesn't fire a real webhook
            discord_webhook_url="",
        )

        classifier = FlowClassifier(settings)
        engine = GreeksEngine(settings)
        detector = UnusualDetector(settings)
        smart = SmartMoneyDetector(settings)
        rules = AlertRules(settings)
        notifier = Notifier(settings)

        future_expiry = (_date.today() + timedelta(days=60)).strftime("%Y%m%d")
        base_time = datetime.now(timezone.utc)

        # Scenario: big block buy that triggers PREMIUM_SIZE
        tick = TickUpdate(
            symbol="SPY", con_id=91000, expiry=future_expiry,
            strike=500.0, right="C",
            timestamp=base_time,
            bid=1.38, ask=1.62, last=1.60,
            volume=2000, open_interest=3000, last_size=2000,
            underlying_price=500.0, implied_vol=0.25, delta=0.45,
        )
        trade = classifier.classify(tick)
        alerts_sent = 0
        if trade:
            enriched = engine.enrich(trade)
            unusual_sig = await detector.detect(enriched)
            smart_sig = smart.score(enriched)

            if unusual_sig:
                alert = rules.evaluate_unusual(unusual_sig)
                logger.info(
                    "[unusual] {} level={} title={}",
                    unusual_sig.symbol, alert.level.value, alert.title,
                )
                await notifier.send(alert)
                alerts_sent += 1

            if smart_sig:
                alert = rules.evaluate_smart_money(smart_sig)
                logger.info(
                    "[smart_money] {} level={} conf={:.0%} title={}",
                    smart_sig.symbol, alert.level.value, smart_sig.confidence, alert.title,
                )
                await notifier.send(alert)
                alerts_sent += 1
        else:
            logger.info("trade below min_premium threshold — no alerts")

        logger.success(
            "Smoke test complete. {} alert(s) evaluated (discord skipped — no webhook configured).",
            alerts_sent,
        )

    asyncio.run(_main())
```

### Step 3: Run the smoke test

```bash
cd "C:\Coding Projects\options-flow-analysis"
python -m src.alerts.notifier
```

Expected output (approximate):
```
INFO  | [unusual] SPY level=high title=SPY UNUSUAL HIGH
INFO  | notifier: discord_webhook_url not set — skipping
INFO  | [smart_money] SPY level=high conf=65% title=SPY SMART MONEY HIGH (65%)
INFO  | notifier: discord_webhook_url not set — skipping
SUCCESS | Smoke test complete. 2 alert(s) evaluated (discord skipped — no webhook configured).
```

(Exact signal counts depend on threshold interactions — at least 1 alert is expected from the large block scenario.)

### Step 4: Run the full test suite one final time

```bash
python -m pytest --tb=short -q
```

Expected: all tests pass (≥ 273 total: 249 existing + ~24 new).

### Step 5: Commit

```bash
git add src/alerts/__init__.py src/alerts/notifier.py
git commit -m "feat: add alerts __init__ exports and notifier smoke test block"
```

---

## Done

After all tasks complete, the following are delivered:
- `config/settings.py` — no changes needed (discord_webhook_url and alert_email already present)
- `src/alerts/rules.py` — `AlertLevel` enum, `Alert` model, `AlertRules` class with evaluate_unusual/evaluate_smart_money
- `src/alerts/notifier.py` — `Notifier` class (async send → Discord webhook via requests, email stub), smoke test block
- `src/alerts/__init__.py` — exports `Alert`, `AlertLevel`, `AlertRules`, `Notifier`
- `tests/test_alerts.py` — ~24 tests covering enum values, model construction, level determination (unusual + smart_money), title/body format, metadata serialization, Discord posting behavior, error handling, empty-URL skips
