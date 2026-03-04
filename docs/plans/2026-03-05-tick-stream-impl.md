# tick_stream.py Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/data/tick_stream.py` — a real-time options tick streaming module that subscribes to qualified option contracts via `reqMktData` and emits `TickUpdate` domain objects into an `asyncio.Queue`.

**Architecture:** Event-driven using `ib_insync`'s `pendingTickersEvent`. On each IBKR push, the handler converts raw `Ticker` objects to `TickUpdate` Pydantic models and drops them into a bounded `asyncio.Queue[TickUpdate]` (maxsize=1000). Downstream consumers (steps 6–11) read from this queue.

**Tech Stack:** Python 3.11+, `ib_insync`, `pydantic`, `asyncio`, `loguru`

---

## Reference files (read before starting)

- `src/data/chain_fetcher.py` — `OptionContract` model, `_clean`, `_clean_int` helpers, coding patterns
- `src/connection/ibkr_client.py` — `IBKRClient`, `IBKRConnectionError` pattern
- `tests/conftest.py` — existing fixtures (`mock_ib`, `mock_settings`)
- `docs/plans/2026-03-05-tick-stream-design.md` — approved design

---

## Task 1: TickStreamError + TickUpdate model

**Files:**
- Create: `src/data/tick_stream.py`
- Create: `tests/test_tick_stream.py`

**Step 1: Write the failing test**

```python
# tests/test_tick_stream.py
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.data.tick_stream import TickStreamError, TickUpdate


def _make_tick(**overrides) -> TickUpdate:
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20260320",
        strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=None, ask=None, last=None, volume=None,
        open_interest=None, last_size=None, bid_size=None,
        ask_size=None, underlying_price=None, implied_vol=None,
        delta=None, gamma=None, theta=None, vega=None,
    )
    return TickUpdate(**(defaults | overrides))


def test_tick_update_mid_both_present():
    tick = _make_tick(bid=1.00, ask=1.10)
    assert tick.mid == 1.05


def test_tick_update_mid_missing_bid():
    tick = _make_tick(bid=None, ask=1.10)
    assert tick.mid is None


def test_tick_update_mid_missing_ask():
    tick = _make_tick(bid=1.00, ask=None)
    assert tick.mid is None


def test_tick_update_right_values():
    call = _make_tick(right="C")
    put = _make_tick(right="P")
    assert call.right == "C"
    assert put.right == "P"


def test_tick_stream_error_is_exception():
    err = TickStreamError("too many subscriptions")
    assert isinstance(err, Exception)
    assert str(err) == "too many subscriptions"
```

**Step 2: Run test to verify it fails**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `tick_stream` does not exist yet.

**Step 3: Write minimal implementation**

```python
# src/data/tick_stream.py
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ib_insync import Option, Ticker
from loguru import logger
from pydantic import BaseModel, computed_field

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient
    from src.data.chain_fetcher import OptionContract


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TickStreamError(Exception):
    """Raised when tick stream subscription fails (cap exceeded, unqualified contract, etc.)."""


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

def _clean(value: float | None, sentinel: float = -1.0) -> float | None:
    """Normalize IBKR sentinel values (nan, -1) to None."""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        return None
    if value == sentinel:
        return None
    return value


def _clean_int(value: float | None) -> int | None:
    """Normalize an IBKR integer field to int or None."""
    cleaned = _clean(value)
    return int(cleaned) if cleaned is not None else None


class TickUpdate(BaseModel):
    """A single real-time market data update for one option contract.

    Emitted into TickStream.queue whenever IBKR pushes updated data for a
    subscribed contract. Designed to flow through the analysis pipeline
    (flow_classifier → unusual_detector → greeks_engine → sentiment → smart_money).

    Attributes:
        symbol: Underlying ticker symbol (e.g. "SPY").
        con_id: IBKR contract ID — unique identifier for the contract.
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
```

