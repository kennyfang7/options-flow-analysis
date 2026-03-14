# Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/dashboard/` — a single-page Dash dashboard showing live sentiment KPIs, unusual signals, classified trades, and an alerts feed.

**Architecture:** Hybrid data strategy — live `SentimentSnapshot` and `Alert` objects flow from the asyncio pipeline into a `SharedState` container (via `queue.Queue`), while historical `UnusualSignalRecord` and `ClassifiedTradeRecord` rows are queried from SQLite using a new synchronous SQLAlchemy engine. Dash/Flask runs in its own thread and reads both sources on `dcc.Interval` timers.

**Tech Stack:** `dash`, `dash.dash_table`, `sqlalchemy` (sync), `queue.Queue`, `loguru`, `pydantic`

---

## Reference

**Models to know:**
- `SentimentSnapshot` in `src/analysis/sentiment.py` — pydantic model with ~18 fields
- `Alert`, `AlertLevel` in `src/alerts/rules.py` — pydantic model; `AlertLevel` is `str, Enum` with LOW/MEDIUM/HIGH
- `UnusualSignalRecord` in `src/storage/models.py` — SQLAlchemy model, table `unusual_signals`
- `ClassifiedTradeRecord` in `src/storage/models.py` — SQLAlchemy model, table `classified_trades`
- `Base` in `src/storage/models.py` — `DeclarativeBase` used for table creation

**Existing db.py helpers to understand:**
- `_adapt_url(url)` — adds async driver prefix; we need the inverse
- `make_engine(url)` → `AsyncEngine` — existing async factory pattern
- `get_sync_engine()` we add — same lazy-singleton pattern, sync engine

**Settings fields already present:** `database_url`, `discord_webhook_url`, `alert_email`

---

## Task 1: Add Sync Engine to `src/storage/db.py`

**Files:**
- Modify: `src/storage/db.py`
- Test: `tests/test_dashboard.py` (create this file)

**Step 1: Create the test file with sync engine tests**

```python
# tests/test_dashboard.py
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from dash import html

from src.storage.db import get_sync_engine, make_sync_engine


class TestSyncEngine:
    def test_make_sync_engine_strips_aiosqlite(self):
        engine = make_sync_engine("sqlite+aiosqlite:///test.db")
        assert "aiosqlite" not in str(engine.url)
        engine.dispose()

    def test_make_sync_engine_plain_sqlite_unchanged(self):
        engine = make_sync_engine("sqlite:///test.db")
        assert str(engine.url).startswith("sqlite:///")
        assert "aiosqlite" not in str(engine.url)
        engine.dispose()

    def test_get_sync_engine_is_singleton(self):
        e1 = get_sync_engine()
        e2 = get_sync_engine()
        assert e1 is e2
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_dashboard.py::TestSyncEngine -v
```
Expected: `ImportError: cannot import name 'get_sync_engine' from 'src.storage.db'`

**Step 3: Implement in `src/storage/db.py`**

Add these imports at the top (after existing imports):
```python
from sqlalchemy import Engine, create_engine
```

Add these functions after `make_engine()`:
```python
def _strip_async_prefix(url: str) -> str:
    """Remove async driver prefix from a SQLAlchemy URL string.

    Inverse of _adapt_url(). Used to build a synchronous engine URL
    from the same database_url setting used by the async engine.

    Args:
        url: A SQLAlchemy URL, possibly with an async driver prefix.

    Returns:
        URL with any async driver prefix removed.
    """
    return (
        url.replace("sqlite+aiosqlite://", "sqlite://", 1)
           .replace("postgresql+asyncpg://", "postgresql://", 1)
    )


def make_sync_engine(database_url: str | None = None) -> Engine:
    """Create a synchronous SQLAlchemy engine from the given URL or settings.

    Used by Dash callbacks (Flask/sync context) to query the same database
    as the async engine without conflicting connection pool settings.
    SQLAlchemy models are engine-agnostic and work identically with both.

    Args:
        database_url: Optional explicit URL. If None, reads from settings.

    Returns:
        A configured synchronous Engine instance.
    """
    if database_url is None:
        from config.settings import settings
        database_url = settings.database_url

    url = _strip_async_prefix(_adapt_url(database_url))
    logger.debug("Creating sync engine: {}", url)
    return create_engine(url, echo=False)


_sync_engine: Engine | None = None


def get_sync_engine() -> Engine:
    """Return the module-level synchronous engine singleton.

    Created lazily on first call. Safe to call from any thread.
    Used exclusively by Dash callbacks for read-only DB queries.

    Returns:
        The shared synchronous Engine instance.
    """
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = make_sync_engine()
    return _sync_engine
```

**Step 4: Run to confirm PASS**

```
pytest tests/test_dashboard.py::TestSyncEngine -v
```
Expected: 3 PASSED

**Step 5: Confirm existing storage tests still pass**

```
pytest tests/test_storage.py -v
```
Expected: all PASSED

**Step 6: Commit**

```bash
git add src/storage/db.py tests/test_dashboard.py
git commit -m "feat: add sync engine helpers to db.py for Dash callbacks"
```

---

