# Unusual Activity Detector — Design Document

**Date:** 2026-03-08
**Module:** `src/analysis/unusual_detector.py`
**Step:** 7 of 14 in the module build order

---

## Overview

The unusual detector consumes `ClassifiedTrade` objects from `FlowClassifier.classify()` and
decides whether each trade is noteworthy enough to surface downstream. It answers the question:
"given that this is a labeled trade (sweep, block, etc.), is it *unusually* large or significant?"

It emits `UnusualSignal` objects to the caller — it does not persist. Persistence is the
orchestration layer's responsibility.

**Phase 1 scope:** Four threshold-based conditions. Statistical anomaly detection against
per-contract historical baselines is deferred to a future iteration and will require async
DB lookups at that point — the interface is designed to accommodate this.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| State | OI cache only (`dict[int, int]`) | OI arrives infrequently from IBKR; cache preserves last-known value across ticks |
| `detect()` signature | `async` | Future DB-backed baselines will need IO; making it async now avoids a breaking change |
| Purge API | `purge_stale(max_age_seconds)` matching FlowClassifier | Consistent orchestration layer call pattern across all analysis modules |
| Output | `UnusualSignal` wrapping `ClassifiedTrade` | Adds reason list + metadata; `trade` field excluded from serialization (same pattern as `ClassifiedTrade.tick`) |
| OTM detection | Delta-based (`\|delta\| <= threshold`) | Accounts for time-to-expiry and IV; more precise than % distance from strike |
| FK to classified_trades | No | Consistent with existing pattern (OptionTick has no FK to option_contracts); avoids persistence ordering constraint |
| `reasons` storage | JSON text array | SQLite has no native array type; JSON is forward-compatible with PostgreSQL operators |
| Field duplication | Accepted | `UnusualSignal` duplicates 8 fields from `ClassifiedTrade`; extract a `ContractIdentity` mixin only if a third model needs the same fields |

---

## Data Models

```python
class UnusualReason(str, Enum):
    PREMIUM_SIZE    = "premium_size"
    # trade.premium >= unusual_premium_threshold (default $250k)
    # Catches: absolute dollar size indicating institutional capital

    OI_RATIO        = "oi_ratio"
    # volume_delta / open_interest >= unusual_oi_ratio_threshold (default 0.50)
    # Catches: one print consuming ≥50% of all existing open positions

    SIGNAL_STRENGTH = "signal_strength"
    # trade.signal_strength >= unusual_signal_threshold (default 5.0)
    # Catches: trades that score high on combined premium + OI-relative volume

    OTM_PREMIUM     = "otm_premium"
    # |delta| <= otm_delta_threshold (default 0.30) AND premium >= otm_premium_threshold (default $100k)
    # Catches: expensive bets on far OTM contracts — the classic "smart money" tell


class UnusualSignal(BaseModel):
    # Identity (flattened for serialization — same pattern as ClassifiedTrade)
    symbol: str
    con_id: int
    expiry: str
    right: str
    strike: float
    trade_type: TradeType
    aggressor: Aggressor
    premium: float | None
    volume_delta: int
    signal_strength: float | None
    delta: float | None
    underlying_price: float | None
    implied_vol: float | None
    effective_price: float | None

    # Detection result
    reasons: list[UnusualReason]    # all conditions that fired (≥1 guaranteed)
    top_reason: UnusualReason       # highest-priority fired reason
    # Priority: PREMIUM_SIZE > OI_RATIO > SIGNAL_STRENGTH > OTM_PREMIUM
    # PREMIUM_SIZE ranks first: it is always available and represents absolute capital commitment.
    # OI_RATIO second: high signal but depends on OI cache being populated.

    flagged_at: datetime            # when detect() was called (not trade.timestamp)
    # Distinguished from trade.timestamp to preserve semantic clarity.
    # In Phase 1 these are nearly identical; in future async implementations
    # there may be measurable latency between tick receipt and signal emission.

    # Full trade available in-memory, excluded from serialization
    trade: ClassifiedTrade = Field(exclude=True)
```

---

## UnusualDetector Class

