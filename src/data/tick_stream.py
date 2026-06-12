from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ib_insync import Option, Ticker
from loguru import logger
from pydantic import BaseModel, ValidationError, computed_field, field_validator

from src.data.chain_fetcher import _clean, _clean_int, OptionContract  # shared IBKR sentinel helpers
from src.utils.validators import (
    clamp_delta,
    clamp_implied_vol,
    has_any_price,
    is_bid_ask_consistent,
    sanitize_right,
    is_expiry_valid,
    is_strike_valid,
)

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient

from src.connection.rate_limiter import RateLimiter


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

    @field_validator("con_id")
    @classmethod
    def con_id_must_be_positive(cls, v: int) -> int:
        """Reject IBKR sentinel con_id=0 (unqualified contract)."""
        if v <= 0:
            raise ValueError(f"con_id must be > 0, got {v}")
        return v

    @field_validator("strike")
    @classmethod
    def strike_must_be_positive(cls, v: float) -> float:
        """Reject strike prices that are zero or negative."""
        if not is_strike_valid(v):
            raise ValueError(f"strike must be > 0, got {v}")
        return v

    @field_validator("right", mode="before")
    @classmethod
    def right_must_be_call_or_put(cls, v: str) -> str:
        """Normalise right to uppercase 'C' or 'P'; reject anything else."""
        return sanitize_right(v)

    @field_validator("expiry")
    @classmethod
    def expiry_must_be_valid_date(cls, v: str) -> str:
        """Reject malformed or non-calendar YYYYMMDD expiry strings."""
        if not is_expiry_valid(v):
            raise ValueError(f"expiry must be a valid YYYYMMDD date, got {v!r}")
        return v

    @field_validator("volume", "last_size", "bid_size", "ask_size", mode="before")
    @classmethod
    def coerce_negative_int_to_none(cls, v: int | None) -> int | None:
        """Coerce negative integer fields to None."""
        if v is not None and v < 0:
            return None
        return v

    @field_validator("implied_vol", mode="before")
    @classmethod
    def clamp_iv(cls, v: float | None) -> float | None:
        """Silently coerce out-of-range IV to None."""
        return clamp_implied_vol(v)

    @field_validator("delta", mode="before")
    @classmethod
    def clamp_delta_field(cls, v: float | None) -> float | None:
        """Silently coerce out-of-range delta to None."""
        return clamp_delta(v)

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

    def __init__(self, client: IBKRClient, limiter: RateLimiter | None = None) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
            limiter: Shared RateLimiter instance. If None, a new one is created.
                Pass the same limiter to ChainFetcher, TickStream, and MarketScanner
                so the 48 msg/sec budget is enforced across all three.
        """
        self._client = client
        self._ib = client.ib
        self._limiter = limiter if limiter is not None else RateLimiter()
        self._queue: asyncio.Queue[TickUpdate] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        # Maps con_id -> (ib_insync.Option contract, underlying_price at subscription time)
        self._subscriptions: dict[int, tuple[Option, float | None]] = {}
        # Maps con_id -> Ticker returned by reqMktData (needed for cancelMktData identity)
        self._active_tickers: dict[int, Ticker] = {}
        self._event_hooked: bool = False
        self._hook_lock: asyncio.Lock = asyncio.Lock()
        self._dropped_ticks: int = 0

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

    @property
    def dropped_ticks(self) -> int:
        """Cumulative count of ticks dropped due to a full queue.

        Incremented each time put_nowait raises QueueFull. Use this to
        detect sustained back-pressure (consumer too slow or queue too small).

        Returns:
            Total dropped ticks since this TickStream was created.
        """
        return self._dropped_ticks

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

            await self._limiter.acquire()
            ticker = self._ib.reqMktData(
                ibkr_contract,
                genericTickList=GENERIC_TICK_LIST,
                snapshot=False,
                regulatorySnapshot=False,
            )
            self._active_tickers[contract.con_id] = ticker
            self._subscriptions[contract.con_id] = (ibkr_contract, underlying_price)

        async with self._hook_lock:
            if not self._event_hooked and self._subscriptions:
                self._ib.pendingTickersEvent += self._on_pending_tickers
                self._event_hooked = True
                logger.debug("subscribe: pendingTickersEvent hooked")

        logger.info(
            "subscribe: {} active subscriptions ({} new this call)",
            self.subscribed_count, new_count,
        )

    async def unsubscribe(
        self, contracts: list[OptionContract] | None = None
    ) -> None:
        """Cancel market data subscriptions.

        Cancels reqMktData using the stored Ticker identity (required by ib_insync).
        Removes the event hook when no subscriptions remain.

        Args:
            contracts: Specific contracts to unsubscribe. If None, unsubscribes all.
        """
        if contracts is None:
            con_ids_to_remove = list(self._subscriptions.keys())
        else:
            con_ids_to_remove = [
                c.con_id for c in contracts
                if c.con_id is not None and c.con_id in self._subscriptions
            ]

        for con_id in con_ids_to_remove:
            ticker = self._active_tickers.pop(con_id, None)
            if ticker is not None:
                self._ib.cancelMktData(ticker)
            self._subscriptions.pop(con_id, None)

        logger.info(
            "unsubscribe: removed {} subscriptions, {} remaining",
            len(con_ids_to_remove), self.subscribed_count,
        )

        if self._event_hooked and not self._subscriptions:
            try:
                self._ib.pendingTickersEvent -= self._on_pending_tickers
            except ValueError:
                logger.warning("unsubscribe: pendingTickersEvent handler was not registered")
            self._event_hooked = False
            logger.debug("unsubscribe: pendingTickersEvent unhooked")

    async def __aenter__(self) -> TickStream:
        """Return self — connection is managed by IBKRClient.

        Returns:
            This TickStream instance.
        """
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Unsubscribe all active market data lines on exit.

        Args:
            exc_type: Exception type, if any.
            exc_val: Exception value, if any.
            exc_tb: Traceback, if any.
        """
        await self.unsubscribe()

    # ------------------------------------------------------------------
    # Internal event handler
    # ------------------------------------------------------------------

    def _on_pending_tickers(self, tickers: set[Ticker]) -> None:
        """Handle the pendingTickersEvent fired by ib_insync.

        Called on the ib_insync event loop — MUST NOT await anything.
        Converts each updated Ticker to a TickUpdate and puts it in the queue.
        Drops ticks (with a warning) when the queue is full.

        Args:
            tickers: Set of Ticker objects updated in the last network packet.
        """
        for ticker in tickers:
            if not ticker.contract or not ticker.contract.conId:
                continue
            con_id = ticker.contract.conId
            if con_id not in self._subscriptions:
                continue

            _, underlying_price = self._subscriptions[con_id]
            update = self._ticker_to_update(ticker, underlying_price)
            if update is None:
                continue

            try:
                self._queue.put_nowait(update)
            except asyncio.QueueFull:
                self._dropped_ticks += 1
                logger.warning(
                    "_on_pending_tickers: queue full (maxsize={}), dropping tick for con_id={} (total dropped={})",
                    QUEUE_MAXSIZE, con_id, self._dropped_ticks,
                )

    def _ticker_to_update(
        self, ticker: Ticker, underlying_price: float | None
    ) -> TickUpdate | None:
        """Convert a raw ib_insync Ticker to a TickUpdate domain object.

        Applies a validation gate: ticks with no actionable price data are
        dropped; ValidationErrors are caught and the tick is dropped with a
        log entry rather than crashing the synchronous event handler.

        Args:
            ticker: Raw Ticker from pendingTickersEvent.
            underlying_price: Underlying price stored at subscription time.

        Returns:
            TickUpdate, or None if the contract has no conId, has no price
            data, or fails model validation.
        """
        c = ticker.contract
        if not c or not c.conId:
            return None

        greeks = ticker.modelGreeks
        right = getattr(c, "right", "C")

        raw_bid = _clean(getattr(ticker, "bid", None))
        raw_ask = _clean(getattr(ticker, "ask", None))
        raw_last = _clean(getattr(ticker, "last", None))

        if not has_any_price(raw_bid, raw_ask, raw_last):
            logger.debug(
                "_ticker_to_update: no price data for con_id={} — dropping tick",
                c.conId,
            )
            return None

        if not is_bid_ask_consistent(raw_bid, raw_ask):
            logger.warning(
                "_ticker_to_update: inverted spread for con_id={} (bid={} > ask={}) — clearing both",
                c.conId, raw_bid, raw_ask,
            )
            raw_bid = None
            raw_ask = None

        try:
            return TickUpdate(
                symbol=c.symbol,
                con_id=c.conId,
                expiry=c.lastTradeDateOrContractMonth,
                strike=c.strike,
                right=right,
                timestamp=datetime.now(timezone.utc),
                bid=raw_bid,
                ask=raw_ask,
                last=raw_last,
                volume=_clean_int(getattr(ticker, "optVolume", None)),
                open_interest=_clean_int(getattr(ticker, "optOpenInterest", None)),
                last_size=_clean_int(getattr(ticker, "lastSize", None)),
                bid_size=_clean_int(getattr(ticker, "bidSize", None)),
                ask_size=_clean_int(getattr(ticker, "askSize", None)),
                underlying_price=(_clean(greeks.undPrice) if greeks else None) or underlying_price,
                implied_vol=_clean(greeks.impliedVol) if greeks else None,
                delta=_clean(greeks.delta) if greeks else None,
                gamma=_clean(greeks.gamma) if greeks else None,
                theta=_clean(greeks.theta) if greeks else None,
                vega=_clean(greeks.vega) if greeks else None,
            )
        except ValidationError as exc:
            logger.error(
                "_ticker_to_update: validation failed for con_id={} — dropping tick. errors={}",
                c.conId, exc.errors(),
            )
            return None


