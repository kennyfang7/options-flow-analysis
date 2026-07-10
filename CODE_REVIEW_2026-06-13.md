# Code Review #2 — 2026-06-13

Second full-codebase review (`src/`, `scripts/` — 36 files). All items from `CODE_REVIEW_2026-06-12.md`
are excluded (already fixed). Work through in the suggested order; check off as you go. After each fix:

```bash
python -m pytest -q -m "not integration"
```

**Suggested fix order:** C1 → C2 → C3 → H1–H13 → M1–M16 → T1–T10 → Low

---

## Summary Table

| ID | Severity | File(s) | Description |
|----|----------|---------|-------------|
| C1 ✓ | Critical | `rate_limiter.py:39–54, 85–100` | No asyncio.Lock on TokenBucket/SlidingWindow — concurrent coroutines race through rate-limit checks |
| C2 ✓ | Critical | `chain_fetcher.py:348` | `or`-chain treats `0.0` price as falsy — falls through to stale close price |
| C3 ✓ | Critical | `greeks_engine.py:222` | `date.today()` uses local wall clock, not UTC — corrupts DTE for all BS Greeks |
| H1 ✓ | High | `queries.py:263–267` | `get_recent_ticks` compares aware UTC datetime against naive-stored values — always returns empty |
| H2 ✓ | High | `queries.py:235–239` | `load_chain_snapshot` passes naive `captured_at` back into pipeline — TypeError on comparison |
| H3 ✓ | High | `flow_classifier.py:369` | `ml_group` key non-unique for rapid same-symbol strategies — causes leg merging |
| H4 ✓ | High | `flow_classifier.py:368` | `ml_net_premium` polluted by unrelated strategy premiums in same window |
| H5 ✓ | High | `tick_stream.py:351–357` | `unsubscribe()` unhooks without `_hook_lock` — TOCTOU race with `subscribe()` |
| H6 ✓ | High | `ibkr_client.py:107, 221` | `connect()` resets `_intentional_disconnect=False` during reconnect — breaks graceful shutdown |
| H7 ✓ | High | `scanner.py:9, 103` | Settings imported at module level — breaks test isolation, inconsistent with all other components |
| H8 ✓ | High | `historical.py:40–61` | `HistoricalBar.timestamp` has no tz-aware validator — naive datetimes accepted silently |
| H9 ✓ | High | `greeks_engine.py:187–194` | Newton-Raphson IV solver has no upper-bound clamp — extreme sigma wastes iterations |
| H10 ✓ | High | `sentiment.py:160` | `update()` prunes on stale `trade.timestamp` — replay bursts inflate window |
| H11 ✓ | High | `earnings.py:69, 138` | `date.today()` is timezone-naive — wrong near UTC midnight |
| H12 ✓ | High | `watchlist.py:177–198` | `save()` non-atomic write + uncaught OSError — data loss on disk error |
| H13 ✓ | High | `historical.py:314–319` | `_parse_bar_date` string fallback raises bare ValueError — drops entire symbol fetch |
| M1 ✓ | Medium | `db.py:125–136` | Async engine singleton `_get_engine` has no lock (sync counterpart does) |
| M2 ✓ | Medium | `rules.py:359` | `net_prem or sum(...)` treats zero premium as falsy — wrong for zero-debit spreads |
| M3 ✓ | Medium | `rules.py:106–155` | `MultiLegBuffer` stale groups never evicted — unbounded memory growth |
| M4 ✓ | Medium | `callbacks.py:186–209` | `_query_signal_rows` full table scan when no symbol filter — composite index unusable |
| M5 ✓ | Medium | `shared_state.py:113` / `callbacks.py:274` | `drain_alerts` hardcoded `max_count=50` diverges from `dashboard_max_alerts` |
| M6 ✓ | Medium | `callbacks.py:100, 120` | `.strftime()` on potentially-None datetimes — crashes callback silently |
| M7 ✓ | Medium | `flow_classifier.py:107` | `volume_delta: int` lacks `ge=1` Pydantic constraint |
| M8 ✓ | Medium | `flow_classifier.py:350–351` | `_symbol_recent` deques unbounded — potential memory growth under high-frequency ticks |
| M9 ✓ | Medium | `flow_classifier.py:173–201` | 3-leg strategies always return COMBO — undocumented limitation |
| M10 ✓ | Medium | `greeks_engine.py:391–404` | `model_dump()` + `**kwargs` EnrichedTrade construction fragile for future excluded fields |
| M11 ✓ | Medium | `rate_limiter.py:87` | Off-by-one: `<=` evicts timestamps at boundary — already fixed (strict `<` in code) |
| M12 ✓ | Medium | `scanner.py:225–252` | `_parse_scan_data` bare attribute access — AttributeError on malformed rows crashes scan |
| M13 ✓ | Medium | `chain_fetcher.py:452–458` | One rate-limit token per batch of 50 contracts — budget ~50x underestimated |
| M14 ✓ | Medium | `run_scanner.py:138` / `run_dashboard.py:178` | Scripts mutate `unusual._oi_cache` directly — breaks encapsulation |
| M15 | Medium | `run_dashboard.py:77–267` | `_pipeline()` is verbatim copy of `run_pipeline()` — maintenance divergence risk |
| M16 ✓ | Medium | `run_dashboard.py:284, 111` | `init_db()` called twice — second failure silently swallowed |