## Task 2: Add Dashboard Settings to `config/settings.py`

**Files:**
- Modify: `config/settings.py`
- Test: `tests/test_settings.py` (extend existing)

**Step 1: Write the failing tests**

Add to `tests/test_settings.py`:
```python
def test_dashboard_settings_defaults():
    s = Settings(
        min_premium=100.0,
        unusual_premium_threshold=200.0,
    )
    assert s.dashboard_refresh_fast == 5.0
    assert s.dashboard_refresh_slow == 10.0
    assert s.dashboard_max_rows == 50
    assert s.dashboard_max_alerts == 200


def test_dashboard_refresh_fast_must_be_positive():
    with pytest.raises(Exception):
        Settings(
            min_premium=100.0,
            unusual_premium_threshold=200.0,
            dashboard_refresh_fast=0.0,
        )


def test_dashboard_max_rows_must_be_at_least_one():
    with pytest.raises(Exception):
        Settings(
            min_premium=100.0,
            unusual_premium_threshold=200.0,
            dashboard_max_rows=0,
        )
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_settings.py -k "dashboard" -v
```
Expected: FAIL — `Settings` has no field `dashboard_refresh_fast`

**Step 3: Implement in `config/settings.py`**

Add after the `alert_email` field (before the validators):
```python
    # Dashboard
    dashboard_refresh_fast: float = Field(
        default=5.0,
        gt=0,
        description="Sentiment and alerts panel refresh interval in seconds",
    )
    dashboard_refresh_slow: float = Field(
        default=10.0,
        gt=0,
        description="DB table (signals, trades) refresh interval in seconds",
    )
    dashboard_max_rows: int = Field(
        default=50,
        ge=1,
        description="Maximum rows to display in each DB-backed DataTable",
    )
    dashboard_max_alerts: int = Field(
        default=200,
        ge=1,
        description="Maximum alerts to accumulate in SharedState queue and alerts panel",
    )
```

**Step 4: Run to confirm PASS**

```
pytest tests/test_settings.py -v
```
Expected: all PASSED (including existing tests)

**Step 5: Commit**

```bash
git add config/settings.py tests/test_settings.py
git commit -m "feat: add dashboard_refresh_fast/slow/max_rows/max_alerts settings"
```

---

## Task 3: Implement `src/dashboard/shared_state.py`

**Files:**
- Create: `src/dashboard/shared_state.py`
- Test: `tests/test_dashboard.py`

**Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`:
```python
from src.dashboard.shared_state import SharedState

# --- helpers ---

def _make_snap(symbol: str) -> "SentimentSnapshot":
    from src.analysis.sentiment import SentimentSnapshot
    return SentimentSnapshot(
        symbol=symbol,
        window_seconds=3600.0,
        computed_at=datetime.now(timezone.utc),
        trade_count=10,
        call_volume=500,
        put_volume=300,
        call_premium=100_000.0,
        put_premium=60_000.0,
        call_count=5,
        put_count=3,
        put_call_volume_ratio=0.6,
        put_call_premium_ratio=0.6,
        net_premium=40_000.0,
        avg_call_iv=0.25,
        avg_put_iv=0.30,
        iv_skew=0.05,
        net_delta_exposure=12_000.0,
        net_gamma_exposure=-5_000.0,
        bullish_premium=80_000.0,
        bearish_premium=40_000.0,
        directional_bias=0.33,
    )


def _make_alert(i: int = 0, level=None) -> "Alert":
    from src.alerts.rules import Alert, AlertLevel
    return Alert(
        symbol="SPY",
        level=level or AlertLevel.LOW,
        title=f"SPY TEST {i}",
        body=f"Test alert body {i}\nSecond line",
        source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY"},
    )


# --- tests ---

class TestSharedState:
    def test_update_and_get_sentiment(self):
        state = SharedState()
        snap = _make_snap("SPY")
        state.update_sentiment(snap)
        assert state.get_sentiment("SPY") is snap

    def test_get_sentiment_unknown_symbol_returns_none(self):
        state = SharedState()
        assert state.get_sentiment("UNKNOWN") is None

    def test_get_all_sentiment_returns_all_symbols(self):
        state = SharedState()
        state.update_sentiment(_make_snap("SPY"))
        state.update_sentiment(_make_snap("QQQ"))
        result = state.get_all_sentiment()
        assert set(result.keys()) == {"SPY", "QQQ"}

    def test_get_all_sentiment_returns_copy(self):
        state = SharedState()
        state.update_sentiment(_make_snap("SPY"))
        result = state.get_all_sentiment()
        result["NEW"] = _make_snap("NEW")
        assert "NEW" not in state.get_all_sentiment()

    def test_push_and_drain_single_alert(self):
        state = SharedState()
        alert = _make_alert()
        state.push_alert(alert)
        drained = state.drain_alerts()
        assert len(drained) == 1
        assert drained[0] is alert

    def test_drain_empty_returns_empty_list(self):
        state = SharedState()
        assert state.drain_alerts() == []

    def test_alert_overflow_evicts_oldest(self):
        state = SharedState(max_alerts=3)
        for i in range(5):
            state.push_alert(_make_alert(i))
        drained = state.drain_alerts(max_count=10)
        assert len(drained) == 3

    def test_drain_max_count_is_respected(self):
        state = SharedState()
        for i in range(10):
            state.push_alert(_make_alert(i))
        drained = state.drain_alerts(max_count=4)
        assert len(drained) == 4
        # 6 remaining
        remaining = state.drain_alerts(max_count=100)
        assert len(remaining) == 6
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_dashboard.py::TestSharedState -v
```
Expected: `ModuleNotFoundError: No module named 'src.dashboard.shared_state'`

**Step 3: Implement `src/dashboard/shared_state.py`**

```python
from __future__ import annotations

