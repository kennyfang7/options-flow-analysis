from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ib_insync import Option, Ticker
from loguru import logger
from pydantic import BaseModel, computed_field

from src.data.chain_fetcher import _clean, _clean_int, OptionContract  # shared IBKR sentinel helpers

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TickStreamError(Exception):
    """Raised when tick stream subscription fails (cap exceeded, unqualified contract, etc.)."""


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


class TickUpdate(BaseModel):
    """A single real-time market data update for one option contract.

    Emitted into TickStream.queue whenever IBKR pushes updated data for a
    subscribed contract. Designed to flow through the analysis pipeline
    (flow_classifier -> unusual_detector -> greeks_engine -> sentiment -> smart_money).

    Attributes:
        symbol: Underlying ticker symbol (e.g. "SPY").
        con_id: IBKR contract ID -- unique identifier for the contract.
        expiry: Expiration date in YYYYMMDD format.
        strike: Strike price.
        right: "C" for call, "P" for put.
        timestamp: UTC datetime when this tick was received.
        bid: Best bid price.
        ask: Best ask price.
        last: Last traded price.
        volume: Session cumulative volume.
        open_interest: Open interest.
        last_size: Size of the most recent trade (contracts). Required by flow_classifier.
        bid_size: Current bid size.
        ask_size: Current ask size.
        underlying_price: Price of the underlying at tick receipt. Required for premium calc.
        implied_vol: Implied volatility as a decimal (0.25 = 25%).
        delta: Delta greek.
        gamma: Gamma greek.
        theta: Theta greek.
        vega: Vega greek.
    """

    symbol: str
    con_id: int
    expiry: str
    strike: float
    right: str
    timestamp: datetime

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    last_size: int | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    underlying_price: float | None = None

    implied_vol: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

    @computed_field
    @property
    def mid(self) -> float | None:
        """Midpoint price between bid and ask.

        Returns:
            Mid price, or None if bid or ask is unavailable.
        """
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MKT_DATA_LINES: int = 95  # IBKR hard limit is 100; leave 5 lines headroom
QUEUE_MAXSIZE: int = 1000
GENERIC_TICK_LIST: str = "100,101"  # 100=volume, 101=open interest


# ---------------------------------------------------------------------------
# TickStream
# ---------------------------------------------------------------------------


class TickStream:
    """Real-time options tick stream via IBKR reqMktData.

    Subscribes to qualified option contracts and emits TickUpdate objects into
    a bounded asyncio.Queue. Uses ib_insync's pendingTickersEvent for
    event-driven updates (no polling).

    Enforces a hard cap of MAX_MKT_DATA_LINES (95) simultaneous subscriptions.

    Example:
        async with IBKRClient() as client:
            fetcher = ChainFetcher(client)
            snapshot = await fetcher.fetch_chain("SPY")
            contracts = [c for c in snapshot.contracts if c.con_id]

            stream = TickStream(client)
            await stream.subscribe(contracts, underlying_price=snapshot.underlying_price)

            tick = await stream.queue.get()
            # hand off to flow_classifier ...

            await stream.unsubscribe()
    """

    def __init__(self, client: IBKRClient) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
        """
        self._client = client
        self._ib = client.ib
        self._queue: asyncio.Queue[TickUpdate] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        # Maps con_id -> (ib_insync.Option contract, underlying_price at subscription time)
        self._subscriptions: dict[int, tuple[Option, float | None]] = {}
        # Maps con_id -> Ticker returned by reqMktData (needed for cancelMktData identity)
        self._active_tickers: dict[int, Ticker] = {}
        self._event_hooked: bool = False

    @property
    def queue(self) -> asyncio.Queue[TickUpdate]:
        """The output queue. Consumers read TickUpdate objects from this queue.

        Returns:
            asyncio.Queue with maxsize=1000.
        """
        return self._queue

    @property
    def subscribed_count(self) -> int:
        """Number of currently active market data subscriptions.

        Returns:
            Count of subscribed contracts.
        """
        return len(self._subscriptions)

    async def subscribe(
        self,
        contracts: list[OptionContract],
        underlying_price: float | None = None,
    ) -> None:
        """Subscribe to real-time market data for the given option contracts.

        Reconstructs ib_insync.Option objects from OptionContract.con_id internally,
        calls reqMktData for each, and hooks pendingTickersEvent on the first call.
        Enforces a hard cap of MAX_MKT_DATA_LINES simultaneous subscriptions.

        Args:
            contracts: Qualified OptionContract objects (must have con_id set).
            underlying_price: Current underlying price; stored on each TickUpdate
                for premium calculations in downstream steps.

        Raises:
            TickStreamError: If subscribing would exceed MAX_MKT_DATA_LINES, or if
                adding the new contracts would exceed the limit.
        """
        eligible = []
        for c in contracts:
            if c.con_id is None:
                logger.warning(
                    "subscribe: skipping {} {} {:.0f}{} — con_id is None",
                    c.symbol, c.expiry, c.strike, c.right,
                )
            else:
                eligible.append(c)

        new_count = len([c for c in eligible if c.con_id not in self._subscriptions])
        total_after = self.subscribed_count + new_count
        if total_after > MAX_MKT_DATA_LINES:
            raise TickStreamError(
                f"Subscribing {new_count} contracts would exceed market data line limit "
                f"({self.subscribed_count} active + {new_count} new = {total_after} > {MAX_MKT_DATA_LINES}). "
                f"Reduce the contract list or unsubscribe first."
            )

        for contract in eligible:
            if contract.con_id in self._subscriptions:
                logger.debug("subscribe: con_id={} already subscribed, skipping", contract.con_id)
                continue

            ibkr_contract = Option()
            ibkr_contract.conId = contract.con_id
            ibkr_contract.symbol = contract.symbol
            ibkr_contract.lastTradeDateOrContractMonth = contract.expiry
            ibkr_contract.strike = contract.strike
            ibkr_contract.right = contract.right
            ibkr_contract.exchange = "SMART"
            ibkr_contract.currency = "USD"
            ibkr_contract.secType = "OPT"

            ticker = self._ib.reqMktData(
                ibkr_contract,
                genericTickList=GENERIC_TICK_LIST,
                snapshot=False,
                regulatorySnapshot=False,
            )
            self._active_tickers[contract.con_id] = ticker
            self._subscriptions[contract.con_id] = (ibkr_contract, underlying_price)

        if not self._event_hooked and self._subscriptions:
            self._ib.pendingTickersEvent += self._on_pending_tickers
            self._event_hooked = True
            logger.debug("subscribe: pendingTickersEvent hooked")

        logger.info(
            "subscribe: {} active subscriptions ({} new this call)",
            self.subscribed_count, new_count,
        )

    def _on_pending_tickers(self, tickers: set) -> None:
        """Handle pendingTickersEvent from ib_insync.

        Stub — full implementation in Task 4 (tick processing pipeline).

        Args:
            tickers: Set of ib_insync.Ticker objects with fresh data.
        """
        # Implemented in Task 4.
        pass
