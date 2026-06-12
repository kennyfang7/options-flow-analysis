from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import Settings
from src.analysis.flow_classifier import Aggressor, ClassifiedTrade, TradeType
from src.data.tick_stream import TickUpdate
from src.storage.models import Base


# ---------------------------------------------------------------------------
# Shared test factory helpers (plain functions — not fixtures)
# ---------------------------------------------------------------------------


def make_tick(**overrides) -> TickUpdate:
    """Factory for TickUpdate with sensible defaults for unit tests.

    All keyword arguments override individual fields. Timestamp defaults to
    datetime.now(timezone.utc) so tests are not sensitive to hardcoded dates.
    """
    defaults = dict(
        symbol="SPY",
        con_id=12345,
        expiry="20260320",
        strike=500.0,
        right="C",
        timestamp=datetime.now(timezone.utc),
        bid=2.00,
        ask=2.50,
        last=2.45,
        volume=100,
        open_interest=1000,
        last_size=50,
        underlying_price=500.0,
        implied_vol=0.25,
        delta=0.45,
    )
    defaults.update(overrides)
    return TickUpdate(**defaults)


def make_trade(tick: TickUpdate | None = None, **overrides) -> ClassifiedTrade:
    """Factory for ClassifiedTrade with sensible defaults for unit tests.

    Builds a tick via make_tick() if none is supplied. All keyword arguments
    override individual ClassifiedTrade fields; tick-derived fields (symbol,
    expiry, etc.) use the tick's values unless explicitly overridden.
    """
    if tick is None:
        tick = make_tick()
    defaults = dict(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        right=tick.right,
        strike=tick.strike,
        underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol,
        delta=tick.delta,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        spread_position=0.9,
        effective_price=2.45,
        last_size=50,
        premium=12_250.0,   # 50 * 2.45 * 100
        signal_strength=1.0,
        volume_delta=50,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


# ---------------------------------------------------------------------------
# pytest configuration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live TWS/Gateway connection (deselect with -m 'not integration')",
    )


@pytest.fixture
def mock_settings() -> Settings:
    """Settings instance with safe test defaults (paper trading port, short timeout)."""
    return Settings(
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=99,
        ibkr_timeout=5,
        ibkr_max_retries=2,
        ibkr_readonly=True,
    )


@pytest.fixture
def mock_ib() -> MagicMock:
    """Pre-configured MagicMock of ib_insync.IB with sensible defaults."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.isConnected.return_value = True
    ib.managedAccounts.return_value = ["DU9999999"]
    ib.disconnect = MagicMock()
    # Simulate the ib_insync event as a simple list that supports +=
    ib.disconnectedEvent = MagicMock()
    ib.disconnectedEvent.__iadd__ = MagicMock(return_value=ib.disconnectedEvent)
    ib.pendingTickersEvent = MagicMock()
    ib.pendingTickersEvent.__iadd__ = MagicMock(return_value=ib.pendingTickersEvent)
    ib.pendingTickersEvent.__isub__ = MagicMock(return_value=ib.pendingTickersEvent)
    ib.reqMktData = MagicMock()
    ib.cancelMktData = MagicMock()
    return ib


@pytest.fixture
def mock_ibkr_client(mock_ib, mock_settings) -> MagicMock:
    """Mocked IBKRClient with a pre-configured mock IB instance."""
    client = MagicMock()
    client.ib = mock_ib
    return client


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
