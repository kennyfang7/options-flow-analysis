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


@pytest.mark.asyncio
async def test_get_session_rollback_discards_uncommitted_rows():
    """Rows added inside a failing session are NOT visible after rollback."""
    from datetime import datetime
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import select
    from src.storage.db import init_db, get_session
    from src.storage.models import ChainSnapshot

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(engine=engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with pytest.raises(ValueError, match="abort"):
        async with get_session(session_factory=factory) as session:
            session.add(ChainSnapshot(
                underlying="SPY",
                underlying_price=500.0,
                captured_at=datetime.utcnow(),
            ))
            raise ValueError("abort")

    # Verify the row was NOT committed — rollback was effective
    async with factory() as check:
        result = await check.execute(
            select(ChainSnapshot).where(ChainSnapshot.underlying == "SPY")
        )
        rows = result.scalars().all()
    assert rows == [], "Rolled-back row must not be visible after the session exits"

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
        load_chain_snapshot,
    )
    assert all([
        Base, ChainSnapshot, OptionContractRecord, OptionTick,
        get_session, init_db,
        insert_chain_snapshot, insert_tick,
        get_latest_snapshot, get_recent_ticks,
        load_chain_snapshot,
    ])


async def test_insert_classified_trade_returns_id(async_db_session):
    """insert_classified_trade returns an integer PK."""
    from datetime import datetime, timezone
    from src.storage import insert_classified_trade
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.45,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.90, effective_price=2.45, last_size=600,
        premium=147000.0, signal_strength=3.5, volume_delta=600,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    trade_id = await insert_classified_trade(async_db_session, trade)
    assert isinstance(trade_id, int)
    assert trade_id > 0


