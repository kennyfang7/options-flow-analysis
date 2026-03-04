from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ib_insync import IB
from loguru import logger

if TYPE_CHECKING:
    from config.settings import Settings


class IBKRConnectionError(ConnectionError):
    """Raised when a connection to TWS/Gateway cannot be established or verified."""


class IBKRClient:
    """Async wrapper around ib_insync.IB managing the full connection lifecycle.

    Handles connect, disconnect, health checks, and automatic reconnection with
    exponential backoff on unexpected disconnects.

    Example:
        async with IBKRClient() as client:
            accounts = await client.verify_connection()
            # use client.ib for data requests ...
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize the client with optional settings override.

        Args:
            settings: Pydantic Settings instance. If None, imports the shared
                singleton from config.settings.
        """
        if settings is None:
            from config.settings import settings as _settings
            settings = _settings

        self._settings = settings
        self._ib = IB()
        self._intentional_disconnect = False
        self._reconnect_task: asyncio.Task | None = None

        self._ib.disconnectedEvent += self._on_disconnect

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def ib(self) -> IB:
        """The underlying ib_insync.IB instance for direct data requests.

        Note:
            Do not call connect/disconnect on this object directly — use the
            IBKRClient methods to keep lifecycle management consistent.
        """
        return self._ib

    @property
    def is_connected(self) -> bool:
        """Quick synchronous check of connection state.

        Returns:
            True if the socket to TWS/Gateway is open, False otherwise.
        """
        return self._ib.isConnected()

    async def connect(self) -> None:
        """Connect to TWS or IB Gateway.

        Reads host, port, clientId, timeout, and readonly from settings.
        Registers the disconnect event handler for auto-reconnect.

        Raises:
            IBKRConnectionError: If the connection attempt fails.
        """
        host = self._settings.ibkr_host
        port = self._settings.ibkr_port
        client_id = self._settings.ibkr_client_id
        timeout = self._settings.ibkr_timeout
        readonly = self._settings.ibkr_readonly

        logger.info(
            "Connecting to TWS/Gateway at {}:{} (clientId={}, readonly={}) ...",
            host, port, client_id, readonly,
        )
        try:
            await self._ib.connectAsync(
                host=host,
                port=port,
                clientId=client_id,
                timeout=timeout,
                readonly=readonly,
            )
        except Exception as exc:
            raise IBKRConnectionError(
                f"Failed to connect to TWS/Gateway at {host}:{port} — {exc}"
            ) from exc

        self._intentional_disconnect = False
        logger.success(
            "Connected. Managed accounts: {}", self._ib.managedAccounts()
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect from TWS/Gateway.

        Sets an internal flag so the reconnection handler does not attempt
        to re-establish the connection after this call.
        """
        self._intentional_disconnect = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._ib.disconnect()
        logger.info("Disconnected from TWS/Gateway.")

    async def verify_connection(self) -> list[str]:
        """Verify the connection is alive and authenticated.

        Goes beyond checking the socket state by requesting the managed
        accounts list from TWS, confirming the session is fully established.

        Returns:
            List of managed account IDs (e.g. ["DU1234567"]).

        Raises:
            IBKRConnectionError: If not connected or no accounts are returned.
        """
        if not self.is_connected:
            raise IBKRConnectionError(
                "Not connected to TWS/Gateway. Call connect() first."
            )

        accounts = self._ib.managedAccounts()
        if not accounts:
            raise IBKRConnectionError(
                "Connected but received empty managedAccounts — "
                "check TWS API permissions."
            )

        logger.debug("verify_connection OK — accounts: {}", accounts)
        return list(accounts)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> IBKRClient:
        """Connect on entry.

        Returns:
            This IBKRClient instance.
        """
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Disconnect on exit, regardless of whether an exception occurred.

        Args:
            exc_type: Exception type, if any.
            exc_val: Exception value, if any.
            exc_tb: Traceback, if any.
        """
        await self.disconnect()

    # ------------------------------------------------------------------
    # Reconnection logic
    # ------------------------------------------------------------------

    def _on_disconnect(self) -> None:
        """Event handler called by ib_insync when the connection drops.

        Schedules the async reconnect coroutine on the running event loop.
        Does nothing if the disconnect was intentional.
        """
        if self._intentional_disconnect:
            return

        logger.warning("Unexpected disconnect detected — scheduling reconnect.")
        loop = asyncio.get_event_loop()
        self._reconnect_task = loop.create_task(self._reconnect_with_backoff())

    async def _reconnect_with_backoff(self) -> None:
        """Attempt to reconnect with exponential backoff.

        Retries up to ``ibkr_max_retries`` times. Delay starts at 1 second
        and doubles each attempt, capped at 60 seconds.
        """
        delay = 1.0
        max_delay = 60.0
        max_retries = self._settings.ibkr_max_retries

        for attempt in range(1, max_retries + 1):
            logger.info(
                "Reconnect attempt {}/{} in {:.0f}s ...",
                attempt, max_retries, delay,
            )
            await asyncio.sleep(delay)
            try:
                await self.connect()
                logger.success("Reconnected successfully on attempt {}.", attempt)
                return
            except IBKRConnectionError as exc:
                logger.warning("Reconnect attempt {} failed: {}", attempt, exc)
                delay = min(delay * 2, max_delay)

        logger.critical(
            "All {} reconnect attempts exhausted. Manual intervention required.",
            max_retries,
        )


# Shared module-level instance — import this for production use.
# Tests should instantiate IBKRClient directly with injected settings.
ibkr_client = IBKRClient()


if __name__ == "__main__":
    async def _main() -> None:
        async with IBKRClient() as client:
            accounts = await client.verify_connection()
            print(f"Connected. Managed accounts: {accounts}")

    asyncio.run(_main())
