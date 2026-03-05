# Storage Layer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Implement async SQLAlchemy storage for option chain snapshots and live ticks across three files: `models.py`, `db.py`, `queries.py`.

**Architecture:** Three ORM models map to three tables (`chain_snapshots`, `option_contracts`, `option_ticks`). A single async engine + `async_sessionmaker` is created lazily on first use (not at import time). Query functions accept `AsyncSession` directly so tests can inject an in-memory SQLite session without touching the real engine.

**Tech Stack:** SQLAlchemy 2.0+, aiosqlite (SQLite async driver), asyncpg (PostgreSQL async driver), pytest-asyncio (`asyncio_mode = "auto"` already configured — no `@pytest.mark.asyncio` decorators needed).

**Design doc:** `docs/plans/2026-03-06-storage-design.md`

---

## Context for the Implementer

### Key existing files to understand before starting
- `src/data/chain_fetcher.py` — defines `OptionChainSnapshot` and `OptionContract` pydantic models (the data coming INTO storage)
- `src/data/tick_stream.py` — defines `TickUpdate` pydantic model (the other data coming INTO storage)
- `config/settings.py` — has `settings.database_url` (module-level singleton, defaults to `"sqlite:///options_flow.db"`)
- `tests/conftest.py` — existing test fixtures; you will ADD an `async_db_session` fixture here in Task 3

### Important conventions
- `from __future__ import annotations` at top of every file
- Google-style docstrings on all public functions
- `loguru` for logging (`from loguru import logger`)
- `mid` is a computed field in both `OptionContract` and `TickUpdate` — do NOT store it in the DB, recompute on reads
- `asyncio_mode = "auto"` in pyproject.toml means ALL async test functions run as asyncio tests automatically

### What each file does (plain English)
- **models.py**: Python classes that SQLAlchemy maps to DB tables. Think of each class as a row blueprint.
- **db.py**: Creates the database connection and session factory. `init_db()` creates the tables. `get_session()` gives you a session to work with.
- **queries.py**: The actual save/read operations. Takes a session + a domain object, saves or reads it.

---

## Task 1: Install Async DB Drivers + ORM Models

**Files:**
- Modify: `requirements.txt`
- Create: `src/storage/models.py`
- Create: `tests/test_storage.py`

### Step 1: Add async drivers to requirements.txt

Open `requirements.txt`. Under the `# Storage` section, add two lines:

```
aiosqlite>=0.20.0       # async SQLite driver (dev)
asyncpg>=0.29.0         # async PostgreSQL driver (prod)
```

The section should look like:
```
# Storage
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.0  # PostgreSQL (prod)
aiosqlite>=0.20.0       # async SQLite driver (dev)
asyncpg>=0.29.0         # async PostgreSQL driver (prod)
```

### Step 2: Install the new dependencies

```bash
pip install aiosqlite>=0.20.0 asyncpg>=0.29.0
```

Expected: Both packages install without errors.

### Step 3: Write the failing tests

Create `tests/test_storage.py` with the following content. These tests verify the ORM model structure without touching a real database — they're purely checking that the Python class has the right columns mapped:

```python
from __future__ import annotations


def test_chain_snapshot_tablename():
    from src.storage.models import ChainSnapshot
    assert ChainSnapshot.__tablename__ == "chain_snapshots"


def test_option_contract_record_tablename():
    from src.storage.models import OptionContractRecord
    assert OptionContractRecord.__tablename__ == "option_contracts"


def test_option_tick_tablename():
    from src.storage.models import OptionTick
    assert OptionTick.__tablename__ == "option_ticks"


def test_chain_snapshot_columns():
    from src.storage.models import ChainSnapshot
    cols = {c.name for c in ChainSnapshot.__table__.columns}
    assert cols == {"id", "underlying", "underlying_price", "captured_at"}


def test_option_contract_record_columns():
    from src.storage.models import OptionContractRecord
    cols = {c.name for c in OptionContractRecord.__table__.columns}
    expected = {
        "id", "snapshot_id", "symbol", "expiry", "strike", "right", "con_id",
        "bid", "ask", "last", "volume", "open_interest",
        "implied_vol", "delta", "gamma", "theta", "vega",
    }
    assert cols == expected


def test_option_tick_columns():
    from src.storage.models import OptionTick
    cols = {c.name for c in OptionTick.__table__.columns}
    expected = {
        "id", "symbol", "con_id", "expiry", "strike", "right", "received_at",
        "bid", "ask", "last", "volume", "open_interest",
        "last_size", "bid_size", "ask_size", "underlying_price",
        "implied_vol", "delta", "gamma", "theta", "vega",
    }
    assert cols == expected


def test_option_contract_record_unique_constraint():
    from src.storage.models import OptionContractRecord
    constraint_names = {c.name for c in OptionContractRecord.__table__.constraints}
    assert "uq_snapshot_contract" in constraint_names


def test_option_ticks_has_con_id_index():
    from src.storage.models import OptionTick
    index_names = {i.name for i in OptionTick.__table__.indexes}
    assert "ix_option_ticks_con_id_received_at" in index_names


def test_chain_snapshots_has_underlying_index():
    from src.storage.models import ChainSnapshot
    index_names = {i.name for i in ChainSnapshot.__table__.indexes}
    assert "ix_chain_snapshots_underlying_captured_at" in index_names
```