---

## CRITICAL

### [x] C1. No mutex protecting RateLimiter token bucket and sliding window

**File:** `src/connection/rate_limiter.py:39–54` (`_TokenBucket.consume`), `85–100` (`_SlidingWindow.consume`)

Both `_TokenBucket` and `_SlidingWindow` read-modify-write shared state (`self._tokens`,
`self._timestamps`) across `await asyncio.sleep()` yield points with **no `asyncio.Lock`**.
Two coroutines can both read `self._tokens = 1.2`, both pass the check, both subtract 1.0,
spending one token twice.

During burst activity (chain_fetcher batches + tick stream + scanner all firing at once),
multiple coroutines from the same event loop race through the token check.

**Why it matters:** This is the core safety mechanism for IBKR's 50 msg/sec and 60 hist/10-min
hard limits. A race means both limits can be silently violated → pacing errors (code 162) or
forced disconnect.

**Fix:**
```python
class _TokenBucket:
    def __init__(self, rate, capacity=None):
        ...
        self._lock = asyncio.Lock()

    async def consume(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)  # sleep OUTSIDE the lock
```

Apply the same pattern to `_SlidingWindow.consume()`.

---

### [x] C2. `_get_underlying_price` treats `0.0` as falsy via `or`-chain

**File:** `src/data/chain_fetcher.py:348`

```python
price = _clean(ticker.midpoint()) or _clean(ticker.last) or _clean(ticker.close)
```

`_clean()` returns `0.0` as-is (not `None`), but `0.0` is falsy in Python. If `midpoint()`
returns `0.0` (IBKR emits this for illiquid pre-market snapshots), the chain falls through to
`last` or `close`. A genuine `0.0` midpoint is skipped and a stale `close` from a prior
session is used instead.

**Why it matters:** `underlying_price` drives the strike filter
(`low = underlying_price * (1 - strike_range_pct)`). A wrong price means wrong strikes are
fetched — the pipeline ingests garbage options.

**Fix:**
```python
def _first_valid_price(*values: float | None) -> float | None:
    """Return the first value that is not None and strictly positive."""
    for v in values:
        if v is not None and v > 0.0:
            return v
    return None

price = _first_valid_price(
    _clean(ticker.midpoint()),
    _clean(ticker.last),
    _clean(ticker.close),
)
```

---

### [x] C3. `_days_to_expiry` uses `date.today()` (local wall clock) instead of UTC

**File:** `src/analysis/greeks_engine.py:212–223`

```python
delta = (exp_date - date.today()).days   # local wall clock
```

`date.today()` returns the system's local date, which on a UTC+X server diverges from UTC.
This cascades into every BS Greek via `T = T_days / 365.0`. For 0DTE options where 1 day =
100% error in `T`, all Greeks (delta, gamma, theta, vega, IV) are wrong.