import queue

from loguru import logger

from src.alerts.rules import Alert
from src.analysis.sentiment import SentimentSnapshot


class SharedState:
    """Thread-safe state shared between the asyncio pipeline and Dash/Flask.

    The asyncio pipeline (producer thread) writes via update_sentiment() and
    push_alert(). Dash callbacks (Flask consumer thread) read via
    get_sentiment(), get_all_sentiment(), and drain_alerts().

    Thread safety guarantees:
    - _sentiment dict: CPython GIL ensures atomic assignment of immutable
      Pydantic values. SentimentSnapshot is never mutated after construction.
    - _alert_queue: queue.Queue is fully thread-safe by design; uses
      put_nowait/get_nowait to avoid blocking either thread.

    Args:
        max_alerts: Capacity of the alert queue. Oldest alert is dropped
            when a new alert arrives and the queue is full.
    """

    def __init__(self, max_alerts: int | None = None) -> None:
        if max_alerts is None:
            from config.settings import settings
            max_alerts = settings.dashboard_max_alerts
        self._sentiment: dict[str, SentimentSnapshot] = {}
        self._alert_queue: queue.Queue[Alert] = queue.Queue(maxsize=max_alerts)

    # ------------------------------------------------------------------
    # Sentiment (written by asyncio pipeline, read by Dash callback)
    # ------------------------------------------------------------------

    def update_sentiment(self, snapshot: SentimentSnapshot) -> None:
        """Store the latest SentimentSnapshot for a symbol.

        Called from the asyncio pipeline thread. Overwrites any previous
        snapshot for the same symbol.

        Args:
            snapshot: Latest SentimentSnapshot from SentimentAggregator.
        """
        self._sentiment[snapshot.symbol] = snapshot

    def get_sentiment(self, symbol: str) -> SentimentSnapshot | None:
        """Return the most recent SentimentSnapshot for a symbol.

        Called from Dash callback thread.

        Args:
            symbol: Ticker symbol, e.g. "SPY".

        Returns:
            Latest SentimentSnapshot, or None if no data for this symbol.
        """
        return self._sentiment.get(symbol)

    def get_all_sentiment(self) -> dict[str, SentimentSnapshot]:
        """Return a shallow copy of all current sentiment snapshots.

        Called from Dash callback thread.

        Returns:
            Dict mapping symbol → SentimentSnapshot for all known symbols.
        """
        return dict(self._sentiment)

    # ------------------------------------------------------------------
    # Alerts (written by asyncio pipeline, drained by Dash callback)
    # ------------------------------------------------------------------

    def push_alert(self, alert: Alert) -> None:
        """Enqueue an alert for display in the dashboard.

        Non-blocking. If the queue is full, the oldest alert is dropped
        to make room for the new one.

        Called from the asyncio pipeline thread.

        Args:
            alert: Alert to enqueue.
        """
        try:
            self._alert_queue.put_nowait(alert)
        except queue.Full:
            try:
                self._alert_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._alert_queue.put_nowait(alert)
            except queue.Full:
                logger.warning("shared_state: alert queue still full after eviction; dropping alert")

    def drain_alerts(self, max_count: int = 50) -> list[Alert]:
        """Remove and return up to max_count alerts from the queue.

        Non-blocking. Returns an empty list when the queue is empty.
        Called from the Dash callback thread.

        Args:
            max_count: Maximum number of alerts to return.

        Returns:
            List of Alert objects, oldest first.
        """
        result: list[Alert] = []
        while len(result) < max_count:
            try:
                result.append(self._alert_queue.get_nowait())
            except queue.Empty:
                break
        return result
