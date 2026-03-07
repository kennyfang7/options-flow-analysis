from __future__ import annotations

import pytest


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


def test_option_ticks_has_symbol_index():
    from src.storage.models import OptionTick
    index_names = {i.name for i in OptionTick.__table__.indexes}
    assert "ix_option_ticks_symbol_received_at" in index_names


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
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from src.storage.db import init_db, get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(engine=engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with pytest.raises(ValueError, match="test error"):
        async with get_session(session_factory=factory) as _session:
            raise ValueError("test error")

    await engine.dispose()


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


async def test_insert_chain_snapshot_skips_none_con_id(async_db_session):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.models import OptionContractRecord
    from src.storage.queries import insert_chain_snapshot

    unqualified = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C",
        con_id=None,  # no con_id — should be skipped
    )
    qualified = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="P",
        con_id=99999,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[unqualified, qualified],
    )
    snapshot_id = await insert_chain_snapshot(async_db_session, snapshot)

    result = await async_db_session.execute(
        select(OptionContractRecord).where(
            OptionContractRecord.snapshot_id == snapshot_id
        )
    )
    records = result.scalars().all()
    assert len(records) == 1  # only the qualified one
    assert records[0].con_id == 99999


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


async def test_classified_trade_record_insert(async_db_session):
    """ClassifiedTradeRecord inserts and reads back correctly."""
    from datetime import datetime
    from src.storage.models import ClassifiedTradeRecord

    record = ClassifiedTradeRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20260320",
        strike=500.0,
        right="C",
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.45,
        trade_type="block",
        aggressor="buy",
        spread_position=0.90,
        effective_price=2.45,
        last_size=600,
        premium=147000.0,
        signal_strength=3.5,
        volume_delta=600,
        window_ticks=1,
        classified_at=datetime(2026, 3, 7, 14, 30, 0),
    )
    async_db_session.add(record)
    await async_db_session.flush()
    assert record.id is not None
    assert record.trade_type == "block"
    assert record.symbol == "SPY"