# ---------------------------------------------------------------------------
# Standalone smoke test (requires live TWS on port 7496/7497)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.connection.ibkr_client import IBKRClient
    from src.data.chain_fetcher import ChainFetcher

    async def _main() -> None:
        async with IBKRClient() as client:
            fetcher = ChainFetcher(client)
            logger.info("Fetching SPY chain (2 expiries, ±5% strikes)...")
            snapshot = await fetcher.fetch_chain("SPY", max_expiries=2, strike_range_pct=0.05)
            contracts = [c for c in snapshot.contracts if c.con_id]
            logger.info("Subscribing to {} contracts...", len(contracts))

            async with TickStream(client) as stream:
                await stream.subscribe(contracts, underlying_price=snapshot.underlying_price)
                logger.success(
                    "Streaming {} contracts. Waiting for 10 ticks...",
                    stream.subscribed_count,
                )

                tick_count = 0
                while tick_count < 10:
                    tick = await asyncio.wait_for(stream.queue.get(), timeout=30)
                    tick_count += 1
                    logger.info(
                        "[{}] {} {} {} {:.0f} | bid={} ask={} last={} delta={} IV={:.1%}",
                        tick_count,
                        tick.symbol, tick.expiry, tick.right, tick.strike,
                        tick.bid, tick.ask, tick.last,
                        tick.delta, tick.implied_vol or 0,
                    )

            logger.success("Smoke test complete.")

    asyncio.run(_main())
