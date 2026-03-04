from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd
from ib_insync import Option, Stock, Ticker
from loguru import logger
from pydantic import BaseModel, computed_field

if TYPE_CHECKING:
    from src.connection.ibkr_client import IBKRClient


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class OptionContract(BaseModel):
    """A single option contract with market data and greeks.

    Attributes:
        symbol: Underlying ticker symbol (e.g. "SPY").
        expiry: Expiration date in YYYYMMDD format.
        strike: Strike price.
        right: "C" for call, "P" for put.
        con_id: IBKR contract ID, set after qualification.
        bid: Best bid price. None if unavailable.
        ask: Best ask price. None if unavailable.
        last: Last traded price. None if unavailable.
        volume: Traded volume for the session. None if unavailable.
        open_interest: Open interest. None if unavailable.
        implied_vol: Implied volatility as a decimal (e.g. 0.25 = 25%). None if unavailable.
        delta: Delta greek. None if unavailable.
        gamma: Gamma greek. None if unavailable.
        theta: Theta greek. None if unavailable.
        vega: Vega greek. None if unavailable.
    """

    symbol: str
    expiry: str
    strike: float
    right: str
    con_id: int | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
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


class OptionChainSnapshot(BaseModel):
    """A full option chain snapshot for a single underlying at a point in time.

    Attributes:
        underlying: Ticker symbol of the underlying (e.g. "SPY").
        underlying_price: Current price of the underlying when snapshot was taken.
        timestamp: UTC datetime the snapshot was captured.
        contracts: All fetched option contracts.
    """

    underlying: str
    underlying_price: float
    timestamp: datetime
    contracts: list[OptionContract]

    def to_dataframe(self) -> pd.DataFrame:
        """Convert the snapshot's contracts to a pandas DataFrame.

        Returns:
            DataFrame with one row per contract and columns matching
            OptionContract fields, plus a 'mid' column.
        """
        return pd.DataFrame([c.model_dump() for c in self.contracts])


# ---------------------------------------------------------------------------
# Sentinel / missing-data helpers
# ---------------------------------------------------------------------------

def _clean(value: float | None, sentinel: float = -1.0) -> float | None:
    """Normalize IBKR sentinel values to None.

    IBKR uses float('nan') and -1 to indicate missing data. This converts
    both to None for consistent downstream handling.

    Args:
        value: Raw value from IBKR.
        sentinel: Sentinel value to treat as missing (default -1.0).

    Returns:
        The original value, or None if it is nan, None, or the sentinel.
    """
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
    """Normalize an IBKR integer field (volume, OI) to int or None.

    Args:
        value: Raw value from IBKR.

    Returns:
        Integer value, or None if missing/sentinel.
    """
    cleaned = _clean(value)
    if cleaned is None:
        return None
    return int(cleaned)


# ---------------------------------------------------------------------------
# Chain fetcher
# ---------------------------------------------------------------------------