### Step 4: Run the tests to verify they fail

```bash
pytest tests/test_storage.py -v
```

Expected: All tests FAIL with `ImportError: cannot import name 'ChainSnapshot' from 'src.storage.models'` (the file is empty).

### Step 5: Implement models.py

Replace the contents of `src/storage/models.py` with:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


class ChainSnapshot(Base):
    """One row per ChainFetcher.fetch_chain() call.

    Stores the point-in-time context for a full option chain capture.
    Related OptionContractRecord rows hold the individual contracts.
    """

    __tablename__ = "chain_snapshots"
    __table_args__ = (
        Index("ix_chain_snapshots_underlying_captured_at", "underlying", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String, nullable=False)
    underlying_price: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    contracts: Mapped[list[OptionContractRecord]] = relationship(
        "OptionContractRecord", back_populates="snapshot", cascade="all, delete-orphan"
    )


class OptionContractRecord(Base):
    """One row per OptionContract within a ChainSnapshot.

    All nullable fields mirror the source OptionContract pydantic model.
    Note: 'mid' is intentionally omitted — compute it as (bid + ask) / 2 on reads.
    """

    __tablename__ = "option_contracts"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "con_id", name="uq_snapshot_contract"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chain_snapshots.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
    con_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)

    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)

    snapshot: Mapped[ChainSnapshot] = relationship(
        "ChainSnapshot", back_populates="contracts"
    )


