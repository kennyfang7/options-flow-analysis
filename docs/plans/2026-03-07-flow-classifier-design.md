# Flow Classifier — Design Document

**Date:** 2026-03-07
**Module:** `src/analysis/flow_classifier.py`
**Step:** 6 of 14 in the module build order

---

## Overview

The flow classifier consumes `TickUpdate` objects from `TickStream.queue` and labels each
qualifying trade as one of: `SWEEP`, `SPLIT`, `BLOCK`, `MULTI_LEG` (future), or `UNKNOWN`.
It emits `ClassifiedTrade` objects to the caller — it does not persist to the database.
Persistence is the orchestration layer's responsibility.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Window storage | In-memory deque per con_id | No DB round-trip on hot path; state loss on restart is acceptable |
| Output | Emit `ClassifiedTrade` only | Single responsibility; caller decides what to do with the result |
| Thresholds | Configurable via `config/settings.py` | Tunable without code changes |
| Multi-leg | Enum placeholder + comment hook only | Cross-contract correlation deferred; foundation in place |
| Classification priority | Sweep → Split → Block → Unknown | Most specific label wins |

---

## Data Models

```python
class TradeType(str, Enum):
    SWEEP = "sweep"
    SPLIT = "split"
    BLOCK = "block"
    MULTI_LEG = "multi_leg"   # placeholder — detection not implemented
    UNKNOWN = "unknown"


class Aggressor(str, Enum):
    BUY = "buy"       # spread_position >= aggressor_buy_threshold (default 0.70)
    SELL = "sell"     # spread_position <= aggressor_sell_threshold (default 0.30)
    NEUTRAL = "neutral"


class ClassifiedTrade(BaseModel):
    # Identity (flattened for ergonomic downstream access)
    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    underlying_price: float | None

    # Greeks from triggering tick
    implied_vol: float | None
    delta: float | None

    # Classification
    trade_type: TradeType
    aggressor: Aggressor
    spread_position: float | None
    # Unclamped (last - bid) / (ask - bid).
    # >1.0 means paid above ask (extreme buy urgency).
    # <0.0 means hit below bid (extreme sell urgency).
    # None when bid/ask/last unavailable or ask == bid (locked market).
    # Treat as probabilistic, not deterministic — stale quotes can produce
    # out-of-range values that do not reflect genuine urgency.

    effective_price: float | None
    # Trade price used for premium computation.
    # = tick.last if bid <= last <= ask, else tick.mid as fallback.
    # None if neither is available (tick is skipped in that case).

    last_size: int | None       # size of the triggering print
    premium: float | None       # last_size × effective_price × 100
    signal_strength: float | None
    # log1p(premium / min_premium) × min(volume_delta / max(open_interest, 1), 10.0)
    # None when open_interest is unavailable.
    # Capped at OI multiplier of 10.0 to prevent low-OI contracts from dominating.
    # Use log1p to avoid signal_strength = 0 at the minimum premium threshold.

    volume_delta: int
    # Increase in cumulative session volume since last tick for this con_id.
    # On first sight of a con_id: approximated as tick.last_size (known limitation).
    # On session reset (volume decrease detected): re-seeded, approximated as tick.last_size.

    window_ticks: int
    # Number of ticks in the detection window used for classification:
    #   SWEEP  → len(recent_sweep_window)
    #   SPLIT  → len(recent_split_window)
    #   BLOCK  → 1
    #   UNKNOWN → 1

    timestamp: datetime         # = tick.timestamp (when trade occurred, not when classified)

    tick: TickUpdate = Field(exclude=True)
    # Full raw tick stored in-memory for downstream access.
    # Excluded from serialization — not written to DB.
```

---

## FlowClassifier Class

```python
class FlowClassifier:
    """Stateful real-time trade classifier.

    Maintains a per-contract in-memory window of recent (tick, aggressor) tuples.
    classify() is synchronous and performs no IO — safe to call on the hot path.

    State is lost on process restart. This is acceptable: the classifier is designed
    for real-time use, and the restart blind spot (a few seconds) is negligible.

    The orchestration layer MUST call purge_stale() periodically (e.g. hourly) to
    evict state for contracts no longer being tracked.
    """

    _windows: dict[int, deque[tuple[TickUpdate, Aggressor]]]  # maxlen=500 per con_id
    _last_volume: dict[int, int]   # con_id → last seen cumulative session volume

    def __init__(self, settings: Settings) -> None: ...

    def classify(self, tick: TickUpdate) -> ClassifiedTrade | None:
        """Classify a single TickUpdate as a trade event.

        Returns None (tick is silently skipped) if:
        - tick.last_size is None (quote update, not a trade print)
        - tick.last is None (cannot compute premium)
        - tick.volume is None (cannot deduplicate against previous snapshot)
        - volume_delta == 0 (duplicate snapshot, no new trades)
        - effective_price cannot be determined (no last within spread, no mid)
        - premium < settings.min_premium (trade too small to be meaningful)
        """

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Evict state for contracts not seen in max_age_seconds.

        Must be called by the orchestration layer (e.g. once per hour).
        Without periodic purging, _windows and _last_volume accumulate entries
        for expired or unsubscribed contracts indefinitely.

        Returns:
            Number of con_ids purged.
        """

    # ---------------------------------------------------------------------------
    # Multi-leg hook (future implementation)
    # ---------------------------------------------------------------------------
    # def _check_cross_contract(self, tick: TickUpdate) -> bool:
    #     """Detect multi-leg trades by correlating prints across contracts.
    #
    #     Implementation requires a cross-contract window keyed by
    #     (symbol, timestamp_bucket) to match related prints (e.g. call + put)
    #     arriving within a short time window on the same underlying.
    #
    #     Not implemented — deferred to a future iteration.
    #     Placeholder TradeType.MULTI_LEG exists in the enum.
    #     """
```

