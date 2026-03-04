# Options Flow Analysis — Project Blueprint

## Architecture at a Glance

```
IBKR TWS/Gateway (local)
        │
        ▼
┌──────────────────┐
│  Connection Layer │  ib_insync async client
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│    Data Layer     │  Chains, ticks, scanners, historical
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│  Storage Layer    │  SQLite → PostgreSQL, models, queries
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│  Analysis Layer   │  Classifier, anomaly detection, Greeks, sentiment
└──────┬───────────┘
       │
       ├──────────────────┐
       ▼                  ▼
┌─────────────┐   ┌──────────────┐
│   Alerts    │   │  Dashboard   │
│  (Discord,  │   │  (Dash /     │
│   email)    │   │   Plotly)    │
└─────────────┘   └──────────────┘
```

---

## Enhancement Roadmap

### Phase 1 — Foundation Enhancements
- **Watchlist manager**: YAML/JSON file of tickers + per-ticker config (thresholds, expiry range)
- **Reconnection logic**: auto-reconnect on TWS disconnect with exponential backoff
- **Data validation layer**: pydantic models between raw IBKR objects and your pipeline to catch malformed data early
- **Replay mode**: save raw tick streams to parquet files so you can replay market sessions offline for development and testing without needing a live connection

### Phase 2 — Analytical Depth
- **IV surface modeling**: build a volatility surface per underlying (strike × expiry); detect skew shifts and term structure changes
- **GEX/DEX calculation**: aggregate gamma and delta exposure across all strikes to identify mechanical support/resistance and potential pin levels
- **Multi-leg detection**: identify spreads, straddles, strangles, and condors by correlating near-simultaneous trades on the same underlying
- **Dark pool correlation**: cross-reference options flow with FINRA dark pool short volume data for confirmation signals
- **Earnings event overlay**: flag flow that coincides with upcoming earnings, FDA dates, or FOMC — context makes flow far more actionable

### Phase 3 — Intelligence Layer
- **ML anomaly scoring**: train an isolation forest or autoencoder on historical flow to score how "unusual" each trade truly is, rather than relying on static thresholds
- **Sector rotation tracker**: aggregate flow across sector ETFs (XLF, XLE, XLK, etc.) to visualize where institutional money is rotating
- **Whale tracker**: fingerprint repeat large traders by patterns (same strikes, timing, size) — you can't identify them, but you can cluster their behavior
- **Backtester**: given historical flow signals, simulate what would have happened if you followed them — essential for validating any strategy built on top of flow data
- **Natural language summaries**: use an LLM to generate end-of-day plain-English flow summaries ("Today's notable flow: heavy call buying in NVDA Jan 150C, $12M premium, likely institutional sweep ahead of earnings")

### Phase 4 — Production Hardening
- **PostgreSQL + TimescaleDB**: time-series optimized storage for tick data at scale
- **Redis caching**: cache option chains, Greeks, and computed metrics to reduce API pressure
- **Containerization**: Docker compose for TWS Gateway + app + database
- **Monitoring**: Prometheus metrics for data freshness, connection health, processing latency
- **Rate limit manager**: centralized tracker to stay within IBKR's 50 msg/sec and pacing limits

---

## Step-by-Step Build Process

### Step 1 — Environment Setup
Set up the Python project with `pyproject.toml`, create the directory structure from the claude.md file, install core dependencies (`ib_insync`, `pandas`, `numpy`, `loguru`, `pydantic-settings`, `sqlalchemy`), and create the `.env.example` with IBKR connection defaults (host, port, client ID).

### Step 2 — Configuration Module
Build `config/settings.py` using pydantic-settings to load `.env` values. Define all tunables here: connection params, scanning thresholds, watchlist path, database URL, alert endpoints. Validate on startup.

### Step 3 — IBKR Connection
Build the connection client that wraps `ib_insync.IB()`. Implement connect, disconnect, health check, and a context manager pattern. Test by connecting to TWS paper trading and verifying you receive a valid `managedAccounts` response.

### Step 4 — Option Chain Fetching
Build the chain fetcher that takes an underlying symbol, qualifies the contract, and pulls the full option chain (all strikes, all expiries or a filtered range). Parse the response into clean dataclass/pydantic models. Test with one ticker (e.g., SPY) and inspect the output.

### Step 5 — Storage Layer
Define SQLAlchemy models for option trades, chain snapshots, and computed metrics. Set up SQLite for development. Write the insert and query patterns you'll need. Test by persisting a chain snapshot and reading it back.

### Step 6 — Live Tick Streaming
Build the tick streamer that subscribes to real-time option data for your watchlist. Handle the incoming ticks as events, normalize them into your data models, and persist to the database. Test by streaming SPY options for a few minutes and verifying data lands in the DB.

### Step 7 — Flow Classification
Build the classifier that labels each trade: block, sweep, split, or multi-leg. Use heuristics based on size, timing, exchange, and aggressor side (trade at bid vs ask). Test against your collected tick data.

### Step 8 — Unusual Activity Detection
Build the anomaly detector that compares current volume against open interest and average daily volume. Flag trades where premium, size, or volume ratios exceed configurable thresholds. Test by running it against a day's worth of stored data.

### Step 9 — Greeks and IV
Build the Greeks engine to calculate or retrieve delta, gamma, theta, vega, and implied volatility for each option. Use IBKR's model data where available, or compute via Black-Scholes as a fallback. This enriches every trade record with risk context.

### Step 10 — Sentiment Aggregation
Build the sentiment module that computes put/call ratios (by volume and premium), net directional premium, and bullish/bearish scoring at the ticker and market-wide level. Test by generating a sentiment snapshot for the current session.

### Step 11 — Smart Money Heuristics
Build the smart money module that combines trade size, premium, expiry proximity, moneyness, and sweep detection to score each trade's likelihood of being institutional. This is the signal layer — the core value of the project.

### Step 12 — Alerts
Build the alert rules engine (configurable conditions like "premium > $1M AND sweep AND OTM") and the notifier (start with Discord webhook — it's the easiest). Test by triggering a mock alert.

### Step 13 — Dashboard
Build the Dash application with a live flow table, filtering controls, sentiment gauges, and a chart of premium flow over time. Wire it to the database and optionally to the live stream. This is the visualization and interaction layer.

### Step 14 — Scanners and Entry Points
Build the IBKR market scanner integration to discover tickers with unusual options activity without needing a predefined watchlist. Create the entry-point scripts (`run_scanner.py`, `run_dashboard.py`) that tie everything together.

### Step 15 — Testing and Iteration
Write integration tests that use IBKR paper trading. Run the full pipeline during market hours, identify gaps, tune thresholds, and iterate on the classifier and anomaly detector based on real data.