```python
class UnusualDetector:
    """Stateless threshold-based filter for unusual options activity.

    Maintains a lightweight OI cache (`_oi_cache`) to persist the last-known
    open_interest per contract across ticks. IBKR sends OI infrequently as a
    separate tick type; without caching, the OI_RATIO check would be silently
    skipped on most ticks.

    detect() is async to accommodate future DB-backed statistical baselines.
    The current implementation performs no IO — it is safe to await on the hot path.

    The orchestration layer MUST call purge_stale() periodically (e.g. hourly)
    to evict state for contracts no longer being tracked.

    Note: OI cache can be seeded at startup from the most recent ChainSnapshot
    via get_latest_snapshot() in queries.py. This is the orchestration layer's
    responsibility, not this module's.
    """

    _oi_cache: dict[int, int]   # con_id → last known open_interest

    def __init__(self, settings: Settings) -> None: ...

    async def detect(self, trade: ClassifiedTrade) -> UnusualSignal | None:
        """Evaluate a ClassifiedTrade against unusual activity thresholds.

        Updates the OI cache from trade.tick.open_interest if available.
        Evaluates four independent conditions. Returns an UnusualSignal if
        any condition fires, otherwise None.

        Returns None without evaluation if trade.trade_type is MULTI_LEG
        (detection deferred — multi-leg premium and delta have different semantics).

        Args:
            trade: ClassifiedTrade from FlowClassifier.classify().

        Returns:
            UnusualSignal if one or more conditions fired, else None.
        """

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """Evict OI cache entries for contracts not seen in max_age_seconds.

        Matches FlowClassifier.purge_stale() signature for consistent
        orchestration layer call pattern.

        Returns:
            Number of con_ids evicted.
        """
```

---

## Detection Logic (Hot Path)

```python
async def detect(self, trade: ClassifiedTrade) -> UnusualSignal | None:
    s = self._settings

    # MULTI_LEG trades are not evaluated — detection not yet implemented
    if trade.trade_type == TradeType.MULTI_LEG:
        return None

    # Update OI cache from tick if available
    if trade.tick.open_interest is not None:
        if trade.con_id not in self._oi_cache:
            logger.debug("unusual_detector: OI cache populated for con_id={}", trade.con_id)
        self._oi_cache[trade.con_id] = trade.tick.open_interest

    oi = self._oi_cache.get(trade.con_id)
    reasons: list[UnusualReason] = []

    # 1. PREMIUM_SIZE — absolute dollar commitment
    if trade.premium is not None and trade.premium >= s.unusual_premium_threshold:
        reasons.append(UnusualReason.PREMIUM_SIZE)

    # 2. OI_RATIO — fraction of all open positions consumed in one print
    if oi is not None and oi > 0 and trade.volume_delta > 0:
        if trade.volume_delta / oi >= s.unusual_oi_ratio_threshold:
            reasons.append(UnusualReason.OI_RATIO)

    # 3. SIGNAL_STRENGTH — composite score from flow classifier
    if trade.signal_strength is not None and trade.signal_strength >= s.unusual_signal_threshold:
        reasons.append(UnusualReason.SIGNAL_STRENGTH)

    # 4. OTM_PREMIUM — expensive bet on a far OTM contract
    # delta is None when IBKR hasn't populated Greeks yet — skip silently
    if (trade.delta is not None
            and abs(trade.delta) <= s.otm_delta_threshold
            and trade.premium is not None
            and trade.premium >= s.otm_premium_threshold):
        reasons.append(UnusualReason.OTM_PREMIUM)

    if not reasons:
        return None

    priority = [
        UnusualReason.PREMIUM_SIZE,
        UnusualReason.OI_RATIO,
        UnusualReason.SIGNAL_STRENGTH,
        UnusualReason.OTM_PREMIUM,
    ]
    top_reason = next(r for r in priority if r in reasons)

    logger.info(
        "unusual_detector: signal {} | top={} reasons={} premium=${:.0f}",
        trade.symbol, top_reason.value,
        [r.value for r in reasons],
        trade.premium or 0,
    )

    return UnusualSignal(
        symbol=trade.symbol,
        con_id=trade.con_id,
        expiry=trade.expiry,
        right=trade.right,
        strike=trade.strike,
        trade_type=trade.trade_type,
        aggressor=trade.aggressor,
        premium=trade.premium,
        volume_delta=trade.volume_delta,
        signal_strength=trade.signal_strength,
        delta=trade.delta,
        underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol,
        effective_price=trade.effective_price,
        reasons=reasons,
        top_reason=top_reason,
        flagged_at=datetime.now(timezone.utc),
        trade=trade,
    )
```

---

## Settings Additions (`config/settings.py`)

