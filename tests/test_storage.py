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