Also affects `SmartMoneyDetector.score()` for the `NEAR_EXPIRY_OTM` check — false positives
when DTE appears one day premature. The `max(delta, 0)` guard silently turns expired options
into `T=0` → `bs_available = False` → all BS Greeks suppressed without any log.

**Fix:**
```python
from datetime import timezone

def _days_to_expiry(expiry: str) -> int:
    exp_date = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
    today_utc = datetime.now(timezone.utc).date()
    delta = (exp_date - today_utc).days
    return max(delta, 0)
```

---

## HIGH

### [x] H1. `get_recent_ticks` aware-vs-naive datetime mismatch — always returns empty

**File:** `src/storage/queries.py:263–267`

```python
since = datetime.now(timezone.utc) - timedelta(minutes=minutes)   # aware
# ...
.where(OptionTick.received_at >= since)   # DB stores naive (via _to_naive_utc)
```

The write path (`insert_tick`) strips timezone via `_to_naive_utc()`. Stored rows have naive
UTC strings like `"2026-06-13 14:30:00"`. But `since` is aware: `"2026-06-13 14:30:00+00:00"`.
SQLite does lexicographic string comparison: the `+00:00` suffix makes every stored tick sort
before `since` → **the query always returns empty**.

**Why it matters:** Flow classifier uses this to look up recent activity. Empty results mean
no history → under-classification (never escalating to SWEEP, SPLIT).

**Fix:**
```python
since = _to_naive_utc(datetime.now(timezone.utc) - timedelta(minutes=minutes))
```

---

### [x] H2. `load_chain_snapshot` passes naive `captured_at` into pipeline

**File:** `src/storage/queries.py:235–239`

```python
snapshot = OptionChainSnapshot(
    timestamp=row.captured_at,   # naive UTC from DB — no tzinfo
)
```

The staleness check at line 193-194 re-attaches timezone to a local `captured_at` variable,
but `row.captured_at` itself remains naive. Any downstream comparison against
`datetime.now(timezone.utc)` raises `TypeError: can't compare offset-naive and offset-aware`.

**Fix:** Use the already-corrected local variable:
```python
timestamp=captured_at,   # the aware UTC local from line 194
```

---

### [x] H3. `ml_group` key non-unique for rapid same-symbol strategies

**File:** `src/analysis/flow_classifier.py:369`

```python
ml_group = f"{tick.symbol}:{sym_win[0][1].isoformat()}"
```

Keyed by symbol + oldest entry timestamp. If two unrelated multi-leg strategies on the same
symbol occur within `multi_leg_window_seconds`, both share the same key →
`MultiLegBuffer._groups` merges all legs → `_classify_multi_leg_strategy` gets a mixed list
→ produces nonsensical result.

**Fix:** Include the current tick's `con_id` in the key:
```python
ml_group = f"{tick.symbol}:{sym_win[0][1].isoformat()}:{tick.con_id}"
```
Or generate a monotonically unique ID.

---

### [x] H4. `strategy_net_premium` polluted by unrelated strategies in window

**File:** `src/analysis/flow_classifier.py:368`

```python
ml_net_premium = sum(p for _, _, p in sym_win)  # ALL legs in window
```

`sym_win` contains *all* entries within `multi_leg_window_seconds`, not just the current
strategy's legs. Combined with H3, premiums from unrelated strategies pollute the sum.

**Fix:** Compute net premium at alert time from the buffered legs (which `rules.py:359`
already does as a fallback). Remove or deprecate the per-tick `strategy_net_premium`.

---

### [x] H5. `unsubscribe()` unhooks without `_hook_lock` — TOCTOU race

**File:** `src/data/tick_stream.py:351–357`

`subscribe()` acquires `_hook_lock` before hooking, but `unsubscribe()` checks
`self._event_hooked` and removes the handler **without** acquiring the lock. Concurrent
subscribe+unsubscribe can result in duplicate handler registration → double-counted ticks.

