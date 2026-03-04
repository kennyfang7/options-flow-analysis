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


# ---------------------------------------------------------------------------
# TickStream skeleton tests
# ---------------------------------------------------------------------------

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