---

## Classification Logic (Hot Path)

### 1. Early Exits

```python
if tick.last_size is None or tick.last is None or tick.volume is None:
    return None
```

### 2. Volume Deduplication + Session Reset Detection

```python
if con_id not in _last_volume or tick.volume < _last_volume[con_id]:
    # First sight of con_id, or IBKR session volume reset (new trading day).
    # Use last_size as best approximation of volume_delta.
    # Known limitation: on first sight, cumulative session volume may already
    # be large; last_size reflects only the most recent print, not the total.
    logger.warning("volume reset detected for con_id={} ({}→{})",
                   con_id, _last_volume.get(con_id, "unseen"), tick.volume)
    _last_volume[con_id] = tick.volume
    volume_delta = tick.last_size
else:
    volume_delta = tick.volume - _last_volume[con_id]
    _last_volume[con_id] = tick.volume

if volume_delta == 0:
    return None
```

### 3. Aggressor + Spread Position

```python
bid, ask, last = tick.bid, tick.ask, tick.last
if bid is not None and ask is not None and last is not None:
    if ask == bid:
        # Locked market — cannot determine direction from price alone.
        spread_position, aggressor = None, Aggressor.NEUTRAL
    else:
        spread_position = (last - bid) / (ask - bid)  # intentionally unclamped
        if spread_position >= settings.aggressor_buy_threshold:
            aggressor = Aggressor.BUY
        elif spread_position <= settings.aggressor_sell_threshold:
            aggressor = Aggressor.SELL
        else:
            aggressor = Aggressor.NEUTRAL
else:
    spread_position, aggressor = None, Aggressor.NEUTRAL
```

### 4. Window Update

```python
# Add current tick + computed aggressor to window. Prune stale entries.
# Aggressor is computed once here — not recomputed during sweep/split checks.
_windows[con_id].append((tick, aggressor))
cutoff = tick.timestamp - timedelta(seconds=settings.classifier_window_seconds)
while _windows[con_id] and _windows[con_id][0][0].timestamp < cutoff:
    _windows[con_id].popleft()
```

### 5. Effective Price + Premium Gate

```python
if bid is not None and ask is not None and last is not None and bid <= last <= ask:
    effective_price = last
elif tick.mid is not None:
    effective_price = tick.mid
else:
    return None

premium = tick.last_size * effective_price * 100
if premium < settings.min_premium:
    return None
```

### 6. Classification (Sweep → Split → Block → Unknown)

```python
now = tick.timestamp
window = _windows[con_id]

# SWEEP: >= sweep_min_legs prints within sweep_window_seconds, all same aggressor.
# Approximation: true sweeps hit multiple exchanges simultaneously; with reqMktData
# we cannot observe exchange routing, so we use rapid same-direction prints as a proxy.
recent_sweep = [(t, agg) for t, agg in window
                if (now - t.timestamp).total_seconds() <= settings.sweep_window_seconds]
if len(recent_sweep) >= settings.sweep_min_legs and _all_same_aggressor(recent_sweep):
    trade_type, window_ticks = TradeType.SWEEP, len(recent_sweep)

# SPLIT: >= split_min_legs prints within split_window_seconds, sizes within
# ±split_size_tolerance of the median size. Detects large orders broken into
# equal-sized pieces to disguise true size.
else:
    recent_split = [(t, agg) for t, agg in window
                    if (now - t.timestamp).total_seconds() <= settings.split_window_seconds]
    if (len(recent_split) >= settings.split_min_legs
            and _sizes_within_tolerance(recent_split, settings.split_size_tolerance)):
        trade_type, window_ticks = TradeType.SPLIT, len(recent_split)

    # BLOCK: single print large enough. Uses last_size (the individual print),
    # not volume_delta (which may aggregate multiple small trades).
    elif tick.last_size >= settings.min_block_size:
        trade_type, window_ticks = TradeType.BLOCK, 1

    else:
        trade_type, window_ticks = TradeType.UNKNOWN, 1
```