**Step 4: Run tests to verify they pass**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All 5 tests PASS.

**Step 5: Commit**

```bash
git add src/data/tick_stream.py tests/test_tick_stream.py
git commit -m "feat(tick_stream): add TickUpdate model and TickStreamError"
```

---

## Task 2: TickStream class skeleton

**Files:**
- Modify: `src/data/tick_stream.py`
- Modify: `tests/test_tick_stream.py`
- Modify: `tests/conftest.py`

**Step 1: Add fixture to conftest.py and write failing tests**

First, add `pendingTickersEvent` support to `mock_ib` in `tests/conftest.py`:

```python
# Add to mock_ib fixture, after the disconnectedEvent lines:
ib.pendingTickersEvent = MagicMock()
ib.pendingTickersEvent.__iadd__ = MagicMock(return_value=ib.pendingTickersEvent)
ib.pendingTickersEvent.__isub__ = MagicMock(return_value=ib.pendingTickersEvent)
ib.reqMktData = MagicMock()
ib.cancelMktData = MagicMock()
```

Then add a `mock_ibkr_client` fixture if not already present (check conftest.py first):

```python
@pytest.fixture
def mock_ibkr_client(mock_ib, mock_settings) -> MagicMock:
    """Mocked IBKRClient with a pre-configured mock IB instance."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.ib = mock_ib
    return client
```

Now add tests to `tests/test_tick_stream.py`:

```python
import asyncio
from unittest.mock import MagicMock
from src.data.tick_stream import TickStream


def test_tick_stream_queue_is_asyncio_queue(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert isinstance(stream.queue, asyncio.Queue)


def test_tick_stream_queue_maxsize(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert stream.queue.maxsize == 1000


def test_tick_stream_subscribed_count_initial(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert stream.subscribed_count == 0
```

**Step 2: Run to verify they fail**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py::test_tick_stream_queue_is_asyncio_queue -v
```

Expected: `AttributeError` — `TickStream` not defined.

**Step 3: Add TickStream skeleton to `src/data/tick_stream.py`**

Add after the `TickUpdate` class:

```python
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
        # Maps con_id → (ib_insync.Option contract, underlying_price at subscription time)
        self._subscriptions: dict[int, tuple[Option, float | None]] = {}
        # Maps con_id → Ticker returned by reqMktData (needed for cancelMktData identity)
        self._active_tickers: dict[int, Ticker] = {}
        self._event_hooked: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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
```

**Step 4: Run tests to verify they pass**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All 8 tests PASS.

**Step 5: Commit**

```bash
git add src/data/tick_stream.py tests/test_tick_stream.py tests/conftest.py
git commit -m "feat(tick_stream): add TickStream skeleton with queue and subscribed_count"
```

---

## Task 3: subscribe() method

**Files:**
- Modify: `src/data/tick_stream.py`
- Modify: `tests/test_tick_stream.py`

**Step 1: Write failing tests**

```python
import pytest
from src.data.tick_stream import TickStream, TickStreamError, MAX_MKT_DATA_LINES
from src.data.chain_fetcher import OptionContract
from datetime import datetime, timezone


def _make_contract(con_id: int = 12345, symbol: str = "SPY") -> OptionContract:
    return OptionContract(
        symbol=symbol, expiry="20260320", strike=500.0,
        right="C", con_id=con_id,
    )