**Fix:**
```python
async with self._hook_lock:
    if self._event_hooked and not self._subscriptions:
        try:
            self._ib.pendingTickersEvent -= self._on_pending_tickers
        except ValueError:
            logger.warning("pendingTickersEvent handler was not registered")
        self._event_hooked = False
```

---

### [x] H6. `connect()` resets `_intentional_disconnect=False` during reconnect

**File:** `src/connection/ibkr_client.py:107, 221`

`connect()` unconditionally sets `_intentional_disconnect = False`. When called from
`_reconnect_with_backoff`, if a concurrent `disconnect()` set the flag to `True`, `connect()`
resets it → the system enters an infinite reconnect loop during planned shutdown.

**Fix:**
```python
async def connect(self, *, _from_reconnect: bool = False) -> None:
    if not _from_reconnect:
        self._intentional_disconnect = False
    ...

# In _reconnect_with_backoff:
await self.connect(_from_reconnect=True)
```

---

### [x] H7. `scanner.py` imports settings at module level — breaks test isolation

**File:** `src/data/scanner.py:9, 103`

Unlike every other component (ibkr_client, chain_fetcher, rate_limiter, historical — all
lazy-load settings inside `__init__`), scanner binds the singleton at import time. Tests
cannot inject mock settings without monkeypatching at module level.

**Fix:** Follow the lazy-load pattern:
```python
class MarketScanner:
    def __init__(self, client, limiter=None, settings=None):
        if settings is None:
            from config.settings import settings as _settings
            settings = _settings
        self._settings = settings
```

---

### [x] H8. `HistoricalBar.timestamp` missing tz-aware validator

**File:** `src/data/historical.py:40–61`

`ScannerResult.scanned_at` has a `field_validator` rejecting naive datetimes. `HistoricalBar`
is missing the same guard. A naive timestamp propagates into `to_dataframe()` and crashes
any comparison against `datetime.now(timezone.utc)`.

**Fix:**
```python
@field_validator("timestamp")
@classmethod
def timestamp_must_be_tz_aware(cls, v):
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (use UTC)")
    return v
```

---

### [x] H9. Newton-Raphson IV solver has no upper-bound sigma clamp

**File:** `src/analysis/greeks_engine.py:187–194`

The lower bound is clamped to `1e-6`, but there is no upper bound. A bad initial guess can
overshoot to `sigma = 50.0` (5000% IV), wasting iterations on a nonsensical trajectory.

**Fix:**
```python
sigma -= (bs - price) / raw_vega
sigma = max(1e-6, min(sigma, 10.0))  # IV > 1000% is not physical
```

---

### [x] H10. `SentimentAggregator.update()` prunes on `trade.timestamp`, not wall clock

**File:** `src/analysis/sentiment.py:147–160`

Delayed ticks (e.g., from reconnect buffer replay) use old timestamps as the prune reference,
causing stale trades to persist in the window until the next `snapshot()` call.

**Fix:**
```python
reference = max(trade.timestamp, datetime.now(timezone.utc))
self._prune(symbol, reference)
```

---

### [x] H11. `EarningsCalendar` uses `date.today()` — timezone-naive

**File:** `src/utils/earnings.py:69, 138`

Same class of bug as C3. `date.today()` returns the local system date. Near UTC midnight,
`days_to_earnings` can be wrong, and `_fetch_yfinance` can filter out same-day earnings.

**Fix:**
```python
def _today_utc() -> date:
    return datetime.now(timezone.utc).date()
```
Apply at lines 69 and 138.

---

### [x] H12. `WatchlistManager.save()` non-atomic write + uncaught OSError

**File:** `src/utils/watchlist.py:177–198`

`Path.write_text()` raises `OSError` on write failure (full disk, permissions). Not caught.
Also non-atomic — crash mid-write produces truncated file.

**Fix:** Write to `.tmp` then `os.replace()`:
```python
tmp_path = save_path.with_suffix(".json.tmp")
try:
    tmp_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(save_path)
except OSError:
    logger.exception("Failed to save watchlist to {}", save_path)
    tmp_path.unlink(missing_ok=True)
    raise
```

---

### [x] H13. `_parse_bar_date` string fallback raises bare ValueError