@pytest.mark.asyncio
async def test_insert_classified_trade_persists_fields(async_db_session):
    """Persisted ClassifiedTradeRecord matches the source ClassifiedTrade."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_classified_trade
    from src.storage.models import ClassifiedTradeRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.data.tick_stream import TickUpdate

    ts = datetime.now(timezone.utc)
    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=ts,
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.45,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.90, effective_price=2.45, last_size=600,
        premium=147000.0, signal_strength=3.5, volume_delta=600,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    trade_id = await insert_classified_trade(async_db_session, trade)

    result = await async_db_session.execute(
        select(ClassifiedTradeRecord).where(ClassifiedTradeRecord.id == trade_id)
    )
    record = result.scalar_one()

    assert record.symbol == "SPY"
    assert record.con_id == 12345
    assert record.trade_type == "block"
    assert record.aggressor == "buy"
    assert record.premium == pytest.approx(147000.0)
    assert record.volume_delta == 600
    assert record.classified_at == ts.replace(tzinfo=None)  # H2: stored as naive UTC


@pytest.mark.asyncio
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
        classified_at=datetime.now(),
    )
    async_db_session.add(record)
    await async_db_session.flush()
    assert record.id is not None
    assert record.trade_type == "block"
    assert record.symbol == "SPY"


@pytest.mark.asyncio
async def test_unusual_signal_record_insert(async_db_session):
    """UnusualSignalRecord inserts and reads back correctly."""
    import json
    from datetime import datetime
    from src.storage.models import UnusualSignalRecord

    record = UnusualSignalRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20260320",
        strike=500.0,
        right="C",
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.20,
        effective_price=2.45,
        trade_type="block",
        aggressor="buy",
        premium=600.0,
        volume_delta=60,
        signal_strength=1.0,
        top_reason="premium_size",
        reasons=json.dumps(["premium_size"]),
        classified_at=datetime.now(),
        flagged_at=datetime.now(),
    )
    async_db_session.add(record)
    await async_db_session.flush()
    assert record.id is not None
    assert record.top_reason == "premium_size"
    assert json.loads(record.reasons) == ["premium_size"]


@pytest.mark.asyncio
async def test_insert_unusual_signal_returns_id(async_db_session):
    """insert_unusual_signal returns a positive integer PK."""
    import json
    from datetime import datetime, timezone
    from src.storage import insert_unusual_signal
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    ts = datetime.now(timezone.utc)
    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=ts,
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.20,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.9, effective_price=2.45, last_size=600,
        premium=600.0, signal_strength=1.0, volume_delta=60,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    signal = UnusualSignal(
        symbol=trade.symbol, con_id=trade.con_id, expiry=trade.expiry,
        right=trade.right, strike=trade.strike, trade_type=trade.trade_type,
        aggressor=trade.aggressor, premium=trade.premium,
        volume_delta=trade.volume_delta, signal_strength=trade.signal_strength,
        delta=trade.delta, underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol, effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime.now(timezone.utc),
        trade=trade,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)
    assert isinstance(signal_id, int)
    assert signal_id > 0


@pytest.mark.asyncio
async def test_insert_unusual_signal_persists_fields(async_db_session):
    """Persisted UnusualSignalRecord matches the source UnusualSignal."""
    import json
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_unusual_signal
    from src.storage.models import UnusualSignalRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    ts = datetime.now(timezone.utc)
    ts_flagged = ts
    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260320", strike=500.0, right="C",
        timestamp=ts,
        bid=2.00, ask=2.50, last=2.45, volume=600, open_interest=1000,
        last_size=600, underlying_price=500.0, implied_vol=0.25, delta=0.20,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol, delta=tick.delta,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.9, effective_price=2.45, last_size=600,
        premium=600.0, signal_strength=1.0, volume_delta=60,
        window_ticks=1, timestamp=tick.timestamp, tick=tick,
    )
    signal = UnusualSignal(
        symbol=trade.symbol, con_id=trade.con_id, expiry=trade.expiry,
        right=trade.right, strike=trade.strike, trade_type=trade.trade_type,
        aggressor=trade.aggressor, premium=trade.premium,
        volume_delta=trade.volume_delta, signal_strength=trade.signal_strength,
        delta=trade.delta, underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol, effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE, UnusualReason.OI_RATIO],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=ts_flagged,
        trade=trade,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)

    result = await async_db_session.execute(
        select(UnusualSignalRecord).where(UnusualSignalRecord.id == signal_id)
    )
    record = result.scalar_one()

    assert record.symbol == "SPY"
    assert record.con_id == 12345
    assert record.trade_type == "block"
    assert record.aggressor == "buy"
    assert record.top_reason == "premium_size"
    assert json.loads(record.reasons) == ["premium_size", "oi_ratio"]
    assert record.premium == pytest.approx(600.0)
    assert record.volume_delta == 60
    assert record.classified_at == ts.replace(tzinfo=None)    # H2: stored as naive UTC
    assert record.flagged_at == ts_flagged.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# load_chain_snapshot tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_chain_snapshot_cache_hit_same_day(async_db_session):
    """Returns reconstructed OptionChainSnapshot when a same-day snapshot exists."""
    from datetime import datetime, timezone
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    contract = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C",
        con_id=12345, bid=1.0, ask=1.05, delta=0.5, implied_vol=0.25,
        volume=100, open_interest=5000,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[contract],
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is not None
    assert result.underlying == "SPY"
    assert result.underlying_price == 500.0
    assert len(result.contracts) == 1
    assert result.contracts[0].con_id == 12345
    assert result.contracts[0].bid == 1.0
    assert result.contracts[0].delta == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_load_chain_snapshot_cache_miss_stale(async_db_session):
    """Returns None when snapshot is from a previous day."""
    from datetime import datetime, timezone, timedelta
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    contract = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C", con_id=12345,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=490.0,
        timestamp=datetime.now(timezone.utc) - timedelta(days=1),
        contracts=[contract],
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is None


@pytest.mark.asyncio
async def test_load_chain_snapshot_cache_miss_empty_db(async_db_session):
    """Returns None when no snapshots exist at all."""
    from src.storage.queries import load_chain_snapshot

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is None


@pytest.mark.asyncio
async def test_load_chain_snapshot_reconstructs_all_fields(async_db_session):
    """All OptionContract fields survive the round-trip through the DB."""
    from datetime import datetime, timezone
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    contract = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C",
        con_id=12345, bid=1.0, ask=1.05, last=1.02,
        volume=100, open_interest=5000,
        implied_vol=0.25, delta=0.50, gamma=0.03, theta=-0.05, vega=0.12,
    )
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[contract],
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is not None
    c = result.contracts[0]
    assert c.symbol == "SPY"
    assert c.expiry == "20260320"
    assert c.strike == 500.0
    assert c.right == "C"
    assert c.con_id == 12345
    assert c.bid == pytest.approx(1.0)
    assert c.ask == pytest.approx(1.05)
    assert c.last == pytest.approx(1.02)
    assert c.volume == 100
    assert c.open_interest == 5000
    assert c.implied_vol == pytest.approx(0.25)
    assert c.delta == pytest.approx(0.50)
    assert c.gamma == pytest.approx(0.03)
    assert c.theta == pytest.approx(-0.05)
    assert c.vega == pytest.approx(0.12)
    assert c.mid == pytest.approx(1.025)


@pytest.mark.asyncio
async def test_load_chain_snapshot_max_age_hours(async_db_session):
    """Returns None when snapshot exceeds max_age_hours even if same day."""
    from datetime import datetime, timezone, timedelta
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    contract = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C", con_id=12345,
    )
    # 9 hours ago — exceeds default max_age_hours=8
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=490.0,
        timestamp=datetime.now(timezone.utc) - timedelta(hours=9),
        contracts=[contract],
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is None


@pytest.mark.asyncio
async def test_load_chain_snapshot_skips_none_con_id_contracts(async_db_session):
    """Contracts with con_id=None are skipped during insert, so not in cache."""
    from datetime import datetime, timezone
    from src.data.chain_fetcher import OptionChainSnapshot, OptionContract
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    contracts = [
        OptionContract(
            symbol="SPY", expiry="20260320", strike=500.0, right="C",
            con_id=None,  # skipped during insert
        ),
        OptionContract(
            symbol="SPY", expiry="20260320", strike=500.0, right="P",
            con_id=99999,
        ),
    ]
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=contracts,
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "SPY")
    assert result is not None
    assert len(result.contracts) == 1
    assert result.contracts[0].con_id == 99999


@pytest.mark.asyncio
async def test_load_chain_snapshot_different_symbols_isolated(async_db_session):
    """Cache lookup is per-symbol — SPY snapshot does not satisfy AAPL lookup."""
    from datetime import datetime, timezone
    from src.data.chain_fetcher import OptionChainSnapshot
    from src.storage.queries import insert_chain_snapshot, load_chain_snapshot

    snapshot = OptionChainSnapshot(
        underlying="SPY",
        underlying_price=500.0,
        timestamp=datetime.now(timezone.utc),
        contracts=[],
    )
    await insert_chain_snapshot(async_db_session, snapshot)

    result = await load_chain_snapshot(async_db_session, "AAPL")
    assert result is None


# ---------------------------------------------------------------------------
# days_to_earnings round-trip tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_classified_trade_days_to_earnings_none(async_db_session):
    """days_to_earnings defaults to None when trade has no earnings data."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_classified_trade
    from src.storage.models import ClassifiedTradeRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=11111, expiry="20260620", strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=1.0, ask=1.10, last=1.05, last_size=100, underlying_price=500.0,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=None, delta=None,
        trade_type=TradeType.UNKNOWN, aggressor=Aggressor.BUY,
        spread_position=None, effective_price=1.05, last_size=100, premium=10500.0,
        signal_strength=1.0, volume_delta=100, window_ticks=1,
        timestamp=tick.timestamp, tick=tick,
    )
    trade_id = await insert_classified_trade(async_db_session, trade)

    result = await async_db_session.execute(
        select(ClassifiedTradeRecord).where(ClassifiedTradeRecord.id == trade_id)
    )
    record = result.scalar_one()
    assert record.days_to_earnings is None