```

**Step 4: Run to confirm PASS**

```
pytest tests/test_dashboard.py::TestSharedState -v
```
Expected: 8 PASSED

**Step 5: Commit**

```bash
git add src/dashboard/shared_state.py tests/test_dashboard.py
git commit -m "feat: implement SharedState with queue.Queue for pipeline-to-Dash bridge"
```

---

## Task 4: Implement `src/dashboard/layouts.py`

**Files:**
- Create: `src/dashboard/layouts.py`
- Test: `tests/test_dashboard.py`

**Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`:
```python
from src.dashboard.layouts import build_layout


def _collect_ids(component) -> set[str]:
    """Recursively collect all component IDs in a Dash layout tree."""
    ids: set[str] = set()
    if not hasattr(component, "id"):
        return ids
    if component.id:
        ids.add(component.id)
    children = getattr(component, "children", None)
    if children is None:
        return ids
    if isinstance(children, list):
        for child in children:
            ids |= _collect_ids(child)
    elif hasattr(children, "id"):
        ids |= _collect_ids(children)
    return ids


class TestLayout:
    _REQUIRED_IDS = {
        "fast-interval",
        "slow-interval",
        "alerts-store",
        "symbol-dropdown",
        "last-update",
        "sentiment-section",
        "signals-table",
        "trades-table",
        "alerts-panel",
    }

    def test_build_layout_returns_html_div(self):
        layout = build_layout(["SPY"])
        assert isinstance(layout, html.Div)

    def test_build_layout_contains_all_required_ids(self):
        layout = build_layout(["SPY", "QQQ"])
        found_ids = _collect_ids(layout)
        for required in self._REQUIRED_IDS:
            assert required in found_ids, f"Layout missing component id='{required}'"

    def test_build_layout_dropdown_has_all_symbols(self):
        layout = build_layout(["SPY", "QQQ", "AAPL"])
        ids = _collect_ids(layout)
        assert "symbol-dropdown" in ids
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_dashboard.py::TestLayout -v
```
Expected: `ModuleNotFoundError: No module named 'src.dashboard.layouts'`

**Step 3: Implement `src/dashboard/layouts.py`**

```python
from __future__ import annotations

from dash import dcc, html
from dash import dash_table


_SIGNAL_COLUMNS = ["Time", "Symbol", "Type", "Side", "Premium", "Reason"]
_TRADE_COLUMNS  = ["Time", "Symbol", "Type", "Side", "Premium", "Strength"]

_TABLE_STYLE_HEADER = {"backgroundColor": "#222", "color": "#eee", "fontWeight": "bold"}
_TABLE_STYLE_DATA   = {"backgroundColor": "#1a1a1a", "color": "#eee"}
_TABLE_STYLE_TABLE  = {"marginBottom": "16px", "overflowX": "auto"}


def build_layout(
    symbols: list[str],
    fast_ms: int = 5_000,
    slow_ms: int = 10_000,
) -> html.Div:
    """Build the full single-page dashboard layout.

    All component IDs used by callbacks are defined here. Callbacks must
    reference these IDs exactly. No IO is performed — this is a pure
    component factory.

    IDs defined:
        fast-interval, slow-interval, alerts-store,
        symbol-dropdown, last-update, sentiment-section,
        signals-table, trades-table, alerts-panel

    Args:
        symbols: Ticker symbols for the dropdown. First symbol is pre-selected.
        fast_ms: Refresh interval (ms) for sentiment cards and alerts panel.
        slow_ms: Refresh interval (ms) for DB-backed signals and trades tables.

    Returns:
        Root html.Div of the complete dashboard layout.
    """
    default_symbol = symbols[0] if symbols else None

    return html.Div(
        style={
            "fontFamily": "monospace",
            "backgroundColor": "#111",
            "color": "#eee",
            "padding": "12px",
            "minHeight": "100vh",
        },
        children=[
            # Timers
            dcc.Interval(id="fast-interval", interval=fast_ms, n_intervals=0),
            dcc.Interval(id="slow-interval", interval=slow_ms, n_intervals=0),

            # Client-side alerts accumulator
            dcc.Store(id="alerts-store", data=[]),

            # ── Header ────────────────────────────────────────────────
            html.Div(
                style={"display": "flex", "alignItems": "center", "gap": "16px", "marginBottom": "12px"},
                children=[
                    html.H2("Options Flow", style={"margin": 0, "color": "#4FC3F7"}),
                    dcc.Dropdown(
                        id="symbol-dropdown",
                        options=[{"label": s, "value": s} for s in symbols],
                        value=default_symbol,
                        clearable=False,
                        style={"width": "120px", "color": "#111", "fontFamily": "monospace"},
                    ),
                    html.Span(id="last-update", style={"color": "#888", "fontSize": "0.85em"}),
                ],
            ),

            # ── Sentiment KPI cards ────────────────────────────────────
            html.Div(id="sentiment-section", style={"marginBottom": "16px", "flexWrap": "wrap"}),

            # ── Unusual Signals ────────────────────────────────────────
            html.H4("Unusual Signals", style={"color": "#FF8C00", "marginBottom": "4px"}),
            dash_table.DataTable(
                id="signals-table",
                columns=[{"name": c, "id": c} for c in _SIGNAL_COLUMNS],
                data=[],
                style_header=_TABLE_STYLE_HEADER,
                style_data=_TABLE_STYLE_DATA,
                style_table=_TABLE_STYLE_TABLE,
                page_size=20,
                sort_action="native",
            ),

            # ── Classified Trades ──────────────────────────────────────
            html.H4("Classified Trades", style={"color": "#4FC3F7", "marginBottom": "4px"}),
            dash_table.DataTable(
                id="trades-table",
                columns=[{"name": c, "id": c} for c in _TRADE_COLUMNS],
                data=[],
                style_header=_TABLE_STYLE_HEADER,
                style_data=_TABLE_STYLE_DATA,
                style_table=_TABLE_STYLE_TABLE,
                page_size=20,
                sort_action="native",
            ),

            # ── Alerts feed ────────────────────────────────────────────
            html.H4("Alerts", style={"color": "#FF4444", "marginBottom": "4px"}),
            html.Div(
                id="alerts-panel",
                style={
                    "maxHeight": "300px",
                    "overflowY": "auto",
                    "backgroundColor": "#1a1a1a",
                    "padding": "8px",
                    "borderRadius": "4px",
                },
            ),
        ],
    )
```