**File:** `src/data/historical.py:314–319`

Unexpected IBKR date format (e.g., `"20250612 15:30:00 US/Eastern"`) raises `ValueError`
from `strptime` with no context. One bad bar drops the entire symbol's fetch.

**Fix:** Wrap in try/except inside `_parse_bar`:
```python
def _parse_bar(self, bar) -> HistoricalBar | None:
    try:
        timestamp = _parse_bar_date(bar.date)
    except (ValueError, AttributeError) as exc:
        logger.warning("Skipping malformed bar {!r}: {}", bar, exc)
        return None
```
Then filter `None` in `fetch_bars()`.

---

## MEDIUM

### [x] M1. Async engine singleton lacks lock (sync counterpart has one)

**File:** `src/storage/db.py:125–136`

`_get_engine()` and `_get_session_factory()` use lazy-init with no lock. `get_sync_engine()`
correctly uses double-checked locking. Inconsistent — two engines can be created if
`get_session()` is called from multiple paths during startup.

**Fix:** Mirror the sync pattern with `threading.Lock()` + double-checked locking.

---

### [x] M2. `net_prem or sum(...)` treats zero premium as falsy

**File:** `src/alerts/rules.py:359`

```python
net_prem = lead.strategy_net_premium or sum(t.premium or 0.0 for t in trades)
```

A zero-debit spread (`strategy_net_premium == 0.0`) falls through to the sum of individual
leg premiums, which can be very large → alert incorrectly escalated from LOW to HIGH.

**Fix:**
```python
net_prem = (
    lead.strategy_net_premium
    if lead.strategy_net_premium is not None
    else sum(t.premium or 0.0 for t in trades)
)
```

---

### [x] M3. `MultiLegBuffer` stale groups never evicted — unbounded memory growth

**File:** `src/alerts/rules.py:106–155`

Groups where the strategy was never completed (cancelled, connection lost) sit in `_groups`
forever. No `purge_stale()` method exists.

**Fix:** Add a `purge_stale(max_age_seconds=3600.0)` method, call it from the orchestration
layer alongside the other `purge_stale()` calls.

---

### [x] M4. Dashboard queries do full table scan when no symbol filter applied

**File:** `src/dashboard/callbacks.py:186–209`

The composite index `ix_unusual_signals_symbol_flagged_at` on `(symbol, flagged_at)` cannot
be used for `ORDER BY flagged_at DESC` without the leading column fixed.

**Fix:** Add single-column indexes in `models.py`:
```python
Index("ix_unusual_signals_flagged_at", "flagged_at"),
Index("ix_classified_trades_classified_at", "classified_at"),
```

---

### [x] M5. `drain_alerts` hardcoded `max_count=50` — lags behind bursts

**File:** `src/dashboard/shared_state.py:113`, `src/dashboard/callbacks.py:274`

Queue is sized to `settings.dashboard_max_alerts` (200), but each drain pulls at most 50.
Burst of >50 alerts causes backlog → new alerts dropped.

**Fix:** `state.drain_alerts(max_count=settings.dashboard_max_alerts)`

---

### [x] M6. `.strftime()` on potentially-None datetimes in callbacks

**File:** `src/dashboard/callbacks.py:100, 120`

`flagged_at.strftime(...)` and `classified_at.strftime(...)` crash with `AttributeError` if
the value is `None` (possible with schema-migrated rows).

**Fix:** `r.flagged_at.strftime("%H:%M:%S") if r.flagged_at is not None else "—"`

---

### [x] M7. `ClassifiedTrade.volume_delta` lacks `ge=1` constraint

**File:** `src/analysis/flow_classifier.py:107`

`volume_delta: int` has no Pydantic constraint. The classifier guarantees `> 0` at creation,
but direct construction (tests, future code) can pass negative values, inverting metrics.

**Fix:** `volume_delta: int = Field(ge=1)`

---

### [x] M8. `_symbol_recent` deques unbounded — memory growth risk

**File:** `src/analysis/flow_classifier.py:350–351`

Per-contract windows use `deque(maxlen=500)`. `_symbol_recent` deques have no `maxlen`.