class ChainFetcher:
    """Fetches option chain snapshots from IBKR for a given underlying.

    Uses an already-connected IBKRClient to discover valid contracts via
    reqSecDefOptParams, qualify them in batches, and retrieve market data
    snapshots including greeks.

    Example:
        async with IBKRClient() as client:
            fetcher = ChainFetcher(client)
            snapshot = await fetcher.fetch_chain("SPY")
            df = snapshot.to_dataframe()
    """

    def __init__(self, client: IBKRClient) -> None:
        """Initialize with a connected IBKRClient.

        Args:
            client: An active IBKRClient instance. Must already be connected.
        """
        self._client = client
        self._ib = client.ib

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_chain(
        self,
        symbol: str = "SPY",
        max_expiries: int = 6,
        strike_range_pct: float = 0.15,
        qual_batch_size: int = 50,
        data_batch_size: int = 45,
    ) -> OptionChainSnapshot:
        """Fetch a full option chain snapshot for the given symbol.

        Orchestrates the full pipeline: qualify underlying → get price →
        discover valid expirations/strikes → filter → build contracts →
        qualify in batches → fetch market data → parse into models.

        Args:
            symbol: Underlying ticker symbol (default "SPY").
            max_expiries: Number of nearest expirations to include (default 6).
            strike_range_pct: Fraction of underlying price defining the strike
                window on each side (default 0.15 = ±15%).
            qual_batch_size: Contracts per batch during qualification (default 50).
            data_batch_size: Contracts per batch during market data fetch (default 45).

        Returns:
            OptionChainSnapshot containing all parsed contracts.

        Raises:
            ValueError: If the underlying cannot be qualified or no chain
                parameters are found.
        """
        logger.info("fetch_chain: starting for {}", symbol)

        stock = await self._qualify_underlying(symbol)
        underlying_price = await self._get_underlying_price(stock)
        logger.info("{} current price: {:.2f}", symbol, underlying_price)

        chain_params = await self._get_chain_params(stock)
        expirations, strikes = self._select_expirations_and_strikes(
            chain_params, underlying_price, max_expiries, strike_range_pct
        )

        raw_contracts = self._build_option_contracts(symbol, expirations, strikes)
        logger.info("Built {} raw option contracts", len(raw_contracts))

        qualified = await self._qualify_contracts_batched(raw_contracts, qual_batch_size)
        logger.info("{} contracts qualified", len(qualified))

        tickers = await self._fetch_market_data(qualified, data_batch_size)
        contracts = [self._parse_ticker(t) for t in tickers]

        no_data = sum(1 for c in contracts if c.bid is None and c.ask is None)
        if no_data > len(contracts) * 0.5:
            logger.warning(
                "{}/{} contracts returned no bid/ask — check market data subscriptions",
                no_data, len(contracts),
            )

        snapshot = OptionChainSnapshot(
            underlying=symbol,
            underlying_price=underlying_price,
            timestamp=datetime.now(timezone.utc),
            contracts=contracts,
        )
        logger.success(
            "fetch_chain complete: {} contracts for {} ({} expiries, price {:.2f})",
            len(contracts), symbol, max_expiries, underlying_price,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Private pipeline steps
    # ------------------------------------------------------------------

    async def _qualify_underlying(self, symbol: str) -> Stock:
        """Qualify the underlying stock contract with IBKR.

        Args:
            symbol: Ticker symbol to qualify.

        Returns:
            Qualified ib_insync Stock contract with conId populated.

        Raises:
            ValueError: If IBKR cannot qualify the contract.
        """
        stock = Stock(symbol, "SMART", "USD")
        qualified = await self._ib.qualifyContractsAsync(stock)
        if not qualified or qualified[0].conId == 0:
            raise ValueError(f"Could not qualify underlying: {symbol}")
        logger.debug("Qualified underlying: {} (conId={})", symbol, qualified[0].conId)
        return qualified[0]

    async def _get_underlying_price(self, stock: Stock) -> float:
        """Fetch the current market price of the underlying.

        Args:
            stock: Qualified Stock contract.

        Returns:
            Current midpoint or last price of the underlying.

        Raises:
            ValueError: If price cannot be determined.
        """
        [ticker] = await self._ib.reqTickersAsync(stock)
        price = _clean(ticker.midpoint()) or _clean(ticker.last) or _clean(ticker.close)
        if price is None:
            raise ValueError(f"Could not determine price for {stock.symbol}")
        return price

    async def _get_chain_params(self, stock: Stock) -> object:
        """Retrieve option chain parameters (valid expirations and strikes).

        Calls reqSecDefOptParams and selects the SMART exchange entry, or
        falls back to the entry with the most expirations.

        Args:
            stock: Qualified Stock contract.

        Returns:
            A single OptionChainParams object with expirations and strikes.

        Raises:
            ValueError: If no chain parameters are found.
        """
        params = await self._ib.reqSecDefOptParamsAsync(
            stock.symbol, "", stock.secType, stock.conId
        )
        if not params:
            raise ValueError(f"No option chain parameters found for {stock.symbol}")

        # Pick the entry with the most strikes — SMART often returns a minimal
        # set for index ETFs like SPY; the richest data is on CBOE/AMEX etc.
        selected = max(params, key=lambda p: len(p.strikes))

        logger.debug(
            "Chain params: exchange={}, {} expirations, {} strikes",
            selected.exchange, len(selected.expirations), len(selected.strikes),
        )
        return selected

    def _select_expirations_and_strikes(
        self,
        params: object,
        underlying_price: float,
        max_expiries: int,
        strike_range_pct: float,
    ) -> tuple[list[str], list[float]]:
        """Filter expirations and strikes to a manageable subset.

        Args:
            params: OptionChainParams from _get_chain_params.
            underlying_price: Current price of the underlying.
            max_expiries: Maximum number of nearest expirations to keep.
            strike_range_pct: Fraction defining the strike window on each side.

        Returns:
            Tuple of (filtered_expirations, filtered_strikes).
        """
        sorted_expiries = sorted(params.expirations)[:max_expiries]

        low = underlying_price * (1 - strike_range_pct)
        high = underlying_price * (1 + strike_range_pct)
        filtered_strikes = sorted(s for s in params.strikes if low <= s <= high)

        logger.info(
            "Filtered to {} expiries and {} strikes (±{:.0f}% of {:.2f})",
            len(sorted_expiries), len(filtered_strikes),
            strike_range_pct * 100, underlying_price,
        )
        return sorted_expiries, filtered_strikes

    def _build_option_contracts(
        self, symbol: str, expirations: list[str], strikes: list[float]
    ) -> list[Option]:
        """Build unqualified Option contracts for every expiry/strike/right combo.

        Args:
            symbol: Underlying ticker symbol.
            expirations: Filtered expiration dates (YYYYMMDD strings).
            strikes: Filtered strike prices.

        Returns:
            List of ib_insync Option objects ready for qualification.
        """
        contracts = [
            Option(symbol, expiry, strike, right, "SMART")
            for expiry in expirations
            for strike in strikes
            for right in ("C", "P")
        ]
        return contracts

    async def _qualify_contracts_batched(
        self, contracts: list[Option], batch_size: int = 50
    ) -> list[Option]:
        """Qualify option contracts in batches, respecting IBKR rate limits.

        Args:
            contracts: Unqualified Option contracts.
            batch_size: Number of contracts per qualification batch.

        Returns:
            List of successfully qualified Option contracts (conId != 0).
        """
        qualified: list[Option] = []
        batches = [contracts[i:i + batch_size] for i in range(0, len(contracts), batch_size)]

        for idx, batch in enumerate(batches, 1):
            logger.debug("Qualifying batch {}/{} ({} contracts)", idx, len(batches), len(batch))
            result = await self._ib.qualifyContractsAsync(*batch)
            qualified.extend(c for c in result if c.conId != 0)
            if idx < len(batches):
                await asyncio.sleep(0.1)

        return qualified

    async def _fetch_market_data(
        self, contracts: list[Option], batch_size: int = 45
    ) -> list[Ticker]:
        """Fetch market data snapshots for qualified contracts in batches.

        Waits 2 seconds after each reqTickers call to allow market data
        to populate (required by ib_insync snapshot behaviour).

        Args:
            contracts: Qualified Option contracts.
            batch_size: Number of contracts per data request batch.

        Returns:
            List of ib_insync Ticker objects with populated market data.
        """
        tickers: list[Ticker] = []
        batches = [contracts[i:i + batch_size] for i in range(0, len(contracts), batch_size)]

        for idx, batch in enumerate(batches, 1):
            logger.debug("Fetching market data batch {}/{}", idx, len(batches))
            result = await self._ib.reqTickersAsync(*batch)
            tickers.extend(result)
            if idx < len(batches):
                await asyncio.sleep(0.5)

        # Allow data to populate after the final batch
        await asyncio.sleep(2)
        return tickers

    def _parse_ticker(self, ticker: Ticker) -> OptionContract:
        """Parse a raw ib_insync Ticker into a clean OptionContract model.

        Normalizes IBKR sentinel values (nan, -1) to None.

        Args:
            ticker: Raw Ticker returned from reqTickers/reqTickersAsync.

        Returns:
            OptionContract with all available fields populated.
        """
        c = ticker.contract
        greeks = ticker.modelGreeks

        return OptionContract(
            symbol=c.symbol,
            expiry=c.lastTradeDateOrContractMonth,
            strike=c.strike,
            right=c.right,
            con_id=c.conId or None,
            bid=_clean(ticker.bid),
            ask=_clean(ticker.ask),
            last=_clean(ticker.last),
            volume=_clean_int(ticker.volume),
            open_interest=_clean_int(
                ticker.callOpenInterest if c.right == "C" else ticker.putOpenInterest
            ),
            implied_vol=_clean(greeks.impliedVol) if greeks else None,
            delta=_clean(greeks.delta) if greeks else None,
            gamma=_clean(greeks.gamma) if greeks else None,
            theta=_clean(greeks.theta) if greeks else None,
            vega=_clean(greeks.vega) if greeks else None,
        )


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.connection.ibkr_client import IBKRClient

    async def _main() -> None:
        async with IBKRClient() as client:
            fetcher = ChainFetcher(client)
            snapshot = await fetcher.fetch_chain("SPY", max_expiries=2, strike_range_pct=0.05)
            df = snapshot.to_dataframe()
            print(f"\nSPY Option Chain Snapshot")
            print(f"  Underlying price : ${snapshot.underlying_price:.2f}")
            print(f"  Timestamp        : {snapshot.timestamp}")
            print(f"  Total contracts  : {len(snapshot.contracts)}")
            print(f"  Expiries         : {sorted(df['expiry'].unique())}")
            print(f"  Strike range     : {df['strike'].min():.2f} – {df['strike'].max():.2f}")
            print(f"\nSample (first 5 rows):")
            print(df[["expiry", "strike", "right", "bid", "ask", "mid", "delta", "implied_vol"]].head())

    import asyncio
    asyncio.run(_main())
