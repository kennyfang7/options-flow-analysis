# tick_stream.py Design
**Date:** 2026-03-05
**Module:** `src/data/tick_stream.py`
**Build Order Step:** 5 of 14

---

## Overview

Real-time options tick streaming via IBKR `reqMktData` + `pendingTickersEvent`.
Subscribes to a set of qualified option contracts and emits `TickUpdate` domain objects
into an `asyncio.Queue` for downstream consumers (flow classifier, storage, alerts).

---

## Approach

Event-driven via `ib_insync`'s `pendingTickersEvent`. IBKR pushes updates; the handler
converts raw `Ticker` objects to `TickUpdate` models and puts them in a bounded queue.
No polling.

---

## Domain Model: `TickUpdate`

```python
class TickUpdate(BaseModel):
    symbol: str
    con_id: int
    expiry: str
    strike: float
    right: str                   # "C" or "P"
    timestamp: datetime          # UTC, when tick received

    bid: float | None
    ask: float | None
    last: float | None
    volume: int | None
    open_interest: int | None
    last_size: int | None        # size of most recent trade (for flow classification)
    bid_size: int | None
    ask_size: int | None
    underlying_price: float | None  # for premium calc in steps 8-10

    implied_vol: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None

    @computed_field mid -> float | None  # (bid + ask) / 2
```

---

## Class Interface: `TickStream`

```python
class TickStreamError(Exception): ...

MAX_MKT_DATA_LINES = 95  # IBKR hard limit is 100; leave 5 headroom

class TickStream:
    def __init__(self, client: IBKRClient) -> None: ...

    async def subscribe(
        self,
        contracts: list[OptionContract],
        underlying_price: float | None = None,
    ) -> None:
        # Enforces MAX_MKT_DATA_LINES cap — raises TickStreamError if exceeded
        # Reconstructs ib_insync.Option from con_id internally
        # Calls reqMktData(contract, genericTickList="100,101", snapshot=False)
        # Stores returned Ticker keyed by con_id for cancelMktData identity
        # Hooks pendingTickersEvent once on first subscribe call

    async def unsubscribe(self, contracts: list[OptionContract] | None = None) -> None:
        # cancelMktData using stored Ticker objects (exact identity required)
        # If contracts=None, unsubscribes all
        # Removes pendingTickersEvent hook when no subscriptions remain

    @property
    def queue(self) -> asyncio.Queue[TickUpdate]: ...

    @property
    def subscribed_count(self) -> int: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(...) -> None: ...  # auto-unsubscribes
```

---

## Data Flow

```
OptionChainSnapshot.contracts (list[OptionContract], qualified with con_id)
    ↓ subscribe()
    Reconstruct ib_insync.Option per con_id
    reqMktData(contract, "100,101", snapshot=False) → Ticker (stored internally)
    Hook pendingTickersEvent

IBKR pushes update
    ↓ pendingTickersEvent fires → Set[Ticker]
    _on_pending_tickers(tickers)
        → _ticker_to_update(ticker) → TickUpdate
        → queue.put_nowait(tick_update)  # must NOT await inside event handler
        → warn + drop if queue full (QueueFull caught)
```

---

## Key Constraints

| Constraint | Detail |
|---|---|
| Market data line limit | Hard cap at `MAX_MKT_DATA_LINES = 95`; raise `TickStreamError` if exceeded |
| `genericTickList` | `"100,101"` required for volume (100) and open interest (101) |
| `put_nowait` not `await put()` | ib_insync event handlers cannot await |
| Bounded queue | `maxsize=1000`; drop oldest + log warning on full |
| `cancelMktData` identity | Must pass exact `Ticker` object returned by `reqMktData` |
| Input type | Accepts `list[OptionContract]`; reconstructs raw `Option` from `con_id` internally |

---

## Error Handling

- `TickStreamError`: subscription cap exceeded, unqualified contract (con_id is None)
- `queue.put_nowait` QueueFull: log warning, drop tick (never block event loop)
- Disconnect: IBKRClient handles reconnection; subscriptions will need resubscribe after reconnect

---

## Testing Strategy

- Unit tests with mocked `IBKRClient` — no live TWS required
- Manually fire `pendingTickersEvent` with fake `Ticker` objects to test handler
- Assert `TickUpdate` fields populated correctly from mock Ticker
- Assert `TickStreamError` raised when cap exceeded
- Assert `cancelMktData` called with correct Ticker identity on unsubscribe
- Integration test marked `@pytest.mark.integration` — requires live TWS

---

## Integration Points

| Step | How it uses TickStream |
|---|---|
| Step 4 (storage) | Consume queue, persist TickUpdate records |
| Step 6 (flow_classifier) | Consume queue, classify by last_size + premium |
| Step 7 (unusual_detector) | Consume queue, compare volume vs OI |
| Step 8 (greeks_engine) | Consume queue, use implied_vol + underlying_price |
| Step 9 (sentiment) | Consume queue, aggregate put/call ratios |
| Step 10 (smart_money) | Consume queue, apply institutional heuristics |