**Fix:** `self._symbol_recent[tick.symbol] = deque(maxlen=1000)`

---

### [x] M9. 3-leg strategies always return COMBO — undocumented

**File:** `src/analysis/flow_classifier.py:173–201`

`_classify_multi_leg_strategy` has branches for `n == 2` and `n == 4` but not `n == 3`.
Common butterflies are never specifically identified. Not a bug, but should be documented.

**Fix:** Add to docstring: "3-leg strategies always return COMBO — butterflies and other 3-leg
structures are not specifically identified."

---

### [x] M10. `EnrichedTrade` construction via `model_dump()` is fragile

**File:** `src/analysis/greeks_engine.py:391–404`

`Field(exclude=True)` on `ClassifiedTrade.tick` means `tick` is absent from `model_dump()`.
It's re-injected manually. Future `exclude=True` fields must also be re-injected.

**Fix:** Document the contract or add a factory class method on `EnrichedTrade`.

---

### [x] M11. Sliding window off-by-one: `<=` evicts boundary timestamps

**File:** `src/connection/rate_limiter.py:87`

```python
while self._timestamps and self._timestamps[0] <= cutoff:
```

A request at exactly `t - 600.0s` is evicted but should still count. With the 55/60 margin
this is unlikely to trigger real pacing errors.

**Fix:** `< cutoff` (strict less-than).

---

### [x] M12. `_parse_scan_data` crashes on malformed rows

**File:** `src/data/scanner.py:225–252`

Bare attribute access on `object`-typed `raw`. Any unexpected shape → `AttributeError`.
One bad row kills all valid rows in the scan.

**Fix:** Wrap in try/except, return `None` for malformed rows, filter in caller.

---

### [x] M13. One rate-limit token per batch of 50 contracts

**File:** `src/data/chain_fetcher.py:452–458`

`qualifyContractsAsync(*batch)` with 50 contracts may send 50 individual
`reqContractDetails` messages. Only 1 token consumed. Need to verify against ib_insync source
whether it's 1 message or N messages.

**Fix:** If N messages: acquire N tokens. If 1 message: document the assumption.

---

### [x] M14. Scripts mutate `unusual._oi_cache` directly

**File:** `scripts/run_scanner.py:138–139`, `scripts/run_dashboard.py:178–179`

Both scripts reach into the private `_oi_cache` dict. Bypasses encapsulation and future
locking.

**Fix:** Add a public `seed_oi_cache(contracts)` method to `UnusualDetector`.

---

### [ ] M15. `_pipeline()` is verbatim copy of `run_pipeline()`

**File:** `scripts/run_dashboard.py:77–267` vs `scripts/run_scanner.py:34–208`

Near-identical code. Any bug fix must be manually mirrored. Already diverged once (different
log messages). This is a significant refactor — consider extracting shared pipeline logic
with callback injection.

---

### [x] M16. `init_db()` called twice — second failure silently swallowed

**File:** `scripts/run_dashboard.py:284, 111`

Called in `__main__` (correctly raises on failure) and again in `_pipeline()` (second failure
caught by generic except, displayed as status string). Remove the `_pipeline()` call.

---

## TESTS — Coverage Gaps

### [x] T1. `load_chain_snapshot` staleness branches untested (Critical)

**File:** `src/storage/queries.py:151`

Three return-None paths: no row, wrong calendar day, older than `max_age_hours`. Only "no row"
is tested. A stale snapshot served as fresh would silently feed bad data into the pipeline.

**Test approach:** Insert a snapshot, manipulate `captured_at` to yesterday and past max age,
assert `None` returned. Three parametrized test cases.

---

### [x] T2. `db.py` PostgreSQL URL adaptation branch has zero coverage (Critical)

**File:** `src/storage/db.py:19`

`_adapt_url("postgresql://...")` → `"postgresql+asyncpg://..."` is completely untested. A
typo would only surface at production deployment.

**Test approach:** Pure unit tests — call `_adapt_url()` and `_strip_async_prefix()` with
PostgreSQL URLs and assert the transformations.

---

