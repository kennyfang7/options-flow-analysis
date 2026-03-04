from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, computed_field

from src.data.chain_fetcher import _clean, _clean_int  # shared IBKR sentinel helpers

if TYPE_CHECKING:
    from ib_insync import Option, Ticker
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
