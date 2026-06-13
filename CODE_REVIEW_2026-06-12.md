# Code Review — 2026-06-12

Full-codebase review (src/, config/, scripts/, tests/ — 58 files, ~15.4k lines, 616 tests).
Every Critical/High item below was **verified against source** before inclusion. Work through
in the suggested order; check off as you go. After each fix run:

```bash
python -m pytest -q -m "not integration"
```

**Suggested fix order:** C1 → H2 → H1 → T1 → H3 → H4 → M1–M7 → T2/T3 → Low

---

## Model Recommendations

Use the cheapest model capable of the task. Rule of thumb:
- **Haiku** — single-file, mechanical changes (add a line, delete a line, swap a value, add assert)
- **Sonnet** — multi-file changes, coordination across modules, or anything requiring judgment
- **Opus** — not needed for any item here; all fixes are well-specified

| Issue | Recommended Model | Reason |
|---|---|---|
| C1 | **Sonnet** | ~8 files touched; must share one `RateLimiter` instance across constructors + scripts |
| H2 | **Haiku** | Add one `_to_naive_utc()` helper; apply in 4 call sites in same file |
| H1 | **Haiku** | One-line swap: `lead.window_ticks` → `len(trades)` |
| T1 | **Haiku** | Replace hardcoded `datetime(2026, ...)` with `datetime.now(timezone.utc)` in 2 test files |
| H3 | **Haiku** | 5-line expansion of a single unpacking statement in chain_fetcher |
| H4 | **Haiku** | Add a docstring sentence to `SentimentSnapshot`; no logic change |
| M1 | **Haiku** | One-line change: `self._send_email(alert)` → `await asyncio.to_thread(self._send_email, alert)` |
| M2 | **Haiku** | One-line change in ibkr_client.disconnect(); ordering matters — read the fix note |
| M3 | **Sonnet** | asyncio.Lock + 3 coordinated edits across subscribe/unsubscribe; race-condition reasoning |
| M4 | **Haiku** | Add 2 assert statements (one per file) after each `_PRIORITY` definition |
| M5 | **Haiku** | `TYPE_CHECKING` import pattern for `SharedState` type hint |
| M6 | **Haiku** | Add `_dropped_ticks` counter + read-only property |
| M7 | **Haiku** | Delete one dead import line in earnings.py |
| T2 | **Sonnet** | Move `make_tick`/`make_trade` to conftest; reconcile signature drift across 6 files |
| T3 | **Sonnet** | Write new tests across 6 scenarios in multiple test files |
| L1–L6 | **Haiku** | Comments, export list, docstring addition, one-line log fix |

---

## CRITICAL

### [x] C1. RateLimiter is never wired into any IBKR call path
**Files:** `src/data/chain_fetcher.py:316, 334, 355, 440, 467` · `src/data/tick_stream.py:280` · `src/data/scanner.py:146` · `scripts/run_scanner.py` · `scripts/run_dashboard.py` · `scripts/backfill.py`

CLAUDE.md mandates `await limiter.acquire()` before *any* IBKR call. The limiter exists and is
fully tested (`src/connection/rate_limiter.py`, 19 tests) but **zero call sites use it** —
confirmed by grep: no `acquire`/`RateLimiter` references in `src/data/` or `scripts/`.
Current pacing is only the arbitrary `asyncio.sleep()`s in `chain_fetcher` → risk of IBKR
pacing violations / forced disconnect under load.

**Fix:**
1. Add an optional constructor param to `ChainFetcher`, `TickStream`, `MarketScanner`:
   ```python
   def __init__(self, client: IBKRClient, limiter: RateLimiter | None = None) -> None:
       self._limiter = limiter or RateLimiter()
   ```
2. Call `await self._limiter.acquire()` immediately before each:
   - `qualifyContractsAsync` (chain_fetcher.py:316, 440 — the batch loop: one acquire **per batch call**, not per contract)
   - `reqTickersAsync` (chain_fetcher.py:334)
   - `reqSecDefOptParamsAsync` (chain_fetcher.py:355)
   - `reqScannerSubscriptionAsync` (scanner.py:146)
   - `reqMktData` (tick_stream.py:280 — sync call, but still counts as an outbound message; acquire before the loop iteration. `subscribe()` is already async so `await` is fine here)
3. Use `await self._limiter.acquire("historical")` before any future `reqHistoricalData` (none exist yet — `historical.py` is empty).
4. In scripts, construct **one shared** `RateLimiter()` and pass it to all three components — a per-component limiter defeats the purpose (the 48 msg/sec budget is per connection).