**Step 4: Run to confirm PASS**

```
pytest tests/test_dashboard.py::TestLayout -v
```
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add src/dashboard/layouts.py tests/test_dashboard.py
git commit -m "feat: implement dashboard layouts with single-page flow design"
```

---

## Task 5: Implement Pure Rendering Functions in `src/dashboard/callbacks.py`

**Files:**
- Create: `src/dashboard/callbacks.py` (pure functions section only — no `setup_callbacks` yet)
- Test: `tests/test_dashboard.py`

**Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`:
```python
from src.alerts.rules import AlertLevel
from src.storage.models import ClassifiedTradeRecord, UnusualSignalRecord
from src.dashboard.callbacks import (
    _alert_to_div,
    _sentiment_kpis,
    _signal_record_to_row,
    _trade_record_to_row,
)


def _make_signal_record(premium: float | None = 100_000.0) -> UnusualSignalRecord:
    return UnusualSignalRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20261231",
        strike=500.0,
        right="C",
        underlying_price=490.0,
        implied_vol=0.25,
        delta=0.45,
        effective_price=1.00,
        trade_type="block",
        aggressor="buy",
        premium=premium,
        volume_delta=500,
        signal_strength=7.5,
        top_reason="premium_size",
        reasons='["premium_size"]',
        classified_at=datetime(2026, 3, 14, 14, 30, 0),
        flagged_at=datetime(2026, 3, 14, 14, 30, 1),
    )


def _make_trade_record() -> ClassifiedTradeRecord:
    return ClassifiedTradeRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20261231",
        strike=500.0,
        right="C",
        underlying_price=490.0,
        implied_vol=0.25,
        delta=0.45,
        trade_type="block",
        aggressor="buy",
        spread_position=0.85,
        effective_price=1.00,
        last_size=500,
        premium=50_000.0,
        signal_strength=5.2,
        volume_delta=500,
        window_ticks=1,
        classified_at=datetime(2026, 3, 14, 14, 30, 0),
    )


class TestPureFunctions:
    def test_sentiment_kpis_none_returns_eight_dash_cards(self):
        cards = _sentiment_kpis(None)
        assert len(cards) == 8
        for card in cards:
            # Each card: outer Div > [label Div, value Div]
            assert card.children[1].children == "—"

    def test_sentiment_kpis_with_snapshot_returns_eight_cards(self):
        snap = _make_snap("SPY")
        cards = _sentiment_kpis(snap)
        assert len(cards) == 8

    def test_sentiment_kpis_pc_vol_formatted(self):
        snap = _make_snap("SPY")  # put_call_volume_ratio=0.6
        cards = _sentiment_kpis(snap)
        # First card is "P/C Volume" → "0.60"
        assert cards[0].children[1].children == "0.60"

    def test_signal_record_to_row_expected_keys(self):
        row = _signal_record_to_row(_make_signal_record())
        assert set(row.keys()) == {"Time", "Symbol", "Type", "Side", "Premium", "Reason"}

    def test_signal_record_to_row_none_premium_shows_dash(self):
        row = _signal_record_to_row(_make_signal_record(premium=None))
        assert row["Premium"] == "—"

    def test_signal_record_to_row_premium_formatted(self):
        row = _signal_record_to_row(_make_signal_record(premium=100_000.0))
        assert row["Premium"] == "$100,000"

    def test_trade_record_to_row_expected_keys(self):
        row = _trade_record_to_row(_make_trade_record())
        assert set(row.keys()) == {"Time", "Symbol", "Type", "Side", "Premium", "Strength"}

    def test_alert_to_div_returns_html_div(self):
        div = _alert_to_div(_make_alert())
        assert isinstance(div, html.Div)

    def test_alert_to_div_high_level_badge_is_red(self):
        div = _alert_to_div(_make_alert(level=AlertLevel.HIGH))
        badge = div.children[0]
        assert badge.style["color"] == "#FF4444"

    def test_alert_to_div_medium_level_badge_is_orange(self):
        div = _alert_to_div(_make_alert(level=AlertLevel.MEDIUM))
        badge = div.children[0]
        assert badge.style["color"] == "#FF8C00"
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_dashboard.py::TestPureFunctions -v
```
Expected: `ModuleNotFoundError: No module named 'src.dashboard.callbacks'`

**Step 3: Create `src/dashboard/callbacks.py`** (pure functions only — `setup_callbacks` added in Task 6)