### 7. Signal Strength

```python
if tick.open_interest is None:
    signal_strength = None
else:
    oi_ratio = min(volume_delta / max(tick.open_interest, 1), 10.0)
    signal_strength = log1p(premium / settings.min_premium) * oi_ratio
```

---

## Helper Functions

### `_all_same_aggressor`

```python
def _all_same_aggressor(entries: list[tuple[TickUpdate, Aggressor]]) -> bool:
    aggressors = {agg for _, agg in entries if agg != Aggressor.NEUTRAL}
    return len(aggressors) == 1
```

### `_sizes_within_tolerance`

```python
def _sizes_within_tolerance(
    entries: list[tuple[TickUpdate, Aggressor]], tol: float
) -> bool:
    """All last_size values within ±tol of the median size.

    Uses median (not mean) for robustness against outliers.
    Returns False if no sizes are available or median is zero.
    """
    sizes = [t.last_size for t, _ in entries if t.last_size is not None]
    if not sizes:
        return False
    median = sorted(sizes)[len(sizes) // 2]
    if median == 0:
        return False
    return all(abs(s - median) / median <= tol for s in sizes)
```

---

## Settings Additions (`config/settings.py`)

```python
# Flow Classifier
sweep_window_seconds: float = 2.0
sweep_min_legs: int = 3
split_window_seconds: float = 5.0
split_min_legs: int = 3
split_size_tolerance: float = 0.20       # ±20% of median last_size
classifier_window_seconds: float = 30.0
aggressor_buy_threshold: float = 0.70
aggressor_sell_threshold: float = 0.30

# Existing fields (no duplication):
#   min_block_size: int = 500
#   min_premium: float  ← add validator:

@field_validator("min_premium")
@classmethod
def min_premium_must_be_positive(cls, v: float) -> float:
    if v <= 0:
        raise ValueError("min_premium must be greater than 0")
    return v
```

---

## Database Schema — `classified_trades` (design now, build later)

> **Note:** The classifier itself does not persist — it emits `ClassifiedTrade` objects.
> The orchestration layer persists via `insert_classified_trade()` in `storage/queries.py`.
> This table is designed now so the persistence layer can be built without revisiting this design.

```
classified_trades
├── id                INTEGER     PK, autoincrement
├── con_id            INTEGER     NOT NULL
├── symbol            TEXT        NOT NULL
├── expiry            TEXT        NOT NULL
├── strike            REAL        NOT NULL
├── right             TEXT(1)     NOT NULL
├── underlying_price  REAL        NULLABLE
├── implied_vol       REAL        NULLABLE
├── delta             REAL        NULLABLE
├── trade_type        TEXT        NOT NULL  -- Enum(TradeType), CHECK constraint
├── aggressor         TEXT        NOT NULL  -- Enum(Aggressor), CHECK constraint
├── spread_position   REAL        NULLABLE
├── effective_price   REAL        NULLABLE
├── last_size         INTEGER     NULLABLE
├── premium           REAL        NULLABLE
├── signal_strength   REAL        NULLABLE
├── volume_delta      INTEGER     NOT NULL
├── window_ticks      INTEGER     NOT NULL
├── classified_at     DATETIME    NOT NULL  -- = tick.timestamp (event time, not classification time)

Indexes:
  ix_classified_trades_symbol_classified_at         (symbol, classified_at)
  ix_classified_trades_con_id_classified_at         (con_id, classified_at)
  ix_classified_trades_symbol_aggressor_classified_at (symbol, aggressor, classified_at)
```

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| reqMktData gives snapshots, not trade prints | Multiple trades can arrive in one volume_delta; individual prints may be missed | Deduplicate via volume delta; document as best-effort |
| Sweep detection cannot observe exchange routing | Sweep label is a heuristic (rapid same-direction prints), not a verified exchange sweep | Lower confidence on sweep signals; document in classifier |
| State lost on restart | 1–2 second blind spot after restart | Acceptable for real-time use; optional DB warm-up deferred |
| First tick per con_id uses last_size as volume_delta approximation | volume_delta may undercount if session was already in progress | Logged, documented, acceptable |
| spread_position outside [0, 1] may be stale quotes or genuine urgency | Cannot distinguish without trade-level data | Treat as probabilistic; downstream modules should not hard-threshold at 1.0 |

---

## Integration Points

| Upstream | `TickStream.queue` → `TickUpdate` |
|---|---|
| **Downstream consumers** | `unusual_detector` (premium, volume_delta, signal_strength), `greeks_engine` (implied_vol, delta via tick), `sentiment` (aggressor, right, premium), `smart_money` (trade_type, signal_strength, delta), `alerts` (all fields) |
| **Persistence** | Caller invokes `insert_classified_trade(session, trade)` — not the classifier |
| **Orchestration** | Caller must invoke `purge_stale()` periodically (e.g. hourly) |