@pytest.mark.asyncio
async def test_subscribe_calls_req_mkt_data(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    contracts = [_make_contract(con_id=100), _make_contract(con_id=101)]
    await stream.subscribe(contracts, underlying_price=500.0)
    assert mock_ibkr_client.ib.reqMktData.call_count == 2


@pytest.mark.asyncio
async def test_subscribe_updates_subscribed_count(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    contracts = [_make_contract(con_id=100), _make_contract(con_id=101)]
    await stream.subscribe(contracts, underlying_price=500.0)
    assert stream.subscribed_count == 2


@pytest.mark.asyncio
async def test_subscribe_hooks_pending_tickers_event_once(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    contracts = [_make_contract(con_id=100)]
    await stream.subscribe(contracts)
    # Call subscribe again — event should still only be hooked once
    await stream.subscribe([_make_contract(con_id=101)])
    assert mock_ibkr_client.ib.pendingTickersEvent.__iadd__.call_count == 1


@pytest.mark.asyncio
async def test_subscribe_raises_on_cap_exceeded(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    # Pre-fill subscriptions to the cap
    for i in range(MAX_MKT_DATA_LINES):
        stream._subscriptions[i] = (MagicMock(), None)
    with pytest.raises(TickStreamError, match="market data line limit"):
        await stream.subscribe([_make_contract(con_id=9999)])


@pytest.mark.asyncio
async def test_subscribe_skips_contracts_without_con_id(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    no_id = OptionContract(
        symbol="SPY", expiry="20260320", strike=500.0, right="C", con_id=None
    )
    await stream.subscribe([no_id])
    assert stream.subscribed_count == 0
    assert mock_ibkr_client.ib.reqMktData.call_count == 0
```

**Step 2: Run to verify they fail**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -k "subscribe" -v
```

Expected: `AttributeError` — `subscribe` not defined.

**Step 3: Implement subscribe() — add to TickStream class**

```python
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
    eligible = [c for c in contracts if c.con_id is not None]
    skipped = len(contracts) - len(eligible)
    if skipped:
        logger.warning(
            "subscribe: skipping {} contracts with no con_id", skipped
        )

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
```

**Step 4: Run tests to verify they pass**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All tests PASS.

**Step 5: Commit**

```bash
git add src/data/tick_stream.py tests/test_tick_stream.py
git commit -m "feat(tick_stream): implement subscribe() with cap enforcement and event hook"
```

---

## Task 4: _on_pending_tickers handler + _ticker_to_update

**Files:**
- Modify: `src/data/tick_stream.py`
- Modify: `tests/test_tick_stream.py`

**Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_pending_tickers_puts_tick_in_queue(mock_ibkr_client):
    """Fire the event handler directly and verify a TickUpdate lands in queue."""
    from unittest.mock import MagicMock
    from ib_insync import Ticker, Option as IbOption

    stream = TickStream(mock_ibkr_client)

    # Build a fake Ticker with the data we care about
    contract = IbOption()
    contract.conId = 12345
    contract.symbol = "SPY"
    contract.lastTradeDateOrContractMonth = "20260320"
    contract.strike = 500.0
    contract.right = "C"

    ticker = MagicMock(spec=Ticker)
    ticker.contract = contract
    ticker.bid = 1.00
    ticker.ask = 1.10
    ticker.last = 1.05
    ticker.volume = 500
    ticker.lastSize = 10
    ticker.bidSize = 50
    ticker.askSize = 30
    ticker.callOpenInterest = 1000
    ticker.putOpenInterest = 0
    ticker.modelGreeks = MagicMock()
    ticker.modelGreeks.impliedVol = 0.25
    ticker.modelGreeks.delta = 0.45
    ticker.modelGreeks.gamma = 0.02
    ticker.modelGreeks.theta = -0.05
    ticker.modelGreeks.vega = 0.10

    # Pre-register the subscription so the handler knows the underlying price
    stream._subscriptions[12345] = (MagicMock(), 500.0)

    # Fire the handler directly (simulates pendingTickersEvent firing)
    stream._on_pending_tickers({ticker})

    assert not stream.queue.empty()
    tick: TickUpdate = stream.queue.get_nowait()
    assert tick.symbol == "SPY"
    assert tick.con_id == 12345
    assert tick.bid == 1.00
    assert tick.ask == 1.10
    assert tick.mid == 1.05
    assert tick.last_size == 10
    assert tick.underlying_price == 500.0
    assert tick.delta == 0.45
    assert tick.implied_vol == 0.25


@pytest.mark.asyncio
async def test_pending_tickers_drops_unknown_contract(mock_ibkr_client):
    """Tickers not in _subscriptions are silently skipped."""
    from unittest.mock import MagicMock
    from ib_insync import Ticker, Option as IbOption

    stream = TickStream(mock_ibkr_client)

    contract = IbOption()
    contract.conId = 99999  # Not in _subscriptions

    ticker = MagicMock(spec=Ticker)
    ticker.contract = contract
    ticker.modelGreeks = None

    stream._on_pending_tickers({ticker})
    assert stream.queue.empty()


@pytest.mark.asyncio
async def test_pending_tickers_drops_on_queue_full(mock_ibkr_client):
    """When queue is full, ticks are dropped with a warning — no crash."""
    from unittest.mock import MagicMock, patch
    from ib_insync import Ticker, Option as IbOption

    stream = TickStream(mock_ibkr_client)

    contract = IbOption()
    contract.conId = 12345
    contract.symbol = "SPY"
    contract.lastTradeDateOrContractMonth = "20260320"
    contract.strike = 500.0
    contract.right = "C"

    ticker = MagicMock(spec=Ticker)
    ticker.contract = contract
    ticker.bid = 1.0
    ticker.ask = 1.1
    ticker.last = 1.05
    ticker.volume = 100
    ticker.lastSize = 1
    ticker.bidSize = 10
    ticker.askSize = 10
    ticker.callOpenInterest = 500
    ticker.putOpenInterest = 0
    ticker.modelGreeks = None

    stream._subscriptions[12345] = (MagicMock(), None)

    # Fill the queue to capacity
    for _ in range(stream.queue.maxsize):
        stream.queue.put_nowait(MagicMock())

    # This should NOT raise — it logs a warning and drops the tick
    stream._on_pending_tickers({ticker})
    assert stream.queue.full()
```

**Step 2: Run to verify they fail**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -k "pending_tickers" -v
```

Expected: `AttributeError` — `_on_pending_tickers` not defined.

**Step 3: Implement the handler — add to TickStream class**

```python
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
        con_id = ticker.contract.conId if ticker.contract else None
        if con_id not in self._subscriptions:
            continue

        _, underlying_price = self._subscriptions[con_id]
        update = self._ticker_to_update(ticker, underlying_price)
        if update is None:
            continue

        try:
            self._queue.put_nowait(update)
        except asyncio.QueueFull:
            logger.warning(
                "_on_pending_tickers: queue full (maxsize={}), dropping tick for con_id={}",
                QUEUE_MAXSIZE, con_id,
            )

def _ticker_to_update(
    self, ticker: Ticker, underlying_price: float | None
) -> TickUpdate | None:
    """Convert a raw ib_insync Ticker to a TickUpdate domain object.

    Args:
        ticker: Raw Ticker from pendingTickersEvent.
        underlying_price: Underlying price stored at subscription time.

    Returns:
        TickUpdate, or None if the contract has no conId.
    """
    c = ticker.contract
    if not c or not c.conId:
        return None

    greeks = ticker.modelGreeks
    right = getattr(c, "right", "C")

    return TickUpdate(
        symbol=c.symbol,
        con_id=c.conId,
        expiry=c.lastTradeDateOrContractMonth,
        strike=c.strike,
        right=right,
        timestamp=datetime.now(timezone.utc),
        bid=_clean(getattr(ticker, "bid", None)),
        ask=_clean(getattr(ticker, "ask", None)),
        last=_clean(getattr(ticker, "last", None)),
        volume=_clean_int(getattr(ticker, "volume", None)),
        open_interest=_clean_int(
            getattr(ticker, "callOpenInterest", None) if right == "C"
            else getattr(ticker, "putOpenInterest", None)
        ),
        last_size=_clean_int(getattr(ticker, "lastSize", None)),
        bid_size=_clean_int(getattr(ticker, "bidSize", None)),
        ask_size=_clean_int(getattr(ticker, "askSize", None)),
        underlying_price=underlying_price,
        implied_vol=_clean(greeks.impliedVol) if greeks else None,
        delta=_clean(greeks.delta) if greeks else None,
        gamma=_clean(greeks.gamma) if greeks else None,
        theta=_clean(greeks.theta) if greeks else None,
        vega=_clean(greeks.vega) if greeks else None,
    )
```

**Step 4: Run all tests**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All tests PASS.

**Step 5: Commit**

```bash
git add src/data/tick_stream.py tests/test_tick_stream.py
git commit -m "feat(tick_stream): implement event handler and _ticker_to_update converter"
```

---

## Task 5: unsubscribe() method

**Files:**
- Modify: `src/data/tick_stream.py`
- Modify: `tests/test_tick_stream.py`

**Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_unsubscribe_all_calls_cancel_mkt_data(mock_ibkr_client):
    from unittest.mock import MagicMock
    from ib_insync import Option as IbOption

    stream = TickStream(mock_ibkr_client)
    # Simulate two subscriptions already active
    fake_ticker_1 = MagicMock()
    fake_ticker_2 = MagicMock()
    stream._subscriptions[100] = (MagicMock(), None)
    stream._subscriptions[101] = (MagicMock(), None)
    stream._active_tickers[100] = fake_ticker_1
    stream._active_tickers[101] = fake_ticker_2
    stream._event_hooked = True

    await stream.unsubscribe()

    assert mock_ibkr_client.ib.cancelMktData.call_count == 2
    assert stream.subscribed_count == 0


@pytest.mark.asyncio
async def test_unsubscribe_specific_contracts(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    fake_ticker = MagicMock()
    stream._subscriptions[100] = (MagicMock(), None)
    stream._subscriptions[101] = (MagicMock(), None)
    stream._active_tickers[100] = fake_ticker
    stream._active_tickers[101] = MagicMock()
    stream._event_hooked = True

    contract_to_remove = _make_contract(con_id=100)
    await stream.unsubscribe([contract_to_remove])

    # Only con_id=100 should be cancelled
    mock_ibkr_client.ib.cancelMktData.assert_called_once_with(fake_ticker)
    assert stream.subscribed_count == 1
    assert 100 not in stream._subscriptions


@pytest.mark.asyncio
async def test_unsubscribe_removes_event_hook_when_empty(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    stream._subscriptions[100] = (MagicMock(), None)
    stream._active_tickers[100] = MagicMock()
    stream._event_hooked = True

    await stream.unsubscribe()

    mock_ibkr_client.ib.pendingTickersEvent.__isub__.assert_called_once()
    assert not stream._event_hooked
```

**Step 2: Run to verify they fail**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -k "unsubscribe" -v
```

Expected: `AttributeError` — `unsubscribe` not defined.

**Step 3: Implement unsubscribe() — add to TickStream class**

```python
async def unsubscribe(
    self, contracts: list[OptionContract] | None = None
) -> None:
    """Cancel market data subscriptions.

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
        self._ib.pendingTickersEvent -= self._on_pending_tickers
        self._event_hooked = False
        logger.debug("unsubscribe: pendingTickersEvent unhooked")
```

**Step 4: Run all tests**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All tests PASS.

**Step 5: Commit**

```bash
git add src/data/tick_stream.py tests/test_tick_stream.py
git commit -m "feat(tick_stream): implement unsubscribe() with cancelMktData identity preservation"
```

---

## Task 6: Context manager + __init__.py exports

**Files:**
- Modify: `src/data/tick_stream.py`
- Modify: `src/data/__init__.py`
- Modify: `tests/test_tick_stream.py`

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_context_manager_auto_unsubscribes(mock_ibkr_client):
    """__aexit__ should call unsubscribe() for all active subscriptions."""
    async with TickStream(mock_ibkr_client) as stream:
        stream._subscriptions[100] = (MagicMock(), None)
        stream._active_tickers[100] = MagicMock()
        stream._event_hooked = True

    # After exiting context, all subscriptions should be gone
    assert stream.subscribed_count == 0
    mock_ibkr_client.ib.cancelMktData.assert_called_once()
```

**Step 2: Run to verify it fails**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py::test_context_manager_auto_unsubscribes -v
```

Expected: `AttributeError` — `__aenter__` not defined.

**Step 3: Add context manager to TickStream class**

```python
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
```

**Step 4: Update `src/data/__init__.py`**

Read the current content first, then add exports:

```python
from .chain_fetcher import ChainFetcher, OptionChainSnapshot, OptionContract
from .tick_stream import TickStream, TickUpdate, TickStreamError

__all__ = [
    "ChainFetcher",
    "OptionChainSnapshot",
    "OptionContract",
    "TickStream",
    "TickUpdate",
    "TickStreamError",
]
```

**Step 5: Run all tests**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/test_tick_stream.py -v
```

Expected: All tests PASS.

**Step 6: Commit**

```bash
git add src/data/tick_stream.py src/data/__init__.py tests/test_tick_stream.py
git commit -m "feat(tick_stream): add context manager and export TickStream from src.data"
```

---

## Task 7: Standalone smoke test block

**Files:**
- Modify: `src/data/tick_stream.py`

**Step 1: Add `__main__` block at the bottom of `tick_stream.py`**

No test needed — this is a manual integration test.

```python
# ---------------------------------------------------------------------------
# Standalone smoke test (requires live TWS on port 7496/7497)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
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
                        "[{}] {} {} {} {:.0f} | bid={} ask={} last={} Δ={} IV={:.1%}",
                        tick_count,
                        tick.symbol, tick.expiry, tick.right, tick.strike,
                        tick.bid, tick.ask, tick.last,
                        tick.delta, tick.implied_vol or 0,
                    )

            logger.success("Smoke test complete.")

    asyncio.run(_main())
```

**Step 2: Run full test suite to confirm nothing broken**

```bash
cd "C:/Coding Projects/options-flow-analysis" && python -m pytest tests/ -v
```

Expected: All tests PASS.

**Step 3: Commit**

```bash
git add src/data/tick_stream.py
git commit -m "feat(tick_stream): add standalone smoke test block"
```

---

## Task 8: Save memory note

**Step 1: Create memory file**

```bash
mkdir -p "C:/Users/kenny/.claude/projects/C--Coding-Projects-options-flow-analysis/memory"
```

Create `memory/MEMORY.md` with a note about the completed module:

```markdown
# Options Flow Analysis — Session Memory

## Build Progress
- Step 1: config/settings.py — DONE
- Step 2: src/connection/ibkr_client.py — DONE
- Step 3: src/data/chain_fetcher.py — DONE
- Step 4: src/storage/ — SKELETON ONLY (models.py, db.py, queries.py empty)
- Step 5: src/data/tick_stream.py — DONE

## Key Patterns
- IBKRClient: async context manager, .ib property for raw IB access
- ChainFetcher: returns OptionChainSnapshot with list[OptionContract]
- TickStream: subscribe(list[OptionContract]) → asyncio.Queue[TickUpdate]
- All modules use loguru logger, from __future__ import annotations, Google docstrings
- Tests: pytest + pytest-asyncio, mock_ib and mock_ibkr_client fixtures in conftest.py
- Integration tests marked @pytest.mark.integration (require live TWS)

## Next Step
Step 6: src/analysis/flow_classifier.py
- Consumes TickUpdate from TickStream.queue
- Classifies trades: sweep, block, split, multi-leg
- Needs: last_size, bid/ask, volume from TickUpdate
```

**Step 2: Final commit**

```bash
git add memory/
git commit -m "chore: update memory with tick_stream completion and build progress"
```