**⚠️ Public interface change:** constructor signatures. Keep `limiter` optional with a
default so existing tests construct unchanged; update tests to assert `acquire` is awaited
(mock `RateLimiter` with `AsyncMock`).

**Context / gotchas:**
- `RateLimiter.acquire(kind)` — `"general"` hits the token bucket only; `"historical"` hits sliding window **then** bucket. Unknown kind raises `ValueError`.
- Do NOT remove the `asyncio.sleep(0.1/0.5/2.0)` calls in `chain_fetcher.py:442–473` — those are for market-data settlement, not pacing (see L5: add a comment saying so).

---

## HIGH

### [x] H1. Wrong leg count in multi-leg alerts
**File:** `src/alerts/rules.py:361`

```python
n_legs = lead.window_ticks   # WRONG
```

`window_ticks` is the lead leg's classifier window tick count (`1 + len(prior_con_ids)` at
classify-time), not the number of legs in the buffered strategy group. The authoritative
list is the function argument.

**Fix:** `n_legs = len(trades)`

Propagates to: alert body line 376 (`"Strategy: X (N legs)"`), debug log line 385, and
`metadata["n_legs"]` line 398 — all fixed automatically by the one-line change.

**Test:** in `tests/test_alerts.py`, build a 4-leg group where the lead trade has
`window_ticks=1`, assert alert body and metadata say 4 legs.

---

### [x] H2. Datetime tzinfo stripping promised but not done on writes
**File:** `src/storage/queries.py:286, 327–328` (audit `insert_tick` / `insert_chain_snapshot` too)

`insert_unusual_signal`'s docstring (lines 299–300) claims "classified_at and flagged_at are
stored as naive UTC (tzinfo stripped)" — but the code passes **aware** datetimes through:

```python
classified_at=trade.timestamp,          # line 286 (insert_classified_trade)
classified_at=signal.trade.timestamp,   # line 327
flagged_at=signal.flagged_at,           # line 328
```

The read path (`queries.py:174`) assumes naive-stored values and re-attaches UTC. With
SQLite, datetimes serialize to ISO strings — mixing `"...+00:00"` (aware) and naive strings
breaks lexicographic `ORDER BY` / time-window filters used by the dashboard queries.

**Fix:** add one helper and apply on every insert path:
```python
def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC for SQLite storage (revisit for PostgreSQL)."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
```
Apply to: `classified_at` (both inserts), `flagged_at`, and audit `received_at`
(`insert_tick`) and `captured_at` (`insert_chain_snapshot`) for the same issue.