### [x] T3. `historical.py` — `fetch_bars` validation, `_parse_bar_date`, `to_dataframe` untested (High)

Multiple entirely-untested functions: invalid `bar_size`/`what_to_show` → `ValueError`,
`_parse_bar_date` with all input types (datetime, date, various string formats),
`HistoricalBars.to_dataframe()` with empty bars list and populated bars,
`avg_daily_volume()` with all-None volumes.

---

### [x] T4. `get_recent_ticks` and `get_latest_snapshot` have no unit tests (High)

**File:** `src/storage/queries.py:248, 130`

Neither function is exercised in `test_storage.py`. `get_recent_ticks` feeds the flow
classifier. A wrong time filter would silently under/over-count window ticks.

**Test approach:** Insert rows with known timestamps, call with time windows, assert correct
rows returned.

---

### [x] T5. `rules.py` — earnings body lines in `evaluate_unusual` and `evaluate_smart_money` untested (High)

**File:** `src/alerts/rules.py:223, 303`

No test passes a trade with `days_to_earnings` set. The three-branch DTE block
(`dte == 0`, `dte <= pre_earnings_days`, `dte > threshold`) is never entered.

**Test approach:** Three parametrized tests per evaluate function with `days_to_earnings`
values of 0, 3, and 30.

---

### [x] T6. `shared_state.py` — `update_pipeline_status` / `get_pipeline_status` untested (High)

Never called in `test_dashboard.py`. The primary bridge between pipeline health and the
dashboard status indicator.

---

### [x] T7. `callbacks.py` — formatting helpers `_fmt_ratio`, `_fmt_dollars`, `_fmt_pct` untested (High)

**File:** `src/dashboard/callbacks.py:26`

Three private helpers with None branches (`"—"` return) and formatting logic. Only exercised
indirectly through snapshot values.

---

### [x] T8. `db.py` — `init_db()` SQLite WAL-mode pragma not verified (Medium)

No test confirms the `PRAGMA journal_mode=wal` is issued for SQLite.

---

### [x] T9. `queries.py` — `_to_naive_utc()` with non-UTC aware datetime untested (Medium)

The conversion from e.g., US/Eastern to naive UTC is never directly tested.

---

### [x] T10. `historical.py` — `_qualify_underlying()` failure paths untested (Medium)

`ValueError` when IBKR returns empty list or `conId == 0` — no test coverage.

---

## LOW

### [ ] L1. `iv_source: str` should be `Literal["ibkr", "black_scholes", "unavailable"]`
**Files:** `greeks_engine.py:287`, `smart_money.py:135`

### [ ] L2. `SentimentSnapshot.call_volume/put_volume` lack `ge=0` constraint
**File:** `sentiment.py:77–78`

### [ ] L3. `_all_same_aggressor` all-neutral behavior deserves call-site comment
**File:** `flow_classifier.py:121–132`

### [ ] L4. `OptionChainSnapshot.underlying_price` lacks `gt=0` Pydantic constraint
**File:** `chain_fetcher.py:144`

### [ ] L5. `historical.py` `duration` parameter not validated before IBKR call
**File:** `historical.py:155–219`

### [ ] L6. `ibkr_client.py` module-level singleton construction is import-time side effect
**File:** `ibkr_client.py:241`

### [ ] L7. `Notifier.send` fires discord/email sequentially, not concurrently
**File:** `notifier.py:60–61` — use `asyncio.gather` when email is implemented.

### [ ] L8. `Alert.metadata: dict[str, Any]` not enforced as JSON-serializable
**File:** `rules.py:78`

### [ ] L9. `ZoneInfo("America/New_York")` constructed per-call in `load_chain_snapshot`
**File:** `queries.py:175` — hoist to module-level constant.

### [ ] L10. `formatting.py` and `market_hours.py` are empty stubs — formatting duplicated in `rules.py`
**Files:** `src/utils/formatting.py`, `src/utils/market_hours.py`

### [ ] L11. `_SYMBOL_RE` rejects valid IBKR dot-notation tickers (e.g. BRK.B)
**File:** `watchlist.py:32`