@pytest.mark.asyncio
async def test_insert_classified_trade_days_to_earnings_value(async_db_session):
    """days_to_earnings round-trips correctly when enriched trade has a value."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_classified_trade
    from src.storage.models import ClassifiedTradeRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="AAPL", con_id=22222, expiry="20260620", strike=200.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=2.0, ask=2.20, last=2.10, last_size=200, underlying_price=200.0,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=None, delta=None,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=None, effective_price=2.10, last_size=200, premium=42000.0,
        signal_strength=2.0, volume_delta=200, window_ticks=1,
        timestamp=tick.timestamp, tick=tick,
    )
    enriched = EnrichedTrade(
        **trade.model_dump(exclude={"tick"}),
        tick=trade.tick,
        days_to_expiry=10,
        moneyness=Moneyness.ATM,
        iv_source="unavailable",
        days_to_earnings=3,
    )
    trade_id = await insert_classified_trade(async_db_session, enriched)

    result = await async_db_session.execute(
        select(ClassifiedTradeRecord).where(ClassifiedTradeRecord.id == trade_id)
    )
    record = result.scalar_one()
    assert record.days_to_earnings == 3


@pytest.mark.asyncio
async def test_insert_unusual_signal_days_to_earnings_none(async_db_session):
    """days_to_earnings stored as NULL when signal's trade has no earnings data."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_unusual_signal
    from src.storage.models import UnusualSignalRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=33333, expiry="20260620", strike=500.0, right="P",
        timestamp=datetime.now(timezone.utc),
        bid=3.0, ask=3.30, last=3.15, last_size=300, underlying_price=500.0,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=None, delta=None,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=None, effective_price=3.15, last_size=300, premium=94500.0,
        signal_strength=3.0, volume_delta=300, window_ticks=1,
        timestamp=tick.timestamp, tick=tick,
    )
    signal = UnusualSignal(
        symbol=trade.symbol, con_id=trade.con_id, expiry=trade.expiry,
        right=trade.right, strike=trade.strike, trade_type=trade.trade_type,
        aggressor=trade.aggressor, premium=trade.premium,
        volume_delta=trade.volume_delta, signal_strength=trade.signal_strength,
        delta=None, underlying_price=trade.underlying_price,
        implied_vol=None, effective_price=trade.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime.now(timezone.utc),
        trade=trade,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)

    result = await async_db_session.execute(
        select(UnusualSignalRecord).where(UnusualSignalRecord.id == signal_id)
    )
    record = result.scalar_one()
    assert record.days_to_earnings is None