class OptionTick(Base):
    """One row per TickUpdate received from TickStream.queue.

    Stores raw streaming tick data for downstream analysis (flow_classifier,
    unusual_detector, etc.). Note: 'mid' is intentionally omitted.
    """

    __tablename__ = "option_ticks"
    __table_args__ = (
        Index("ix_option_ticks_con_id_received_at", "con_id", "received_at"),
        Index("ix_option_ticks_symbol_received_at", "symbol", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)
```

### Step 6: Run the tests to verify they pass

```bash
pytest tests/test_storage.py -v
```

Expected: All 9 tests PASS.

### Step 7: Commit

```bash
git add requirements.txt src/storage/models.py tests/test_storage.py
git commit -m "feat: add SQLAlchemy ORM models for storage layer"
```

---

## Task 2: DB Engine and Session Management

**Files:**
- Create: `src/storage/db.py`
- Modify: `tests/test_storage.py` (append new tests)

### Step 1: Write the failing tests

Append these tests to `tests/test_storage.py`:

```python
async def test_init_db_creates_tables():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from src.storage.db import init_db

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(engine=engine)

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        tables = {row[0] for row in result}

    await engine.dispose()
    assert "chain_snapshots" in tables
    assert "option_contracts" in tables
    assert "option_ticks" in tables


async def test_get_session_yields_async_session():
    from sqlalchemy.ext.asyncio import (
        AsyncSession, create_async_engine, async_sessionmaker
    )
    from src.storage.db import init_db, get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(engine=engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with get_session(session_factory=factory) as session:
        assert isinstance(session, AsyncSession)

    await engine.dispose()


async def test_get_session_rollbacks_on_exception():
    import pytest
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from src.storage.db import init_db, get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(engine=engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with pytest.raises(ValueError, match="test error"):
        async with get_session(session_factory=factory) as _session:
            raise ValueError("test error")

    await engine.dispose()
```

### Step 2: Run to verify they fail

```bash
pytest tests/test_storage.py::test_init_db_creates_tables -v
```

Expected: FAIL with `ImportError: cannot import name 'init_db' from 'src.storage.db'`.

### Step 3: Implement db.py

Replace the contents of `src/storage/db.py` with:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.storage.models import Base


def _adapt_url(url: str) -> str:
    """Add the async driver prefix to a SQLAlchemy URL if not already present.

    SQLite requires the 'aiosqlite' driver; PostgreSQL requires 'asyncpg'.
    Leaves already-adapted URLs unchanged.

    Args:
        url: A standard SQLAlchemy database URL string.

    Returns:
        URL with the appropriate async driver prefix inserted.
    """
    if url.startswith("sqlite") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def make_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async SQLAlchemy engine from the given URL or settings.

    Args:
        database_url: Optional explicit URL. If None, reads from settings.

    Returns:
        A configured AsyncEngine instance.
    """
    if database_url is None:
        from config.settings import settings
        database_url = settings.database_url

    url = _adapt_url(database_url)
    logger.debug("Creating async engine: {}", url)
    return create_async_engine(url, echo=False)


# Module-level singletons — created lazily on first use so that
# importing this module never touches the database or settings at test time.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(_get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Create all tables defined in models.py if they do not already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS semantics.

    Args:
        engine: Optional engine to use. Defaults to the module-level engine
            (reads DATABASE_URL from settings). Pass a test engine in tests.
    """
    e = engine or _get_engine()
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("init_db: all tables created/verified")


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a database session.

    Commits on clean exit, rolls back on exception.

    Args:
        session_factory: Optional factory to use. Defaults to the module-level
            factory. Pass a test factory in tests.

    Yields:
        An AsyncSession ready for use.

    Example:
        async with get_session() as session:
            await insert_tick(session, tick)
    """
    factory = session_factory or _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### Step 4: Run the tests to verify they pass

```bash
pytest tests/test_storage.py -v
```

Expected: All 12 tests PASS.

### Step 5: Commit

```bash
git add src/storage/db.py tests/test_storage.py
git commit -m "feat: add async DB engine and session management"
```

---

## Task 3: Test Fixture for Query Tests

**Files:**
- Modify: `tests/conftest.py`

The `async_db_session` fixture creates a fresh in-memory SQLite database for each test. Query tests (Tasks 4–6) all use this fixture instead of touching the real engine.

### Step 1: Add imports and fixture to conftest.py

Append these imports at the top of `tests/conftest.py` (after the existing imports):

```python
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.storage.models import Base
```

Then append this fixture at the bottom of `tests/conftest.py`:

```python
@pytest_asyncio.fixture
async def async_db_session() -> AsyncSession:
    """In-memory SQLite session for storage tests.

    Creates all tables fresh for each test, yields the session,
    then disposes the engine. Tests are fully isolated.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
```

### Step 2: Verify the fixture works

```bash
pytest tests/test_storage.py -v
```

Expected: All existing tests still PASS. (The fixture will be exercised in the next tasks.)

### Step 3: Commit

```bash
git add tests/conftest.py
git commit -m "test: add async_db_session fixture for storage tests"
```

---

## Task 4: insert_chain_snapshot

**Files:**
- Create: `src/storage/queries.py`
- Modify: `tests/test_storage.py` (append new tests)

### Step 1: Write the failing tests

Append these tests to `tests/test_storage.py`:

```python
async def test_insert_chain_snapshot_returns_id(async_db_session):
    from datetime import datetime, timezone
    from src.data.chain_fetcher import OptionChainSnapshot
    from src.storage.queries import insert_chain_snapshot

    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[],
    )
    snapshot_id = await insert_chain_snapshot(async_db_session, snapshot)
    assert isinstance(snapshot_id, int)
    assert snapshot_id > 0


async def test_insert_chain_snapshot_persists_contracts(async_db_session):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.models import OptionContractRecord
    from src.storage.queries import insert_chain_snapshot

    contract = OptionContract(
        symbol="SPY",
        expiry="20260320",
        strike=500.0,
        right="C",
        con_id=12345,
        bid=1.0,
        ask=1.05,
        delta=0.5,
        implied_vol=0.25,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[contract],
    )
    snapshot_id = await insert_chain_snapshot(async_db_session, snapshot)

    result = await async_db_session.execute(
        select(OptionContractRecord).where(
            OptionContractRecord.snapshot_id == snapshot_id
        )
    )
    records = result.scalars().all()
    assert len(records) == 1
    assert records[0].symbol == "SPY"
    assert records[0].con_id == 12345
    assert records[0].bid == 1.0
    assert records[0].delta == 0.5


async def test_insert_chain_snapshot_unique_constraint(async_db_session):
    """Two contracts with the same con_id in the same snapshot should raise."""
    import pytest
    from datetime import datetime, timezone
    from sqlalchemy.exc import IntegrityError
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot

    contract = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C", con_id=99999,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[contract, contract],  # duplicate con_id
    )
    with pytest.raises(IntegrityError):
        await insert_chain_snapshot(async_db_session, snapshot)
```

### Step 2: Run to verify they fail

```bash
pytest tests/test_storage.py::test_insert_chain_snapshot_returns_id -v
```

Expected: FAIL with `ImportError: cannot import name 'insert_chain_snapshot' from 'src.storage.queries'`.

### Step 3: Implement insert_chain_snapshot in queries.py

Replace the contents of `src/storage/queries.py` with:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.chain_fetcher import OptionChainSnapshot
from src.data.tick_stream import TickUpdate
from src.storage.models import ChainSnapshot, OptionContractRecord, OptionTick


async def insert_chain_snapshot(
    session: AsyncSession, snapshot: OptionChainSnapshot
) -> int:
    """Persist an OptionChainSnapshot and all its contracts in one transaction.

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        snapshot: The pydantic OptionChainSnapshot returned by ChainFetcher.

    Returns:
        The auto-generated primary key of the new chain_snapshots row.
    """
    db_snapshot = ChainSnapshot(
        underlying=snapshot.underlying,
        underlying_price=snapshot.underlying_price,
        captured_at=snapshot.timestamp,
    )
    session.add(db_snapshot)
    await session.flush()  # populate db_snapshot.id before inserting contracts

    for c in snapshot.contracts:
        session.add(
            OptionContractRecord(
                snapshot_id=db_snapshot.id,
                symbol=c.symbol,
                expiry=c.expiry,
                strike=c.strike,
                right=c.right,
                con_id=c.con_id,
                bid=c.bid,
                ask=c.ask,
                last=c.last,
                volume=c.volume,
                open_interest=c.open_interest,
                implied_vol=c.implied_vol,
                delta=c.delta,
                gamma=c.gamma,
                theta=c.theta,
                vega=c.vega,
            )
        )

    return db_snapshot.id
```

### Step 4: Run the tests to verify they pass

```bash
pytest tests/test_storage.py -v
```

Expected: All tests PASS.

### Step 5: Commit

```bash
git add src/storage/queries.py tests/test_storage.py
git commit -m "feat: add insert_chain_snapshot query"
```

---

## Task 5: insert_tick

**Files:**
- Modify: `src/storage/queries.py` (append function)
- Modify: `tests/test_storage.py` (append new tests)

### Step 1: Write the failing tests

Append to `tests/test_storage.py`:

```python
async def test_insert_tick_returns_id(async_db_session):
    from datetime import datetime, timezone
    from src.data.tick_stream import TickUpdate
    from src.storage.queries import insert_tick

    tick = TickUpdate(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime.now(timezone.utc),
        bid=1.0,
        ask=1.05,
        last=1.02,
        last_size=10,
        underlying_price=500.0,
    )
    tick_id = await insert_tick(async_db_session, tick)
    assert isinstance(tick_id, int)
    assert tick_id > 0


async def test_insert_tick_persists_all_fields(async_db_session):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.data.tick_stream import TickUpdate
    from src.storage.models import OptionTick
    from src.storage.queries import insert_tick

    tick = TickUpdate(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime.now(timezone.utc),
        bid=1.0,
        ask=1.05,
        last=1.02,
        last_size=10,
        bid_size=50,
        ask_size=30,
        underlying_price=500.0,
        delta=0.5,
        implied_vol=0.25,
    )
    await insert_tick(async_db_session, tick)

    result = await async_db_session.execute(
        select(OptionTick).where(OptionTick.con_id == 12345)
    )
    record = result.scalar_one()
    assert record.symbol == "SPY"
    assert record.last_size == 10
    assert record.bid_size == 50
    assert record.ask_size == 30
    assert record.underlying_price == 500.0
    assert record.delta == 0.5
    assert record.implied_vol == 0.25


async def test_insert_tick_none_fields_stored_as_null(async_db_session):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.data.tick_stream import TickUpdate
    from src.storage.models import OptionTick
    from src.storage.queries import insert_tick

    tick = TickUpdate(
        symbol="AAPL",
        con_id=99999,
        expiry="20260320",
        strike=200.0,
        right="P",
        timestamp=datetime.now(timezone.utc),
        # all optional fields left as None
    )
    await insert_tick(async_db_session, tick)

    result = await async_db_session.execute(
        select(OptionTick).where(OptionTick.con_id == 99999)
    )
    record = result.scalar_one()
    assert record.bid is None
    assert record.delta is None
    assert record.last_size is None
```

### Step 2: Run to verify they fail

```bash
pytest tests/test_storage.py::test_insert_tick_returns_id -v
```

Expected: FAIL with `ImportError: cannot import name 'insert_tick'`.

### Step 3: Append insert_tick to queries.py

Add this function at the end of `src/storage/queries.py`:

```python
async def insert_tick(session: AsyncSession, tick: TickUpdate) -> int:
    """Persist one TickUpdate from the live stream.

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        tick: The pydantic TickUpdate received from TickStream.queue.

    Returns:
        The auto-generated primary key of the new option_ticks row.
    """
    db_tick = OptionTick(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        strike=tick.strike,
        right=tick.right,
        received_at=tick.timestamp,
        bid=tick.bid,
        ask=tick.ask,
        last=tick.last,
        volume=tick.volume,
        open_interest=tick.open_interest,
        last_size=tick.last_size,
        bid_size=tick.bid_size,
        ask_size=tick.ask_size,
        underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol,
        delta=tick.delta,
        gamma=tick.gamma,
        theta=tick.theta,
        vega=tick.vega,
    )
    session.add(db_tick)
    await session.flush()
    return db_tick.id
```

### Step 4: Run the tests to verify they pass

```bash
pytest tests/test_storage.py -v
```

Expected: All tests PASS.

### Step 5: Commit

```bash
git add src/storage/queries.py tests/test_storage.py
git commit -m "feat: add insert_tick query"
```

---

## Task 6: Read Queries

**Files:**
- Modify: `src/storage/queries.py` (append two functions)
- Modify: `tests/test_storage.py` (append new tests)

### Step 1: Write the failing tests

Append to `tests/test_storage.py`:

```python
async def test_get_latest_snapshot_returns_most_recent(async_db_session):
    from datetime import datetime, timezone, timedelta
    from src.data.chain_fetcher import OptionChainSnapshot
    from src.storage.queries import insert_chain_snapshot, get_latest_snapshot

    older = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=490.0,
        timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
        contracts=[],
    )
    newer = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[],
    )
    await insert_chain_snapshot(async_db_session, older)
    await insert_chain_snapshot(async_db_session, newer)

    result = await get_latest_snapshot(async_db_session, "SPY")
    assert result is not None
    assert result.underlying_price == 500.0


async def test_get_latest_snapshot_returns_none_for_unknown(async_db_session):
    from src.storage.queries import get_latest_snapshot

    result = await get_latest_snapshot(async_db_session, "UNKNOWN")
    assert result is None


async def test_get_recent_ticks_returns_within_window(async_db_session):
    from datetime import datetime, timezone, timedelta
    from src.data.tick_stream import TickUpdate
    from src.storage.queries import insert_tick, get_recent_ticks

    now = datetime.now(timezone.utc)
    recent = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=now, bid=1.0, ask=1.05,
    )
    old = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=now - timedelta(hours=2), bid=0.9, ask=0.95,
    )
    await insert_tick(async_db_session, recent)
    await insert_tick(async_db_session, old)

    results = await get_recent_ticks(async_db_session, con_id=12345, minutes=5)
    assert len(results) == 1
    assert results[0].bid == 1.0


async def test_get_recent_ticks_returns_empty_for_no_matches(async_db_session):
    from src.storage.queries import get_recent_ticks

    results = await get_recent_ticks(async_db_session, con_id=99999, minutes=5)
    assert results == []
```

### Step 2: Run to verify they fail

```bash
pytest tests/test_storage.py::test_get_latest_snapshot_returns_most_recent -v
```

Expected: FAIL with `ImportError: cannot import name 'get_latest_snapshot'`.

### Step 3: Append read functions to queries.py

Add these two functions at the end of `src/storage/queries.py`:

```python
async def get_latest_snapshot(
    session: AsyncSession, underlying: str
) -> ChainSnapshot | None:
    """Fetch the most recent chain snapshot for a given underlying.

    Args:
        session: Active AsyncSession.
        underlying: Ticker symbol, e.g. "SPY".

    Returns:
        The most recent ChainSnapshot row, or None if none exist.
    """
    result = await session.execute(
        select(ChainSnapshot)
        .where(ChainSnapshot.underlying == underlying)
        .order_by(ChainSnapshot.captured_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_recent_ticks(
    session: AsyncSession, con_id: int, minutes: int = 1
) -> list[OptionTick]:
    """Fetch tick records for a contract within the last N minutes.

    Used by flow_classifier to retrieve recent activity for a contract.

    Args:
        session: Active AsyncSession.
        con_id: IBKR contract ID to filter by.
        minutes: Lookback window in minutes (default 1).

    Returns:
        List of OptionTick rows ordered by received_at ascending.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    result = await session.execute(
        select(OptionTick)
        .where(OptionTick.con_id == con_id, OptionTick.received_at >= since)
        .order_by(OptionTick.received_at.asc())
    )
    return list(result.scalars().all())
```

### Step 4: Run all tests to verify they pass

```bash
pytest tests/test_storage.py -v
```

Expected: All tests PASS.

### Step 5: Commit

```bash
git add src/storage/queries.py tests/test_storage.py
git commit -m "feat: add get_latest_snapshot and get_recent_ticks queries"
```

---

## Task 7: Storage __init__.py Exports

**Files:**
- Modify: `src/storage/__init__.py`

### Step 1: Write the failing test

Append to `tests/test_storage.py`:

```python
def test_storage_package_exports():
    from src.storage import (
        Base,
        ChainSnapshot,
        OptionContractRecord,
        OptionTick,
        get_session,
        init_db,
        insert_chain_snapshot,
        insert_tick,
        get_latest_snapshot,
        get_recent_ticks,
    )
    assert all([
        Base, ChainSnapshot, OptionContractRecord, OptionTick,
        get_session, init_db,
        insert_chain_snapshot, insert_tick,
        get_latest_snapshot, get_recent_ticks,
    ])
```

### Step 2: Run to verify it fails

```bash
pytest tests/test_storage.py::test_storage_package_exports -v
```

Expected: FAIL with `ImportError`.

### Step 3: Implement src/storage/__init__.py

Replace the contents of `src/storage/__init__.py` with:

```python
from __future__ import annotations

from src.storage.db import get_session, init_db
from src.storage.models import Base, ChainSnapshot, OptionContractRecord, OptionTick
from src.storage.queries import (
    get_latest_snapshot,
    get_recent_ticks,
    insert_chain_snapshot,
    insert_tick,
)

__all__ = [
    "Base",
    "ChainSnapshot",
    "OptionContractRecord",
    "OptionTick",
    "get_session",
    "init_db",
    "insert_chain_snapshot",
    "insert_tick",
    "get_latest_snapshot",
    "get_recent_ticks",
]
```

### Step 4: Run all storage tests

```bash
pytest tests/test_storage.py -v
```

Expected: All tests PASS.

### Step 5: Run the full test suite to check for regressions

```bash
pytest -v
```

Expected: All tests PASS (storage tests + existing tick_stream, connection tests).

### Step 6: Commit

```bash
git add src/storage/__init__.py tests/test_storage.py
git commit -m "feat: export storage layer public API from __init__.py"
```

---

## Done

All 7 tasks complete. The storage layer is fully implemented and tested:
- `src/storage/models.py` — 3 ORM models with indexes and constraints
- `src/storage/db.py` — lazy async engine, session factory, `init_db()`, `get_session()`
- `src/storage/queries.py` — `insert_chain_snapshot`, `insert_tick`, `get_latest_snapshot`, `get_recent_ticks`
- `tests/test_storage.py` — full test coverage
- `tests/conftest.py` — `async_db_session` fixture for isolated in-memory DB tests