```python
# Unusual Activity Detector
unusual_premium_threshold: float = Field(
    default=250_000.0, description="Minimum single-trade premium ($) to flag as PREMIUM_SIZE"
)
unusual_oi_ratio_threshold: float = Field(
    default=0.50, description="Minimum volume_delta/open_interest ratio to flag as OI_RATIO"
)
unusual_signal_threshold: float = Field(
    default=5.0, description="Minimum signal_strength score to flag as SIGNAL_STRENGTH"
)
otm_delta_threshold: float = Field(
    default=0.30, description="Maximum |delta| to consider a contract OTM for OTM_PREMIUM check"
)
otm_premium_threshold: float = Field(
    default=100_000.0, description="Minimum premium ($) for an OTM contract to flag as OTM_PREMIUM"
)

@model_validator(mode="after")
def unusual_premium_above_min_premium(self) -> Settings:
    if self.unusual_premium_threshold <= self.min_premium:
        raise ValueError(
            f"unusual_premium_threshold ({self.unusual_premium_threshold}) "
            f"must exceed min_premium ({self.min_premium})"
        )
    return self

# Validators for range correctness
@field_validator("unusual_oi_ratio_threshold")
@classmethod
def oi_ratio_must_be_positive(cls, v: float) -> float:
    if v <= 0:
        raise ValueError("unusual_oi_ratio_threshold must be greater than 0")
    return v

@field_validator("otm_delta_threshold")
@classmethod
def otm_delta_must_be_in_range(cls, v: float) -> float:
    if not (0 < v < 1):
        raise ValueError("otm_delta_threshold must be between 0 and 1 (exclusive)")
    return v

@field_validator("unusual_signal_threshold")
@classmethod
def signal_threshold_must_be_positive(cls, v: float) -> float:
    if v <= 0:
        raise ValueError("unusual_signal_threshold must be greater than 0")
    return v
```

> **Note:** `unusual_volume_multiplier` (already in settings) is intentionally unused by this
> module. Volume vs. ADV comparison requires session accumulation and is deferred to
> `smart_money.py` (step 10). The setting is retained to avoid a breaking config change.

---

## Database Schema — `unusual_signals`

> The detector itself does not persist. The orchestration layer persists via
> `insert_unusual_signal()` in `storage/queries.py`.

```
unusual_signals
├── id               INTEGER     PK, autoincrement
├── con_id           INTEGER     NOT NULL
├── symbol           TEXT        NOT NULL
├── expiry           TEXT        NOT NULL
├── strike           REAL        NOT NULL
├── right            TEXT(1)     NOT NULL
├── underlying_price REAL        NULLABLE
├── implied_vol      REAL        NULLABLE
├── delta            REAL        NULLABLE
├── effective_price  REAL        NULLABLE
├── trade_type       TEXT        NOT NULL  -- TradeType enum value
├── aggressor        TEXT        NOT NULL  -- Aggressor enum value
├── premium          REAL        NULLABLE
├── volume_delta     INTEGER     NOT NULL
├── signal_strength  REAL        NULLABLE
├── top_reason       TEXT        NOT NULL  -- UnusualReason enum value
├── reasons          TEXT        NOT NULL  -- JSON array, e.g. '["premium_size","oi_ratio"]'
├── classified_at    DATETIME    NOT NULL  -- = trade.timestamp (join key to classified_trades)
├── flagged_at       DATETIME    NOT NULL  -- when detect() emitted this signal

Indexes:
  ix_unusual_signals_symbol_flagged_at     (symbol, flagged_at)
  ix_unusual_signals_con_id_flagged_at     (con_id, flagged_at)
```

> No FK to `classified_trades` — consistent with project pattern. Use `(con_id, classified_at)`
> for time-window joins when correlating signals to their source trades.

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| OI_RATIO check offline until OI tick arrives | First N ticks per contract skip OI check | Cache pre-seeded by orchestration layer from ChainSnapshot at startup |
| No statistical baseline (Phase 1) | Cannot detect "unusual for this specific contract" — only "unusual in absolute terms" | Threshold tuning; deferred to future iteration with DB lookups |
| `unusual_volume_multiplier` setting unused | Setting exists with no consumer | Documented; consumed by `smart_money.py` (step 10) |
| MULTI_LEG trades skipped | Premium and delta semantics differ for multi-leg strategies | Guarded early return; revisit when MULTI_LEG detection is implemented |
| DTE not explicitly computed | Near-expiry OTM bets are the strongest signal but DTE requires parsing `expiry` string | Delta-based OTM threshold partially accounts for this (delta shrinks as expiry approaches for OTM contracts) |

---

## Integration Points

| Upstream | `FlowClassifier.classify()` → `ClassifiedTrade` |
|---|---|
| **Downstream consumers** | `alerts/rules.py` (top_reason, reasons, premium, aggressor), `dashboard/` (all fields via DB), `smart_money.py` (signal history, cross-contract correlation) |
| **Persistence** | Caller invokes `insert_unusual_signal(session, signal)` — not the detector |
| **Orchestration** | Caller must invoke `purge_stale()` periodically (e.g. hourly); optionally seed `_oi_cache` at startup from latest ChainSnapshot |