@pytest.mark.asyncio
async def test_insert_unusual_signal_days_to_earnings_value(async_db_session):
    """days_to_earnings round-trips when trade is an EnrichedTrade with a value."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from src.storage import insert_unusual_signal
    from src.storage.models import UnusualSignalRecord
    from src.analysis.flow_classifier import ClassifiedTrade, Aggressor, TradeType
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.unusual_detector import UnusualReason, UnusualSignal
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="TSLA", con_id=44444, expiry="20260620", strike=300.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=5.0, ask=5.50, last=5.25, last_size=400, underlying_price=300.0,
    )
    trade = ClassifiedTrade(
        symbol=tick.symbol, con_id=tick.con_id, expiry=tick.expiry,
        right=tick.right, strike=tick.strike, underlying_price=tick.underlying_price,
        implied_vol=None, delta=None,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=None, effective_price=5.25, last_size=400, premium=210000.0,
        signal_strength=4.0, volume_delta=400, window_ticks=1,
        timestamp=tick.timestamp, tick=tick,
    )
    enriched = EnrichedTrade(
        **trade.model_dump(exclude={"tick"}),
        tick=trade.tick,
        days_to_expiry=5,
        moneyness=Moneyness.OTM,
        iv_source="unavailable",
        days_to_earnings=2,
    )
    signal = UnusualSignal(
        symbol=enriched.symbol, con_id=enriched.con_id, expiry=enriched.expiry,
        right=enriched.right, strike=enriched.strike, trade_type=enriched.trade_type,
        aggressor=enriched.aggressor, premium=enriched.premium,
        volume_delta=enriched.volume_delta, signal_strength=enriched.signal_strength,
        delta=None, underlying_price=enriched.underlying_price,
        implied_vol=None, effective_price=enriched.effective_price,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=datetime.now(timezone.utc),
        trade=enriched,
    )
    signal_id = await insert_unusual_signal(async_db_session, signal)

    result = await async_db_session.execute(
        select(UnusualSignalRecord).where(UnusualSignalRecord.id == signal_id)
    )
    record = result.scalar_one()
    assert record.days_to_earnings == 2
