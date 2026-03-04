# tests/test_tick_stream.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.data.chain_fetcher import OptionContract
from src.data.tick_stream import MAX_MKT_DATA_LINES, TickStream, TickStreamError, TickUpdate


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


def _make_contract(con_id: int = 12345, symbol: str = "SPY") -> OptionContract:
    return OptionContract(
        symbol=symbol, expiry="20260320", strike=500.0,
        right="C", con_id=con_id,
    )


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


# ---------------------------------------------------------------------------
# TickStream skeleton tests
# ---------------------------------------------------------------------------


def test_tick_stream_queue_is_asyncio_queue(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert isinstance(stream.queue, asyncio.Queue)


def test_tick_stream_queue_maxsize(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert stream.queue.maxsize == 1000


def test_tick_stream_subscribed_count_initial(mock_ibkr_client):
    stream = TickStream(mock_ibkr_client)
    assert stream.subscribed_count == 0


# ---------------------------------------------------------------------------
# subscribe() tests
# ---------------------------------------------------------------------------


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
