from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from src.connection.ibkr_client import IBKRClient, IBKRConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(mock_ib: MagicMock, mock_settings: Settings) -> IBKRClient:
    """Return an IBKRClient with its internal IB instance replaced by mock_ib."""
    client = IBKRClient(settings=mock_settings)
    client._ib = mock_ib
    return client


# ---------------------------------------------------------------------------
# Unit tests (no TWS required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_success(mock_ib: MagicMock, mock_settings: Settings) -> None:
    """connect() succeeds when ib.connectAsync completes without error."""
    client = make_client(mock_ib, mock_settings)

    await client.connect()

    mock_ib.connectAsync.assert_awaited_once_with(
        host="127.0.0.1",
        port=7497,
        clientId=99,
        timeout=5,
        readonly=True,
    )
    assert client._intentional_disconnect is False


@pytest.mark.asyncio
async def test_connect_failure(mock_ib: MagicMock, mock_settings: Settings) -> None:
    """connect() raises IBKRConnectionError when connectAsync raises."""
    mock_ib.connectAsync.side_effect = TimeoutError("timed out")
    client = make_client(mock_ib, mock_settings)

    with pytest.raises(IBKRConnectionError, match="Failed to connect"):
        await client.connect()


@pytest.mark.asyncio
async def test_disconnect_sets_flag(mock_ib: MagicMock, mock_settings: Settings) -> None:
    """disconnect() sets _intentional_disconnect and calls ib.disconnect."""
    client = make_client(mock_ib, mock_settings)
    client._intentional_disconnect = False

    await client.disconnect()

    assert client._intentional_disconnect is True
    mock_ib.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_context_manager(mock_ib: MagicMock, mock_settings: Settings) -> None:
    """Async context manager connects on enter and disconnects on exit."""
    client = make_client(mock_ib, mock_settings)

    async with client:
        mock_ib.connectAsync.assert_awaited_once()

    mock_ib.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_verify_connection_returns_accounts(
    mock_ib: MagicMock, mock_settings: Settings
) -> None:
    """verify_connection() returns the managed accounts list when connected."""
    mock_ib.isConnected.return_value = True
    mock_ib.managedAccounts.return_value = ["DU9999999"]
    client = make_client(mock_ib, mock_settings)

    accounts = await client.verify_connection()

    assert accounts == ["DU9999999"]


@pytest.mark.asyncio
async def test_verify_connection_raises_when_disconnected(
    mock_ib: MagicMock, mock_settings: Settings
) -> None:
    """verify_connection() raises IBKRConnectionError when not connected."""
    mock_ib.isConnected.return_value = False
    client = make_client(mock_ib, mock_settings)

    with pytest.raises(IBKRConnectionError, match="Not connected"):
        await client.verify_connection()


@pytest.mark.asyncio
async def test_no_reconnect_on_intentional_disconnect(
    mock_ib: MagicMock, mock_settings: Settings
) -> None:
    """_on_disconnect() does nothing when _intentional_disconnect is True."""
    client = make_client(mock_ib, mock_settings)
    client._intentional_disconnect = True

    with patch.object(client, "_reconnect_with_backoff", new_callable=AsyncMock) as mock_reconnect:
        client._on_disconnect()
        # Give event loop a tick to potentially schedule the task
        await asyncio.sleep(0)
        mock_reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_called_on_unexpected_disconnect(
    mock_ib: MagicMock, mock_settings: Settings
) -> None:
    """_on_disconnect() schedules _reconnect_with_backoff when disconnect is unexpected."""
    client = make_client(mock_ib, mock_settings)
    client._intentional_disconnect = False

    reconnect_called = asyncio.Event()

    async def fake_reconnect() -> None:
        reconnect_called.set()

    with patch.object(client, "_reconnect_with_backoff", side_effect=fake_reconnect):
        client._on_disconnect()
        await asyncio.wait_for(reconnect_called.wait(), timeout=1.0)

    assert reconnect_called.is_set()


# ---------------------------------------------------------------------------
# Integration test (requires live TWS/Gateway)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_connection() -> None:
    """Connect to live TWS, verify managedAccounts, then disconnect cleanly.

    Requires TWS or IB Gateway to be running locally with API access enabled.
    Run with: pytest -m integration
    """
    async with IBKRClient() as client:
        accounts = await client.verify_connection()

    assert len(accounts) > 0, "Expected at least one managed account"