### [ ] L12. `backfill.py` — no per-symbol progress logging for large watchlists
**File:** `scripts/backfill.py:74–86`

### [ ] L13. `backfill.py` — CLI symbols not validated against ticker regex before IBKR connection
**File:** `scripts/backfill.py:42`

### [ ] L14. Deferred `EarningsCalendar` import intent not commented
**Files:** `run_scanner.py:78`, `run_dashboard.py:121`

### [ ] L15. `greeks_engine.py` redundant `from datetime import date as _date` in `__main__` block
**File:** `greeks_engine.py:421`

### [ ] L16. `validators.py` — `is_price_valid(0.0)=True` vs `is_strike_valid(0.0)=False` asymmetry undocumented
**File:** `validators.py:27–41`

### [ ] L17. Docstring mismatch: `_signal_record_to_row` says 6 keys, returns 7 (includes ErnDTE)
**File:** `callbacks.py:97–98`

### [ ] L18. `EarningsCalendar` concurrent prefetch() race — double-fetch, potential None-overwrites
**File:** `earnings.py:99–118` — Add in-flight dedup set for thundering-herd prevention.

---

## Verified FALSE ALARMS (do not "fix")

| Claim | Why it's wrong |
|---|---|
| `_clean()` float `==` comparison against `-1.0` is fragile | IBKR emits exactly `-1.0` (integer sentinel); `math.isnan` handles `nan` above it |
| `_on_disconnect` using `asyncio.get_running_loop()` is unsafe | ib_insync fires `disconnectedEvent` on the ib_insync loop; `get_running_loop()` is correct |
| `_reconnect_task` cancellation interferes with `disconnect()` | `CancelledError` propagates cleanly without interfering |
| `OptionContract.mid` returns `None` when bid/ask both `None` | Correct behavior — not a bug |
| `HistoricalBars.avg_daily_volume` division by zero | `volumes` is checked for non-empty before dividing |
| `FlowClassifier` window pruning off-by-one | Prune happens before classification; current tick is in window during classification |
| `premium = tick.last_size * effective_price * 100` when `effective_price=None` | Cannot happen: `effective_price` set unconditionally in prior branch |
| `_sizes_within_tolerance` median calculation | Standard formula; `median == 0` guard prevents div-by-zero |
| `purge_stale()` evicting an active entry | `_last_volume.pop(con_id, None)` is defensive; only confirmed stale entries evicted |
| `log1p(premium / s.min_premium)` when `min_premium == 0` | `min_premium_must_be_positive` validator prevents `<= 0` |
| `_classify_multi_leg_strategy` vertical vs diagonal detection | Correctly distinguishes by expiry count, strike count |
| `SharedState._sentiment` dict concurrent access | CPython GIL + atomic dict ops on immutable Pydantic values — sound |
| `push_alert` queue eviction race | `queue.Queue` is fully thread-safe; worst case is a second `Full` warning |
| `get_session()` exception suppression | Re-raises after rollback — correct |
| `insert_chain_snapshot` flush before FK use | `session.flush()` populates `db_snapshot.id` first — correct |
| `Alert(**model_dump())` round-trip in callbacks | Pydantic v2 coerces ISO strings back to datetime and `str,Enum` back to enum — correct |
| `_to_naive_utc` with already-naive input | Returns input unchanged — correct |
| WatchlistManager `save()` uses `json.dumps(default=str)` | Only Pydantic `model_dump(mode="json")` is passed — `default=str` never fires |
| `_load_txt()` doesn't set `_mtime` | Called from `load()` which sets `_mtime` at line 131 before delegating |
| Double `asyncio.run()` in `run_dashboard.py` | Different threads, different event loops — no conflict |
| `_SlidingWindow.consume` negative sleep duration | `max(wait, 0.0)` guard is correct |
| `_bs_gamma` with `S=0, sigma=0, T=0` | All guarded: `S > 0`, `T > 0` by `bs_available`, `sigma > 0` by enclosing check |

---

## Post-fix checklist
- [x] `python -m pytest -q -m "not integration"` — all green (675 passed)
- [x] Update `MEMORY.md` with new fixes
