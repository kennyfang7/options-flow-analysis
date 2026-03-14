# Dashboard Design — Step 12

**Date:** 2026-03-14
**Module:** `src/dashboard/`
**Approach:** Hybrid (DB-backed tables + live in-memory sentiment/alerts)

---

## Problem

The analysis pipeline (steps 1–11) produces `ClassifiedTrade`, `EnrichedTrade`,
`UnusualSignal`, `SmartMoneySignal`, `SentimentSnapshot`, and `Alert` objects.
There is no way to observe this data in real time without a visualization layer.

---

## Architecture

### Data Strategy: Hybrid

| Panel | Source |
|---|---|
| Recent classified trades | SQLite via sync session (`classified_trades`) |
| Unusual signals feed | SQLite via sync session (`unusual_signals`) |
| Live sentiment gauges | In-memory `SharedState` (written by asyncio pipeline) |
| Recent alerts log | In-memory `SharedState` (written by asyncio pipeline) |

Dash/Flask is synchronous and runs in a separate thread from the asyncio event
loop. DB queries in callbacks use a synchronous SQLAlchemy engine. Live data
(sentiment, alerts) is exchanged through `queue.Queue`-based `SharedState` —
no manual locking required, no event-loop interference.

### File Structure

```
src/dashboard/
├── shared_state.py   — SharedState class (queue.Queue, thread-safe)
├── app.py            — Dash app factory; accepts SharedState
├── layouts.py        — Pure layout functions (no IO, no callbacks)
└── callbacks.py      — dcc.Interval callbacks; reads SharedState + sync DB
```

### SharedState

```python
class SharedState:
    """Thread-safe bridge between asyncio pipeline and Dash/Flask."""
    _sentiment: dict[str, SentimentSnapshot]   # latest per symbol
    _sentiment_q: queue.Queue                  # bounded at 500
    _alerts_q: queue.Queue                     # bounded at 200
```

The asyncio pipeline calls `state.update_sentiment(snapshot)` and
`state.push_alert(alert)` using `put_nowait` (non-blocking). Dash callbacks
call `state.get_sentiment(symbol)` and `state.drain_alerts()` using
`get_nowait` — no blocking, no lock.

### Sync Engine

`src/storage/db.py` gains two helpers:

- `make_sync_engine(url)` — strips async driver prefix (`aiosqlite`, `asyncpg`),
  returns a standard `create_engine()` instance
- `get_sync_engine()` — lazy singleton, safe to call from any thread

No changes to models. SQLAlchemy ORM models are engine-agnostic.

---

## Layout: Single-Page, Top-to-Bottom

```
┌─────────────────────────────────────────────────────┐
│  OPTIONS FLOW  │ Symbol: [SPY ▼]   🔴 Last: 14:32  │  ← header strip
├─────────────────────────────────────────────────────┤
│  P/C Vol  │  P/C Prem  │  Net Prem  │  Directional  │  ← sentiment KPIs (4 cards)
│  IV Skew  │  ΔExp      │  ΓExp      │  Trades: 42   │
├─────────────────────────────────────────────────────┤
│  UNUSUAL SIGNALS                      [auto-refresh] │
│  Time │ Symbol │ Type │ Side │ Premium │ Reason      │
│  ...                                                 │
├─────────────────────────────────────────────────────┤
│  CLASSIFIED TRADES                    [auto-refresh] │
│  Time │ Symbol │ Type │ Side │ Premium │ Strength    │
│  ...                                                 │
├─────────────────────────────────────────────────────┤
│  ALERTS LOG                           [live feed]    │
│  🔴 HIGH  │ SPY UNUSUAL HIGH │ BLOCK BUY $450k ...  │
│  🟠 MED   │ TSLA SMART MONEY │ ...                  │
└─────────────────────────────────────────────────────┘
```

- **Header**: Dash `dcc.Dropdown` for symbol selection; last-updated timestamp
- **Sentiment KPIs**: 8 `html.Div` cards populated by a 5-second `dcc.Interval`
  reading `SharedState.get_sentiment(symbol)`
- **Unusual Signals table**: `dash_table.DataTable` refreshed every 10s via DB query
  (`select UnusualSignalRecord order by flagged_at desc limit 50`)
- **Classified Trades table**: `dash_table.DataTable` refreshed every 10s via DB query
  (`select ClassifiedTradeRecord order by classified_at desc limit 100`)
