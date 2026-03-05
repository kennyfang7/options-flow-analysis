# Storage Layer Design
**Date:** 2026-03-06
**Module:** `src/storage/` (models.py, db.py, queries.py)
**Build Order Step:** 4 of 14 (CLAUDE.md) / Step 5 of 15 (PROJECT_BLUEPRINT.md)

---

## Overview

Persistent storage for raw options data: chain snapshots (from ChainFetcher) and live ticks
(from TickStream). Uses SQLAlchemy 2.0 async ORM with SQLite for development and PostgreSQL
for production. Analysis output tables (classifications, anomalies, sentiment) are added later
by their respective modules.

---

## Approach

Single async engine + `async_sessionmaker` (the "one shared tap" pattern). Engine reads
`DATABASE_URL` from `config/settings.py` (already defined). Session factory is created once
at startup and passed to any module that needs DB access. All writes use
`async with session_factory() as session` for explicit lifecycle management.

---

## Schema: 3 Tables

### `chain_snapshots`
One row per `ChainFetcher.fetch_chain()` call.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| underlying | VARCHAR | e.g. "SPY" |
| underlying_price | FLOAT | Price at snapshot time |
| captured_at | DATETIME | UTC |

Index: `(underlying, captured_at)`

---

### `option_contracts`
One row per `OptionContract` within a snapshot. Linked to `chain_snapshots`.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| snapshot_id | INTEGER FK → chain_snapshots.id | |
| symbol | VARCHAR | |
| expiry | VARCHAR | YYYYMMDD |
| strike | FLOAT | |
| right | VARCHAR(1) | "C" or "P" |
| con_id | INTEGER NULLABLE | IBKR contract ID |
| bid | FLOAT NULLABLE | |
| ask | FLOAT NULLABLE | |
| last | FLOAT NULLABLE | |
| volume | INTEGER NULLABLE | |
| open_interest | INTEGER NULLABLE | |
| implied_vol | FLOAT NULLABLE | |
| delta | FLOAT NULLABLE | |
| gamma | FLOAT NULLABLE | |
| theta | FLOAT NULLABLE | |
| vega | FLOAT NULLABLE | |

Index: `(snapshot_id)` (FK implied)
Unique constraint: `(snapshot_id, con_id)` — no duplicate contracts per snapshot
Note: `mid` is NOT stored — recompute as `(bid + ask) / 2` on reads.

---

### `option_ticks`
One row per `TickUpdate` from `TickStream.queue`.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| symbol | VARCHAR | |
| con_id | INTEGER | |
| expiry | VARCHAR | YYYYMMDD |
| strike | FLOAT | |
| right | VARCHAR(1) | "C" or "P" |
| received_at | DATETIME | UTC, microsecond precision |
| bid | FLOAT NULLABLE | |
| ask | FLOAT NULLABLE | |
| last | FLOAT NULLABLE | |
| volume | INTEGER NULLABLE | Session cumulative |
| open_interest | INTEGER NULLABLE | |
| last_size | INTEGER NULLABLE | Size of most recent trade |
| bid_size | INTEGER NULLABLE | |
| ask_size | INTEGER NULLABLE | |
| underlying_price | FLOAT NULLABLE | |
| implied_vol | FLOAT NULLABLE | |
| delta | FLOAT NULLABLE | |
| gamma | FLOAT NULLABLE | |
| theta | FLOAT NULLABLE | |
| vega | FLOAT NULLABLE | |

Indexes:
- `(con_id, received_at)` — primary query pattern for flow_classifier
- `(symbol, received_at)` — aggregate analysis by underlying

Note: `mid` is NOT stored — recompute on reads.

---

## Files

### `src/storage/models.py`
SQLAlchemy 2.0 ORM models using `DeclarativeBase`. Defines `ChainSnapshot`,
`OptionContractRecord`, `OptionTick` mapped classes with all columns, constraints, and indexes.

### `src/storage/db.py`
- `create_async_engine()` — reads `DATABASE_URL` from settings
- `async_sessionmaker` — session factory instance
- `init_db()` — `CREATE TABLE IF NOT EXISTS` (runs on startup)
- `get_session()` — async context manager returning `AsyncSession`

### `src/storage/queries.py`
- `insert_chain_snapshot(session, snapshot: OptionChainSnapshot) -> int` — saves snapshot + all contracts, returns snapshot_id
- `insert_tick(session, tick: TickUpdate) -> int` — saves one tick, returns tick_id
- `get_latest_snapshot(session, underlying: str) -> ChainSnapshot | None`
- `get_recent_ticks(session, con_id: int, minutes: int = 1) -> list[OptionTick]`

---

## Data Flow

```
ChainFetcher.fetch_chain() → OptionChainSnapshot
    → insert_chain_snapshot()
        → INSERT chain_snapshots (1 row)
        → INSERT option_contracts (N rows, bulk)

TickStream.queue → TickUpdate
    → insert_tick()
        → INSERT option_ticks (1 row per tick)
```

---

## Key Constraints

| Constraint | Detail |
|---|---|
| No stored `mid` | Computed field — recompute on reads as (bid+ask)/2 |
| Unique contracts per snapshot | UniqueConstraint(snapshot_id, con_id) |
| Async sessions only | Never use sync session in async context |
| Migration-safe | Use `CREATE TABLE IF NOT EXISTS` via SQLAlchemy metadata |
| DATABASE_URL | Already in settings.py — SQLite dev, PostgreSQL prod |

---

## Testing Strategy

- In-memory SQLite engine for all unit tests (`:memory:`)
- Pytest fixture in `conftest.py`: `async_db_session` — creates tables, yields session, drops after
- Test `insert_chain_snapshot` with a real `OptionChainSnapshot` pydantic object
- Test `insert_tick` with a real `TickUpdate` pydantic object
- Assert reads return correct data
- Assert unique constraint blocks duplicate contracts
- No live TWS required — purely DB layer tests
