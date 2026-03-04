# Options Flow Analysis — Claude Code Guide

## Project Overview
Real-time options flow analysis platform powered by IBKR TWS API (with live options data package). Detects unusual activity, tracks smart money, and surfaces actionable signals from the options market.

## Tech Stack
- **Python 3.11+**
- **IBKR API**: `ib_insync` (high-level wrapper around TWS/Gateway API)
- **Data**: `pandas`, `numpy`
- **Storage**: SQLite (dev) → PostgreSQL (prod)
- **Visualization**: `plotly`, `dash` (dashboard)
- **Async**: `asyncio` (ib_insync is async-native)
- **Scheduling**: `APScheduler`
- **Logging**: `loguru`
- **Config**: `.env` + `pydantic-settings`

## Project Structure
```
options-flow/
├── claude.md
├── pyproject.toml
├── .env.example
├── config/
│   └── settings.py              # Pydantic settings, IBKR connection params
├── src/
│   ├── __init__.py
│   ├── connection/
│   │   ├── __init__.py
│   │   └── ibkr_client.py       # TWS/Gateway connect, disconnect, health check
│   ├── data/
│   │   ├── __init__.py
│   │   ├── scanner.py           # Market scanners (unusual volume, OI changes)
│   │   ├── chain_fetcher.py     # Option chain snapshots per underlying
│   │   ├── tick_stream.py       # Real-time tick-by-tick options data
│   │   └── historical.py        # Historical bar data for context
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── flow_classifier.py   # Classify trades: sweep, block, split, multi-leg
│   │   ├── unusual_detector.py  # Flag anomalies (vol vs OI, size vs ADV, premium)
│   │   ├── greeks_engine.py     # Greeks calculations / IV surface
│   │   ├── sentiment.py         # Put/call ratios, net premium, directional bias
│   │   └── smart_money.py       # Heuristics for institutional vs retail detection
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── models.py            # SQLAlchemy / dataclass models
│   │   ├── db.py                # DB engine, session management
│   │   └── queries.py           # Common query patterns
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── rules.py             # Alert trigger conditions
│   │   └── notifier.py          # Discord webhook, email, desktop toast
│   ├── dashboard/
│   │   ├── __init__.py
│   │   ├── app.py               # Dash app entry point
│   │   ├── layouts.py           # Page layouts
│   │   └── callbacks.py         # Interactive callbacks
│   └── utils/
│       ├── __init__.py
│       ├── formatting.py        # Display helpers, currency, Greek symbols
│       └── market_hours.py      # Market calendar, session awareness
├── scripts/
│   ├── run_scanner.py           # Entry: start flow scanning
│   ├── backfill.py              # Backfill historical OI / volume
│   └── run_dashboard.py         # Entry: launch Dash UI
└── tests/
    ├── conftest.py
    ├── test_connection.py
    ├── test_flow_classifier.py
    └── test_unusual_detector.py
```

## Module Build Order
Build and test modules in this sequence — each depends on the ones before it:
1. `config/settings.py` — env loading, connection params
2. `src/connection/ibkr_client.py` — connect to TWS, verify market data
3. `src/data/chain_fetcher.py` — pull option chains for a ticker
4. `src/storage/models.py` + `db.py` — define schema, persist snapshots
5. `src/data/tick_stream.py` — stream live ticks
6. `src/analysis/flow_classifier.py` — label trade types
7. `src/analysis/unusual_detector.py` — anomaly detection
8. `src/analysis/greeks_engine.py` — IV / Greeks layer
9. `src/analysis/sentiment.py` — aggregate metrics
10. `src/analysis/smart_money.py` — institutional heuristics
11. `src/alerts/rules.py` + `notifier.py` — alerting
12. `src/dashboard/` — visualization layer
13. `src/data/scanner.py` — IBKR market scanners
14. `scripts/` — entry points

## Coding Conventions
- **Type hints everywhere** — use `from __future__ import annotations`
- **Docstrings**: Google style on all public functions
- **Async-first**: use `async/await` with `ib_insync`; avoid blocking calls
- **Error handling**: never swallow exceptions silently; log with `loguru`
- **Data flow**: raw IBKR objects → pydantic/dataclass models → pandas DataFrames for analysis
- **No hardcoded tickers**: everything configurable via `.env` or watchlist files
- **Tests**: each module gets a corresponding test file; use `pytest` + `pytest-asyncio`

## IBKR-Specific Notes
- TWS or IB Gateway must be running locally (port 7497 for paper, 7496 for live)
- `ib_insync` handles the socket connection — always use `IB()` singleton pattern
- Rate limits: ~50 messages/sec to TWS; batch option chain requests
- Market data requires subscriptions — user has live options package
- Use `qualifyContracts()` before requesting data
- Option contract format: `Stock` → `Option(symbol, expiry, strike, right)`
- Real-time data comes via `reqMktData` or `reqTickByTickData`
- Historical data via `reqHistoricalData` — respect pacing limits (60 req/10 min)

## Key Domain Concepts
- **Block trade**: single large print, ≥ defined threshold (e.g., 500+ contracts)
- **Sweep**: aggressive order hitting multiple exchanges rapidly to fill
- **Unusual activity**: volume significantly exceeds open interest or average daily volume
- **Premium**: total dollar value of the trade (contracts × price × 100)
- **Smart money signal**: large premium + near-expiry + OTM = potential informed bet
- **Put/Call ratio**: aggregate sentiment gauge; extreme readings are contrarian signals
- **IV skew**: relative IV between OTM puts vs calls — reveals hedging demand
- **GEX/DEX**: gamma/delta exposure — useful for pinning and support/resistance levels

## When Generating Code
- Always check the module build order — don't reference modules that haven't been built yet
- Provide working code with proper imports, not pseudocode
- Include a `if __name__ == "__main__"` block for standalone testing where appropriate
- When touching the database, provide migration-safe patterns
- Dashboard components should be self-contained and testable in isolation
