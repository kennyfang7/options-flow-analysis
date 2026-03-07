from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import Settings
from src.storage.models import Base


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
