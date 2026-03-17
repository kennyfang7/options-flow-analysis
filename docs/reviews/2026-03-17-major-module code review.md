# 2026-03-17 Major Module Code Review

Scope: modules prioritized by `claude.md` build order (`config`, `connection`, `data`, `analysis`, `storage`, `alerts`, `scripts`).

## Findings

---

### 1) **High** — Streaming analytics use a stale underlying price for the full subscription lifetime

**Status: Confirmed. Better fix available than recommended.**

**Where:** `src/data/tick_stream.py`

**Evidence:**
- `_subscriptions` dict stores `(ibkr_contract, underlying_price)` at subscribe-time (`line 228`).
- `_on_pending_tickers()` reads `_, underlying_price = self._subscriptions[con_id]` (`line 314`) — always the cached value.
- That stale price is passed to `_ticker_to_update()` and set as `underlying_price=underlying_price` on every `TickUpdate` (`line 364`).

**Why it matters:** `GreeksEngine` BS-fallback uses `underlying_price` to compute moneyness, Black-Scholes price, and all Greeks. `SmartMoneyDetector` uses moneyness and `GreeksEngine` output for scoring. `SentimentAggregator` uses delta/gamma for GEX/DEX. All of these can be silently wrong if the underlying moves significantly intraday.

**Recommended fix (from review):** Periodically refresh underlying prices by symbol/conId from a parallel subscription cache.

**Better fix:** IBKR's `modelGreeks` object (already accessed at `tick_stream.py:343`) contains an `undPrice` field — the exact underlying price the IBKR model used when computing that tick's Greeks. This is available per-tick with no additional subscriptions or polling overhead. `_ticker_to_update()` should prefer `greeks.undPrice` when available, falling back to the cached subscription price only when `modelGreeks` is absent. This is more accurate than any periodic refresh because it is the contemporaneous underlying price for that specific tick's computation.

**Proposed change (one line in `_ticker_to_update`):**
```python
# current (stale):
underlying_price=underlying_price,

# fix:
underlying_price=(_clean(greeks.undPrice) if greeks else None) or underlying_price,
```

---

### 2) **High** — Timestamp fields are timezone-aware in app code but persisted to timezone-naive DB columns

**Status: Confirmed. Fix is straightforward.**

**Where:** `src/storage/models.py` (all `DateTime` columns) and `src/storage/queries.py` (all `.replace(tzinfo=None)` calls).

**Evidence:**
- Every datetime column in `models.py` uses `DateTime` without `timezone=True` — `captured_at` (`line 28`), `received_at` (`line 97`), `classified_at` (`lines 156, 199`), `flagged_at` (`line 200`).
- `queries.py` manually strips tzinfo before writing: `line 146` (`get_recent_ticks` lookback), `line 188` (`insert_classified_trade`), `lines 228–229` (`insert_unusual_signal`).
- The strip in `get_recent_ticks` (`line 146`) also affects reads: the comparison `received_at >= since` works today on SQLite but would silently compare wrong values after a PostgreSQL migration if the column stores tz-aware timestamps.

**Why it matters:** The `.replace(tzinfo=None)` strip is a SQLite workaround that papers over the schema issue. It's already flagged as a TODO in the code. On PostgreSQL, `DateTime` without `timezone=True` maps to `TIMESTAMP WITHOUT TIME ZONE` — any DST-adjacent data or multi-timezone deployment produces incorrect ordering and window queries.

**Recommended fix:**
1. Change all `DateTime` columns to `DateTime(timezone=True)` in `models.py`.
2. Remove all `.replace(tzinfo=None)` calls in `queries.py` — pass timezone-aware datetimes directly.
3. SQLAlchemy handles both backends correctly: SQLite stores as UTC ISO string; PostgreSQL uses `TIMESTAMP WITH TIME ZONE`.

This is a breaking schema change — requires a `DROP` + `CREATE` or an `ALTER COLUMN` migration for any existing databases.

---

### 3) **Medium/High** — Integration tests requiring live IBKR run in default `pytest` execution

**Status: Confirmed. One-line fix.**

**Where:** `pyproject.toml`

