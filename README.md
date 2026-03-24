# Options Flow Analysis

Real-time options flow analysis platform powered by the IBKR TWS API. Detects unusual activity, tracks smart money, and surfaces actionable signals from the options market.

---

## Features

- **Real-time tick streaming** — subscribes to live options data via IBKR TWS (up to 95 contracts simultaneously)
- **Trade classification** — identifies sweeps, blocks, splits, and multi-leg strategies
- **Greeks enrichment** — delta, gamma, theta, vega, IV from IBKR modelGreeks with Black-Scholes fallback
- **Unusual activity detection** — flags trades by premium size, OI ratio, signal strength, and OTM premium
- **Smart money scoring** — institutional heuristics (sweep aggression, big OTM bets, near-expiry positioning)
- **Sentiment aggregation** — rolling put/call ratios, net premium, IV skew, delta/gamma exposure per symbol
- **Market scanner** — auto-discovers hot symbols via IBKR scanner when no watchlist is provided
- **Alerting** — Discord webhook and email (stub) notifications at LOW / MEDIUM / HIGH severity
- **Dash dashboard** — live UI with sentiment metrics, signal log, trade feed, and alert stream
- **Persistent storage** — SQLite (dev) / PostgreSQL (prod) via async SQLAlchemy
- **Rate limiting** — centralised async rate limiter respecting IBKR's 50 msg/sec and 60 historical/10 min hard limits
- **Watchlist management** — JSON-backed ticker list with CRUD, group filtering, and hot-reload

---

## Prerequisites

| Requirement | Detail |
|---|---|
| Python | 3.11+ |
| IBKR TWS or IB Gateway | Running locally (paper: port 7497, live: port 7496) |
| Market data subscription | Live options data package required |
| OS | Windows / macOS / Linux |

---

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd options-flow-analysis

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e .
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Key settings:

```ini
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7496          # 7497 = paper trading, 7496 = live trading
IBKR_CLIENT_ID=1
IBKR_READONLY=true

# Database
DATABASE_URL=sqlite:///options_flow.db   # swap for postgresql:// in prod

# Watchlist
WATCHLIST_PATH=config/watchlist.txt

# Scanning Thresholds
UNUSUAL_VOLUME_MULTIPLIER=3.0   # flag if volume > X * avg daily volume
MIN_BLOCK_SIZE=500               # contracts to qualify as a block trade
MIN_PREMIUM=50000                # minimum dollar premium to track ($50k)

# Alerts (optional)
DISCORD_WEBHOOK_URL=
ALERT_EMAIL=
```

### Watchlist

Create `config/watchlist.txt` with one ticker per line:

```
SPY
QQQ
AAPL
TSLA
```

If the watchlist is empty or omitted, the IBKR market scanner will auto-discover the most active options symbols.

---

## Running

### Flow Scanner (headless)

Streams ticks, classifies trades, fires alerts, and persists to DB.

```bash
# Use symbols from watchlist
python scripts/run_scanner.py

# Watch specific symbols
python scripts/run_scanner.py SPY QQQ NVDA

# Stop with Ctrl+C
```

### Dashboard

Launches the Dash UI with a live pipeline feeding it in a background thread.

```bash
# Use watchlist symbols, default port 8050
python scripts/run_dashboard.py

# Custom symbols and port
python scripts/run_dashboard.py SPY AAPL --port 8080

# With Dash hot-reload
python scripts/run_dashboard.py --debug
```

Open `http://localhost:8050` in your browser.

---

## Analysis Pipeline

Every tick flows through this sequence:

```
IBKR TWS
   │
   ▼
TickStream          — real-time tick-by-tick options data (asyncio queue)
   │
   ▼
FlowClassifier      — labels trade type: BLOCK / SWEEP / SPLIT / MULTI_LEG / SINGLE
   │
   ▼
GreeksEngine        — enriches with delta, gamma, theta, vega, IV, moneyness, DTE
   │
   ├──▶ SentimentAggregator   — rolling P/C ratios, net premium, GEX/DEX per symbol
   │
   ├──▶ UnusualDetector       — flags by PREMIUM_SIZE / OI_RATIO / SIGNAL_STRENGTH / OTM_PREMIUM
   │       └──▶ AlertRules ──▶ Notifier (Discord / email)
   │
   └──▶ SmartMoneyDetector    — scores institutional signals (SWEEP / BIG_OTM / NEAR_EXPIRY / etc.)
           └──▶ AlertRules ──▶ Notifier (Discord / email)

All enriched trades and unusual signals → SQLite / PostgreSQL
Dashboard reads DB + SharedState for live UI updates
```

---

## Project Structure