**Context:** MEMORY.md claims stripping was already in place — it is not; update MEMORY.md
when fixed. Note `.astimezone(utc)` first (don't just `.replace(tzinfo=None)`) so non-UTC
aware datetimes are converted correctly.

**Migration note:** existing DB rows may already contain aware ISO strings. Dev DB: simplest
to delete the SQLite file and let `init_db()` recreate. Otherwise a one-off
`UPDATE ... SET col = REPLACE(col,'+00:00','')` cleanup.

---

### [x] H3. Fragile single-element unpack in underlying price fetch
**File:** `src/data/chain_fetcher.py:334`

```python
[ticker] = await self._ib.reqTickersAsync(stock)
```

Raises a bare `ValueError: not enough values to unpack` on 0 results (or too many on 2+),
with no symbol context.

**Fix:**
```python
tickers = await self._ib.reqTickersAsync(stock)
if len(tickers) != 1:
    raise ValueError(
        f"Expected exactly 1 ticker for {symbol}, got {len(tickers)}"
    )
ticker = tickers[0]
```

**Test:** mock `reqTickersAsync` returning `[]`, assert the descriptive error.

---

### [x] H4. IV skew includes MULTI_LEG trades — inconsistent with other metrics
**File:** `src/analysis/sentiment.py:219–230`

Delta/gamma exposure (line 243: sign forced to 0.0) and bullish/bearish premium (lines 264,
272) all exclude `TradeType.MULTI_LEG`. The OTM IV lists do not:

```python
otm_call_ivs = [
    t.implied_vol for t in window
    if t.right == "C"
    and t.moneyness == Moneyness.OTM
    and t.implied_vol is not None      # no MULTI_LEG filter
]
```

**Decision needed:** MEMORY.md says "MULTI_LEG still counted in ... IV skew (only directional
metrics zeroed)" — so this *may* be intentional (IV skew is non-directional; a straddle leg's
IV is still a valid IV observation). Pick one:
- **(a) Keep behavior** → document it in the `SentimentSnapshot` docstring ("IV skew includes
  multi-leg legs; only directional metrics exclude them"). Lowest-risk option, consistent
  with original design intent.
- **(b) Exclude** → add `and t.trade_type != TradeType.MULTI_LEG` to both comprehensions,
  update affected tests in `tests/test_sentiment.py`, and update MEMORY.md.

Recommendation: **(a)** — IV is a property of the contract, not the order's direction.

---

## MEDIUM

### [x] M1. `_send_email` called synchronously on the event loop
**File:** `src/alerts/notifier.py:61`

```python
await asyncio.to_thread(self._send_discord, alert)  # correct
self._send_email(alert)                              # blocking call when SMTP lands
```

Its own docstring (line 110) says future SMTP must use `asyncio.to_thread`. Harmless today
(stub returns early), guaranteed event-loop stall later.

**Fix:** `await asyncio.to_thread(self._send_email, alert)` now, so the contract is already
honored when SMTP is implemented.

---

### [x] M2. Blocking `ib.disconnect()` inside async `disconnect()`
**File:** `src/connection/ibkr_client.py:121`

`IB.disconnect()` is synchronous socket teardown; can stall the loop if the socket is stuck.

**Fix:** `await asyncio.to_thread(self._ib.disconnect)`. Keep `_intentional_disconnect = True`
and the reconnect-task cancel **before** the to_thread call (ordering matters — the
disconnected-event handler checks the flag).

---

### [x] M3. TickStream event-hook register/unregister not defensive
**File:** `src/data/tick_stream.py:289–292, 330`

- `subscribe()` checks `if not self._event_hooked` but the method awaits between check and
  hook — two concurrent `subscribe()` calls can both pass the check and double-register
  (ib_insync handlers **stack**, they don't replace → duplicate ticks).
- `unsubscribe()`'s `self._ib.pendingTickersEvent -= self._on_pending_tickers` raises
  `ValueError` if the handler isn't actually registered.

**Fix:**
```python
# __init__:
self._hook_lock = asyncio.Lock()

# subscribe():
async with self._hook_lock:
    if not self._event_hooked and self._subscriptions:
        self._ib.pendingTickersEvent += self._on_pending_tickers
        self._event_hooked = True

# unsubscribe():
if self._event_hooked and not self._subscriptions:
    try:
        self._ib.pendingTickersEvent -= self._on_pending_tickers
    except ValueError:
        logger.warning("pendingTickersEvent handler was not registered")
    self._event_hooked = False
```

---

### [x] M4. `_PRIORITY` completeness not asserted
**Files:** `src/analysis/unusual_detector.py:107` · `src/analysis/smart_money.py:68`

`smart_money.py` asserts `_CONFIDENCE_WEIGHTS` covers every `SmartMoneyReason` (lines 78–80)
but neither module asserts `_PRIORITY` does. Adding a new reason without updating `_PRIORITY`
makes `next(r for r in _PRIORITY if r in reasons)` raise `StopIteration` at runtime
(unusual_detector.py:206, smart_money.py:249).

**Fix:** add after each `_PRIORITY` definition:
```python
assert set(_PRIORITY) == set(UnusualReason), (
    "_PRIORITY must contain an entry for every UnusualReason"
)
# and in smart_money.py:
assert set(_PRIORITY) == set(SmartMoneyReason), (
    "_PRIORITY must contain an entry for every SmartMoneyReason"
)
```

---

### [x] M5. `setup_callbacks` state param typed as `object`
**File:** `src/dashboard/callbacks.py:213`

```python
def setup_callbacks(app: Dash, state: object) -> None:
```

Violates "type hints everywhere". **Fix:** type as `SharedState`. If there's a circular
import, use:
```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.dashboard.shared_state import SharedState
def setup_callbacks(app: Dash, state: SharedState) -> None: ...
```

---

### [x] M6. Silent tick drops on full queue — no observability
**File:** `src/data/tick_stream.py:378–384`

On `asyncio.QueueFull` the tick is dropped with only a warning log. During bursts this is
invisible data loss.

**Fix:** add `self._dropped_ticks = 0` counter, increment in the except branch, expose via
a read-only property (and optionally include in periodic stats logging from the
orchestration loop). Keep `put_nowait` — the handler is synchronous (cannot await), this is
by design (see MEMORY: pendingTickersEvent handler is sync).

---

### [x] M7. Dead shadowed import in earnings parser
**File:** `src/utils/earnings.py:180–181`

```python
from datetime import date as _date   # never used; module-level `date` already imported
```

**Fix:** delete the line. (The `import pandas as pd` on the next line IS used by
`pd.Timestamp(val).date()` — keep it, or hoist next to the other local pandas import.)

---

## LOW

### [x] L1. `src/data/historical.py` is a 0-byte stub
Either implement (CLAUDE.md describes it: historical bars via `reqHistoricalData`, must use
`limiter.acquire("historical")`) or delete the file and remove from CLAUDE.md's structure
diagram. If C1 lands first, implementing it later gets the limiter for free.

### [x] L2. `get_sync_engine` missing from `src/storage/__init__.py` `__all__`
It's a public API used by dashboard callbacks. Add to the export list.

### [x] L3. `premium_str` could be inlined — `src/alerts/rules.py:232` (cosmetic)

### [x] L4. Layout docstring missing `pipeline-status` — `src/dashboard/layouts.py:24–26`
The "IDs defined:" list omits the `pipeline-status` span added at lines 64–67.

### [x] L5. Comment the chain_fetcher sleeps — `src/data/chain_fetcher.py:442–473`
After C1 they will look like redundant rate limiting. Add:
`# settlement delay so IBKR market data populates — NOT rate limiting (RateLimiter handles pacing)`

### [x] L6. RateLimiter logging/encapsulation nits — `src/connection/rate_limiter.py:165–169, 183`
Debug log reads `self._bucket.available` *after* consume (property re-refills → misleading
value); `stats()` reaches into `_bucket._capacity`. Fix: capture available before consume or
drop the log; add a public `capacity` property.

---

## TESTS

### [x] T1 (Medium). Hardcoded 2026 dates — will rot
**Files:** `tests/test_unusual_detector.py:143, 170` · `tests/test_storage.py:136–149`

Violates the project's own convention (MEMORY.md: "Test helpers MUST use
`datetime.now(timezone.utc)` as default timestamp — hardcoded dates break purge_stale tests
as days pass"). Replace `datetime(2026, 3, ...)` with `datetime.now(timezone.utc)` or
relative offsets (`now - timedelta(hours=2)`). Note: `datetime(2020, 1, 1)` in
test_unusual_detector.py:454 is **intentional** (stale-purge test needs an old date) — add a
comment there rather than changing it.

### [x] T2 (Medium). Duplicate `make_tick` / `make_trade` factory helpers
Defined independently in test_flow_classifier.py, test_alerts.py, test_unusual_detector.py,
test_tick_stream.py, test_greeks_engine.py, test_smart_money.py. Move canonical versions to
`tests/conftest.py` (plain functions, not fixtures, so defaults can be overridden per call)
and import everywhere. Watch for small signature drift between copies — reconcile to a
superset with keyword defaults.

### [x] T3. Coverage gaps (add tests for)
- [x] Scanner failure path: `scanner.py:147–149` — mock `reqScannerSubscriptionAsync` raising; assert `RuntimeError` with message.
- [x] Dashboard callback exception branches (all 4 try/excepts in `callbacks.py:227–280`): monkeypatch state/query fns to raise; assert each callback returns its safe default.
- [x] `get_session()` rollback: insert valid row + raise in same session; assert row absent after rollback (`db.py:178–183`).
- [x] Notifier timeout: `requests.Timeout` side effect on `requests.post`; assert no raise (locked in).
- [x] Concurrent `RateLimiter.acquire()`: 10 parallel `asyncio.create_task` acquires; assert no token leakage.
- [x] Script entry points: KeyboardInterrupt handling in `run_scanner.py:216`; `init_db` failure in `run_dashboard.py:280`.
- [x] Pipeline resilience: one symbol's `fetch_chain` raising must not kill the loop (`run_scanner.py:141–142`).

---

## Verified FALSE ALARMS (do not "fix")

| Claim | Why it's wrong |
|---|---|
| "`.where()` applied after `.limit()` in dashboard queries" | Order is correct — `callbacks.py:185–186, 203–204` |
| "Unused `init_db` import in run_dashboard `__main__`" | Used at line 280: `asyncio.run(init_db())` |
| "Missing `smart_money.purge_stale()` / `greeks.purge_stale()` in pipelines" | Both are documented stateless no-ops (return 0) |
| "`remaining, remaining` duplicate log arg in scripts" | Template has 3 placeholders after symbol — intentional (`truncating X to Y (cap remaining=Y)`) |
| "FlowClassifier window pruning off-by-one / spread div-by-zero / BS T=0 crash" | All correctly guarded — verified |
| "Empty exposure sums return 0.0 instead of None" | Intentional and documented |

---

## Post-fix checklist
- [x] `python -m pytest -q -m "not integration"` — all green (627 tests, 2026-06-13)
- [ ] Update `MEMORY.md`: correct the queries.py stripping claim (H2); note RateLimiter wiring (C1); note IV-skew decision (H4)
- [ ] If H2 done: delete/migrate dev SQLite DB so stored datetime formats are uniform