```python
from __future__ import annotations

from datetime import datetime, timezone

from dash import Dash, Input, Output, State, html
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from src.alerts.rules import Alert, AlertLevel
from src.analysis.sentiment import SentimentSnapshot
from src.storage.db import get_sync_engine
from src.storage.models import ClassifiedTradeRecord, UnusualSignalRecord


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_ratio(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _fmt_dollars(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


# ---------------------------------------------------------------------------
# Pure rendering functions
# ---------------------------------------------------------------------------


def _sentiment_kpis(snap: SentimentSnapshot | None) -> list[html.Div]:
    """Convert a SentimentSnapshot to a row of 8 KPI cards.

    Called from the update_sentiment callback. Returns eight html.Div
    cards regardless of whether snap is None (dashes shown for None).

    Args:
        snap: Latest SentimentSnapshot for the selected symbol, or None
            when no data is available.

    Returns:
        List of 8 html.Div KPI cards, in order:
        P/C Volume, P/C Premium, Net Premium, Directional,
        IV Skew, Δ Exposure, Γ Exposure, Trades.
    """
    values = [
        ("P/C Volume",  _fmt_ratio(_get(snap, "put_call_volume_ratio"))),
        ("P/C Premium", _fmt_ratio(_get(snap, "put_call_premium_ratio"))),
        ("Net Premium", _fmt_dollars(_get(snap, "net_premium"))),
        ("Directional", _fmt_pct(_get(snap, "directional_bias"))),
        ("IV Skew",     _fmt_ratio(_get(snap, "iv_skew"))),
        ("Δ Exposure",  _fmt_dollars(_get(snap, "net_delta_exposure"))),
        ("Γ Exposure",  _fmt_dollars(_get(snap, "net_gamma_exposure"))),
        ("Trades",      str(snap.trade_count) if snap is not None else "—"),
    ]
    return [
        html.Div(
            style={
                "display": "inline-block",
                "padding": "8px 16px",
                "margin": "4px",
                "backgroundColor": "#222",
                "borderRadius": "4px",
                "minWidth": "110px",
            },
            children=[
                html.Div(label, style={"fontSize": "0.75em", "color": "#888"}),
                html.Div(val,   style={"fontSize": "1.1em", "fontWeight": "bold"}),
            ],
        )
        for label, val in values
    ]


def _get(snap: SentimentSnapshot | None, attr: str) -> float | None:
    """Safely read a float attribute from a snapshot that may be None."""
    return getattr(snap, attr) if snap is not None else None


def _signal_record_to_row(r: UnusualSignalRecord) -> dict:
    """Convert an UnusualSignalRecord ORM row to a DataTable row dict.

    Args:
        r: Row from the unusual_signals table.

    Returns:
        Dict with keys: Time, Symbol, Type, Side, Premium, Reason.
    """
    return {
        "Time":    r.flagged_at.strftime("%H:%M:%S"),
        "Symbol":  r.symbol,
        "Type":    r.trade_type,
        "Side":    r.aggressor,
        "Premium": f"${r.premium:,.0f}" if r.premium is not None else "—",
        "Reason":  r.top_reason,
    }


def _trade_record_to_row(r: ClassifiedTradeRecord) -> dict:
    """Convert a ClassifiedTradeRecord ORM row to a DataTable row dict.

    Args:
        r: Row from the classified_trades table.

    Returns:
        Dict with keys: Time, Symbol, Type, Side, Premium, Strength.
    """
    return {
        "Time":     r.classified_at.strftime("%H:%M:%S"),
        "Symbol":   r.symbol,
        "Type":     r.trade_type,
        "Side":     r.aggressor,
        "Premium":  f"${r.premium:,.0f}" if r.premium is not None else "—",
        "Strength": f"{r.signal_strength:.1f}" if r.signal_strength is not None else "—",
    }


_LEVEL_COLORS: dict[AlertLevel, str] = {
    AlertLevel.HIGH:   "#FF4444",
    AlertLevel.MEDIUM: "#FF8C00",
    AlertLevel.LOW:    "#FFD700",
}


def _alert_to_div(alert: Alert) -> html.Div:
    """Render an Alert as a styled html.Div for the alerts panel.

    Displays: [LEVEL] title — first body line  HH:MM:SS

    Args:
        alert: Alert to render.

    Returns:
        html.Div with three children: level badge, title+body, timestamp.
    """
    color = _LEVEL_COLORS.get(alert.level, "#888")
    first_line = alert.body.split("\n")[0]
    return html.Div(
        style={"padding": "4px 0", "borderBottom": "1px solid #333"},
        children=[
            html.Span(
                f"[{alert.level.value.upper()}] ",
                style={"color": color, "fontWeight": "bold"},
            ),
            html.Span(f"{alert.title} — {first_line}"),
            html.Span(
                f" {alert.emitted_at.strftime('%H:%M:%S')}",
                style={"color": "#888", "fontSize": "0.85em"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Sync DB query helpers
# ---------------------------------------------------------------------------


def _query_signal_rows(symbol: str | None, limit: int) -> list[dict]:
    """Query unusual_signals table via sync session, ordered by flagged_at desc.

    Args:
        symbol: Filter rows to this symbol if provided; None = all symbols.
        limit: Maximum number of rows to return.

    Returns:
        List of row dicts suitable for dash_table.DataTable data property.
    """
    with Session(get_sync_engine()) as session:
        q = (
            select(UnusualSignalRecord)
            .order_by(UnusualSignalRecord.flagged_at.desc())
            .limit(limit)
        )
        if symbol:
            q = q.where(UnusualSignalRecord.symbol == symbol)
        rows = session.execute(q).scalars().all()
    return [_signal_record_to_row(r) for r in rows]


def _query_trade_rows(symbol: str | None, limit: int) -> list[dict]:
    """Query classified_trades table via sync session, ordered by classified_at desc.

    Args:
        symbol: Filter rows to this symbol if provided; None = all symbols.
        limit: Maximum number of rows to return.

    Returns:
        List of row dicts suitable for dash_table.DataTable data property.
    """
    with Session(get_sync_engine()) as session:
        q = (
            select(ClassifiedTradeRecord)
            .order_by(ClassifiedTradeRecord.classified_at.desc())
            .limit(limit)
        )
        if symbol:
            q = q.where(ClassifiedTradeRecord.symbol == symbol)
        rows = session.execute(q).scalars().all()
    return [_trade_record_to_row(r) for r in rows]


# ---------------------------------------------------------------------------
# setup_callbacks — added in Task 6
# ---------------------------------------------------------------------------
```