**Evidence:**
- `[tool.pytest.ini_options]` defines the `integration` marker (`line 6`) but has no `addopts` key.
- Running plain `pytest` picks up all tests including `@pytest.mark.integration` ones, which immediately fail with a connection-refused error to `127.0.0.1:7497`.
- No CI job or environment guard separates the two test tiers.

**Why it matters:** Every developer without a running TWS/Gateway sees failures on every test run. This trains the team to ignore red test output, masking real regressions. It also blocks any CI pipeline that doesn't run a TWS instance.

**Recommended fix:** Add one line to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
addopts = "-m 'not integration'"
```
Integration tests are then opt-in: `pytest -m integration`. If CI is added later, create a separate job that sets `IBKR_HOST` and runs `pytest -m integration`.

---

## Summary

| # | Severity | Finding | Fix Complexity |
|---|----------|---------|----------------|
| 1 | High | Stale underlying price in `TickStream` | 1-line change in `_ticker_to_update` |
| 2 | High | Timezone-naive DB columns, tzinfo stripped at write | Schema migration + remove `.replace(tzinfo=None)` across `queries.py` |
| 3 | Medium/High | Integration tests not excluded from default `pytest` | 1-line `addopts` in `pyproject.toml` |

Finding 3 is the lowest-risk and highest-ROI fix — do it first. Finding 1 can be patched in minutes. Finding 2 is the most invasive (requires a migration) but eliminates an entire class of subtle time-window bugs before the project moves to PostgreSQL.

No silent exception swallowing was found in critical data/analysis paths; errors are logged with context throughout. The module architecture and async patterns are sound.
Scope: modules prioritized by `claude.md` build order (`config`, `connection`, `data`, `analysis`, `storage`, `alerts`, `scripts`).

## Findings

### 1) **High** — Streaming analytics use a stale underlying price for the full subscription lifetime
- **Where:** `TickStream.subscribe()` stores one `underlying_price` per contract at subscribe-time; `_ticker_to_update()` reuses that same cached value for every later tick.
- **Evidence:** subscription cache shape and assignment in `tick_stream.py`.
- **Why it matters:** premium-derived and risk-derived downstream signals (Greeks fallback, moneyness, gamma exposure, smart-money scoring) can drift materially intraday when the underlying moves, but the pipeline continues to evaluate trades against an old price.
- **Impact path:** `TickStream` → `FlowClassifier`/`GreeksEngine`/`SentimentAggregator`/`SmartMoneyDetector`.
- **Recommendation:** refresh underlying prices periodically (or per tick batch) by symbol/conId, or enrich ticks with live underlying prices from a lightweight parallel subscription cache.

### 2) **High** — Timestamp fields are timezone-aware in app code but persisted to timezone-naive DB columns
- **Where:** ORM models define all datetime columns as `DateTime` without `timezone=True`.
- **Evidence:** `captured_at`, `received_at`, `classified_at`, `flagged_at` (and related fields) in `storage/models.py`.
- **Why it matters:** the runtime consistently emits UTC-aware datetimes (`datetime.now(timezone.utc)`), but database schema drops/normalizes timezone context inconsistently by backend. This creates subtle ordering/window bugs (especially around DST/localtime assumptions) once data is queried outside the same process.
- **Impact path:** all time-window logic and historical analytics.
- **Recommendation:** migrate schema to `DateTime(timezone=True)` (or hard UTC epoch columns) and normalize read/write boundaries explicitly.

### 3) **Medium/High** — Integration tests requiring live IBKR run in default `pytest` execution
- **Where:** live tests are marked `@pytest.mark.integration`, but project config does not exclude them by default.
- **Evidence:** integration tests in `tests/test_connection.py`, `tests/test_chain_fetcher.py`, `tests/test_scanner.py`; `pyproject.toml` only defines marker metadata and no default `-m "not integration"`.
- **Why it matters:** standard CI/local runs fail in environments without a running TWS/Gateway process, masking real regressions and reducing trust in test outcomes.
- **Observed now:** default `pytest -q` fails with connection-refused errors to `127.0.0.1:7497`.
- **Recommendation:** set default pytest args to exclude integration tests (or guard with env flag), and run integration suite in a dedicated job/environment.

## Notes
- The rest of the reviewed core modules are generally well-structured and documented.
- No silent exception swallowing was found in critical data/analysis paths; errors are logged with context.