```
options-flow-analysis/
├── config/
│   └── settings.py              # Pydantic settings (loaded from .env)
├── src/
│   ├── connection/
│   │   ├── ibkr_client.py       # TWS connect / disconnect / health check
│   │   └── rate_limiter.py      # Async rate limiter (48 msg/sec, 55 hist/10 min)
│   ├── data/
│   │   ├── scanner.py           # IBKR market scanners (unusual volume, IV gainers)
│   │   ├── chain_fetcher.py     # Option chain snapshots per underlying
│   │   └── tick_stream.py       # Real-time tick-by-tick options data
│   ├── analysis/
│   │   ├── flow_classifier.py   # Classify trades: sweep / block / split / multi-leg
│   │   ├── unusual_detector.py  # Flag anomalies (vol vs OI, premium size, signal strength)
│   │   ├── greeks_engine.py     # IV / Greeks layer (IBKR + Black-Scholes fallback)
│   │   ├── sentiment.py         # Rolling P/C ratios, net premium, directional bias
│   │   └── smart_money.py       # Institutional heuristics and confidence scoring
│   ├── storage/
│   │   ├── models.py            # SQLAlchemy models (option_contracts, ticks, trades, signals)
│   │   ├── db.py                # Async DB engine, session management
│   │   └── queries.py           # Common query patterns (insert, load, query recent)
│   ├── alerts/
│   │   ├── rules.py             # Alert trigger conditions and severity mapping
│   │   └── notifier.py          # Discord webhook, email stub
│   ├── dashboard/
│   │   ├── app.py               # Dash app entry point
│   │   ├── layouts.py           # Page layouts
│   │   ├── callbacks.py         # Live-updating callbacks (sentiment, signals, trades, alerts)
│   │   └── shared_state.py      # Thread-safe bridge between asyncio pipeline and Dash
│   └── utils/
│       ├── formatting.py        # Display helpers, currency, Greek symbols
│       ├── market_hours.py      # Market calendar, session awareness
│       ├── validators.py        # Price, strike, expiry, IV, delta validation
│       └── watchlist.py         # WatchlistManager (JSON-backed CRUD, hot-reload)
├── scripts/
│   ├── run_scanner.py           # Entry: headless flow scanner
│   ├── run_dashboard.py         # Entry: Dash UI + pipeline
│   └── backfill.py              # Backfill historical OI / volume
├── tests/                       # pytest test suite (545+ tests)
├── .env.example
└── pyproject.toml
```

---

## Testing

```bash
# Run all unit tests (integration tests excluded by default)
pytest

# Run with verbose output
pytest -v

# Run a specific module
pytest tests/test_flow_classifier.py

# Include integration tests (requires live TWS connection)
pytest -m integration
```

Tests are in `tests/` with a corresponding file for each source module. Integration tests require an active TWS/Gateway connection and are skipped in CI.

---

## Key Concepts

| Term | Meaning |
|---|---|
| **Block trade** | Single large print — >= 500 contracts (configurable) |
| **Sweep** | Aggressive order hitting multiple exchanges rapidly |
| **Unusual activity** | Volume significantly exceeds open interest or average daily volume |
| **Premium** | Total dollar value: contracts x price x 100 |
| **Smart money signal** | Large premium + near-expiry + OTM — potential informed bet |
| **Put/Call ratio** | Aggregate sentiment gauge; extreme readings are contrarian |
| **IV skew** | Relative IV between OTM puts vs calls — reveals hedging demand |
| **GEX** | Gamma exposure — useful for pinning and support/resistance levels |
| **DEX** | Delta exposure — aggregate directional bias across all tracked trades |

---

## IBKR Notes

- TWS or IB Gateway must be running locally before starting the scanner or dashboard
- Paper trading port: **7497** — Live trading port: **7496**
- `IBKR_READONLY=true` prevents accidental order placement
- The live options data package subscription is required for real-time tick data
- Hard rate limits: 50 msg/sec, 60 historical requests/10 min — the built-in rate limiter stays 2 below each
- Maximum 100 concurrent market data lines — the pipeline enforces a 95-contract cap

---

## Alerts

Configure `DISCORD_WEBHOOK_URL` in `.env` to receive alerts:

| Severity | Condition |
|---|---|
| HIGH | Premium-size unusual signal; smart money confidence >= 0.70 |
| MEDIUM | OI-ratio or OTM-premium signal; smart money confidence >= 0.50 |
| LOW | Signal-strength signal; smart money confidence < 0.50 |

Discord embeds include symbol, trade details, reason, and timestamp.

---

## Production Deployment

Switch to PostgreSQL by updating `DATABASE_URL`:

```ini
DATABASE_URL=postgresql://user:password@host:5432/options_flow
```

The storage layer auto-selects `asyncpg` for PostgreSQL and `aiosqlite` for SQLite — no code changes required.