- **Alerts log**: styled `html.Div` list refreshed every 5s from
  `SharedState.drain_alerts()` + in-memory accumulator in callback store

---

## Components

### `shared_state.py`
- `SharedState` dataclass with `queue.Queue` fields
- `update_sentiment(snapshot)` — `put_nowait`; evicts oldest if full
- `push_alert(alert)` — `put_nowait`; evicts oldest if full
- `get_sentiment(symbol)` → `SentimentSnapshot | None`
- `get_all_sentiment()` → `dict[str, SentimentSnapshot]`
- `drain_alerts(max_count=50)` → `list[Alert]`

### `app.py`
- `create_app(state: SharedState) -> Dash` — builds Dash app, calls `setup_callbacks`
- Imports layout from `layouts.py`, callbacks from `callbacks.py`
- `if __name__ == "__main__"` block: creates `SharedState`, starts app with debug server

### `layouts.py`
- `build_layout(symbols: list[str]) -> html.Div` — full page layout
- `sentiment_cards()` → list of KPI card divs (ids for callback targets)
- `signals_table()` → `dash_table.DataTable` component
- `trades_table()` → `dash_table.DataTable` component
- `alerts_panel()` → styled div container
- No side effects, fully testable in isolation

### `callbacks.py`
- `setup_callbacks(app: Dash, state: SharedState) → None`
- Four `@app.callback` functions:
  1. `update_sentiment_cards` — Input: `dcc.Interval(5s)` + symbol dropdown → 8 KPI outputs
  2. `update_signals_table` — Input: `dcc.Interval(10s)` + symbol dropdown → DataTable data
  3. `update_trades_table` — Input: `dcc.Interval(10s)` + symbol dropdown → DataTable data
  4. `update_alerts_panel` — Input: `dcc.Interval(5s)` → alerts HTML + `dcc.Store` accumulator

---

## Data Flow

```
asyncio pipeline
  └─ SentimentAggregator.snapshot() ──► SharedState.update_sentiment()
  └─ AlertRules.evaluate_*()        ──► SharedState.push_alert()

Dash callback (Thread B, every 5s)
  └─ SharedState.get_sentiment(symbol) ──► KPI cards
  └─ SharedState.drain_alerts()        ──► alerts panel

Dash callback (Thread B, every 10s)
  └─ sync Session + select(UnusualSignalRecord) ──► signals table
  └─ sync Session + select(ClassifiedTradeRecord) ──► trades table
```

---

## Storage Changes

**`src/storage/db.py`** — add:
```python
def make_sync_engine(database_url: str | None = None) -> Engine: ...
def get_sync_engine() -> Engine: ...  # lazy singleton
```

No changes to `models.py`, `queries.py`, or any analysis module.

---

## Settings Changes

Add to `config/settings.py`:
```python
dashboard_refresh_fast: float = Field(default=5.0, gt=0)   # sentiment + alerts interval (s)
dashboard_refresh_slow: float = Field(default=10.0, gt=0)  # DB table interval (s)
dashboard_max_rows: int = Field(default=50, ge=1)           # max rows per DB table
dashboard_max_alerts: int = Field(default=200, ge=1)        # max alerts in SharedState
```

---

## Testing

### `tests/test_dashboard.py`

- `test_shared_state_update_sentiment` — write snapshot, read back via `get_sentiment`
- `test_shared_state_push_alert` — push alert, drain, verify contents
- `test_shared_state_overflow` — push 201 alerts to a cap-200 queue; oldest evicted
- `test_build_layout_smoke` — `build_layout(["SPY"])` returns `html.Div` without error
- `test_callbacks_sentiment` — inject mock `SharedState`, call `update_sentiment_cards`,
  verify output format
- `test_callbacks_signals_table` — inject seeded in-memory SQLite, verify table rows
- `test_callbacks_trades_table` — same pattern as signals table
- `test_get_sync_engine` — verify engine returns, URL has no async prefix

No integration tests needed (dashboard has no IBKR dependency).

---

## Constraints / Non-Goals

- No authentication — local-only dashboard
- No write operations from dashboard — read-only by design
- No WebSocket/SSE streaming — `dcc.Interval` polling is sufficient for 5-10s refresh
- No multi-page routing — single scrollable page only
- Dashboard does NOT call `Notifier` — alerts flow one-way from pipeline → SharedState → UI