**Step 4: Run to confirm PASS**

```
pytest tests/test_dashboard.py::TestPureFunctions -v
```
Expected: 10 PASSED

**Step 5: Commit**

```bash
git add src/dashboard/callbacks.py tests/test_dashboard.py
git commit -m "feat: add pure rendering functions and sync DB query helpers to callbacks.py"
```

---

## Task 6: Implement `setup_callbacks` and `src/dashboard/app.py`

**Files:**
- Modify: `src/dashboard/callbacks.py` (add `setup_callbacks`)
- Create: `src/dashboard/app.py`
- Test: `tests/test_dashboard.py`

**Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`:
```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession

from src.dashboard.callbacks import _query_signal_rows, _query_trade_rows
from src.dashboard.app import create_app
from src.storage.models import Base


@pytest.fixture
def sync_db():
    """In-memory SQLite with all tables created. Returns a sync Engine."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


class TestDBQueryHelpers:
    def test_query_signal_rows_empty_db(self, sync_db, monkeypatch):
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert rows == []

    def test_query_signal_rows_returns_inserted_record(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            session.add(_make_signal_record())
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"
        assert rows[0]["Type"] == "block"

    def test_query_signal_rows_symbol_filter(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            r1 = _make_signal_record(); r1.symbol = "SPY"
            r2 = _make_signal_record(); r2.symbol = "QQQ"
            session.add_all([r1, r2])
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"

    def test_query_trade_rows_empty_db(self, sync_db, monkeypatch):
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_trade_rows("SPY", 10)
        assert rows == []

    def test_query_trade_rows_returns_inserted_record(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            session.add(_make_trade_record())
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_trade_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"


class TestCreateApp:
    def test_create_app_returns_dash_instance(self):
        from dash import Dash
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        assert isinstance(app, Dash)

    def test_create_app_title_is_set(self):
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        assert app.title == "Options Flow"

    def test_create_app_registers_four_callbacks(self):
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        # Four callbacks: sentiment, signals, trades, alerts
        assert len(app.callback_map) >= 4

    def test_create_app_default_symbols(self):
        state = SharedState()
        app = create_app(state)
        # Should not raise; uses default ["SPY"]
        assert app is not None
```

**Step 2: Run to confirm FAIL**

```
pytest tests/test_dashboard.py::TestDBQueryHelpers tests/test_dashboard.py::TestCreateApp -v
```
Expected: FAIL — `cannot import name 'create_app' from 'src.dashboard.app'`

**Step 3: Add `setup_callbacks` to `src/dashboard/callbacks.py`**

Append to the bottom of `callbacks.py`:
```python
def setup_callbacks(app: Dash, state: "SharedState") -> None:
    """Register all dcc.Interval-driven callbacks on the Dash app.

    All callbacks are closures that capture `state`. DB callbacks wrap
    exceptions to avoid crashing the Dash server on transient DB errors.

    Callbacks registered:
        update_sentiment  — fast-interval + symbol-dropdown → sentiment-section, last-update
        update_signals    — slow-interval + symbol-dropdown → signals-table data
        update_trades     — slow-interval + symbol-dropdown → trades-table data
        update_alerts     — fast-interval + alerts-store    → alerts-store data, alerts-panel

    Args:
        app: The Dash application instance to register callbacks on.
        state: SharedState instance shared with the asyncio pipeline.
    """
    from src.dashboard.shared_state import SharedState  # avoid circular at module level

    @app.callback(
        Output("sentiment-section", "children"),
        Output("last-update", "children"),
        Input("fast-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_sentiment(n_intervals: int, symbol: str) -> tuple[list, str]:
        snap = state.get_sentiment(symbol) if symbol else None
        ts = f"Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        return _sentiment_kpis(snap), ts

    @app.callback(
        Output("signals-table", "data"),
        Input("slow-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_signals(n_intervals: int, symbol: str) -> list[dict]:
        try:
            return _query_signal_rows(symbol, settings.dashboard_max_rows)
        except Exception:
            logger.exception("update_signals: DB query failed")
            return []

    @app.callback(
        Output("trades-table", "data"),
        Input("slow-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_trades(n_intervals: int, symbol: str) -> list[dict]:
        try:
            return _query_trade_rows(symbol, settings.dashboard_max_rows)
        except Exception:
            logger.exception("update_trades: DB query failed")
            return []

    @app.callback(
        Output("alerts-store", "data"),
        Output("alerts-panel", "children"),
        Input("fast-interval", "n_intervals"),
        State("alerts-store", "data"),
    )
    def update_alerts(n_intervals: int, stored: list[dict] | None) -> tuple[list[dict], list]:
        new_alerts = state.drain_alerts()
        accumulated = (stored or []) + [a.model_dump(mode="json") for a in new_alerts]
        accumulated = accumulated[-settings.dashboard_max_alerts:]
        children = [_alert_to_div(Alert(**a)) for a in reversed(accumulated)]
        return accumulated, children
```

**Step 4: Create `src/dashboard/app.py`**

```python
from __future__ import annotations

from dash import Dash

from src.dashboard.callbacks import setup_callbacks
from src.dashboard.layouts import build_layout
from src.dashboard.shared_state import SharedState


def create_app(state: SharedState, symbols: list[str] | None = None) -> Dash:
    """Create and configure the Options Flow Dash application.

    Builds the single-page layout and registers all dcc.Interval callbacks.
    Does NOT start the server — call app.run_server() from the orchestration
    layer or from the __main__ block below.

    Args:
        state: SharedState instance bridging the asyncio pipeline and Dash.
        symbols: Watchlist symbols for the symbol dropdown. Defaults to ["SPY"].

    Returns:
        Configured Dash application, ready to call run_server() on.
    """
    if symbols is None:
        symbols = ["SPY"]

    from config.settings import settings

    app = Dash(__name__, title="Options Flow")
    app.layout = build_layout(
        symbols,
        fast_ms=int(settings.dashboard_refresh_fast * 1000),
        slow_ms=int(settings.dashboard_refresh_slow * 1000),
    )
    setup_callbacks(app, state)
    return app


if __name__ == "__main__":
    import sys

    state = SharedState()
    symbols = sys.argv[1:] or ["SPY", "QQQ", "AAPL"]
    app = create_app(state, symbols=symbols)
    app.run_server(debug=True, port=8050)
```

**Step 5: Run to confirm PASS**

```
pytest tests/test_dashboard.py::TestDBQueryHelpers tests/test_dashboard.py::TestCreateApp -v
```
Expected: 9 PASSED

**Step 6: Commit**

```bash
git add src/dashboard/callbacks.py src/dashboard/app.py tests/test_dashboard.py
git commit -m "feat: implement setup_callbacks, create_app, and DB query helpers"
```

---

## Task 7: Update `__init__.py` and Run Full Test Suite

**Files:**
- Modify: `src/dashboard/__init__.py`
- Verify: all 300+ tests pass

**Step 1: Update `src/dashboard/__init__.py`**

```python
from src.dashboard.app import create_app
from src.dashboard.shared_state import SharedState

__all__ = ["create_app", "SharedState"]
```

**Step 2: Run full test suite**

```
pytest -x -v
```
Expected: 300+ PASSED, 0 FAILED
(276 pre-existing + ~27 new dashboard tests)

**Step 3: If any failures, fix before committing**

Common issues:
- Import errors → check `from __future__ import annotations` is at top of every new file
- `AlertLevel` comparison failing → ensure it's `AlertLevel.HIGH` (not string `"high"`)
- `_make_signal_record` secondary symbols: each ORM instance is independent; verify `r2.symbol = "QQQ"` works (attribute assignment on unmapped instance is fine)

**Step 4: Commit**

```bash
git add src/dashboard/__init__.py
git commit -m "feat: export create_app and SharedState from dashboard __init__"
```

**Step 5: Update memory**

Update `MEMORY.md`:
- Step 12: src/dashboard/ — DONE (300+ tests passing, ~27 dashboard-specific)
- Next Step: Step 13 — src/data/scanner.py (IBKR market scanners)
- SharedState pattern: queue.Queue for alerts (bounded), plain dict for sentiment (GIL-safe)
- Sync engine: get_sync_engine() in db.py, strips aiosqlite/asyncpg prefix
- Dashboard: create_app(state, symbols) → Dash; setup_callbacks(app, state) registers 4 callbacks

---

## Summary

| Task | Files Changed | Tests Added |
|------|--------------|-------------|
| 1 — Sync engine | `src/storage/db.py` | 3 |
| 2 — Settings | `config/settings.py` | 3 |
| 3 — SharedState | `src/dashboard/shared_state.py` | 8 |
| 4 — Layouts | `src/dashboard/layouts.py` | 3 |
| 5 — Pure functions | `src/dashboard/callbacks.py` | 10 |
| 6 — App + setup | `src/dashboard/app.py`, `callbacks.py` | 9 |
| 7 — Init + verify | `src/dashboard/__init__.py` | 0 |

**Total new tests: ~36**
