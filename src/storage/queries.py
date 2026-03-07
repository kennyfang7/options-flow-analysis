from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.chain_fetcher import OptionChainSnapshot
from src.data.tick_stream import TickUpdate
from src.storage.models import ChainSnapshot, OptionContractRecord, OptionTick


async def insert_chain_snapshot(
    session: AsyncSession, snapshot: OptionChainSnapshot
) -> int:
    """Persist an OptionChainSnapshot and all its qualified contracts in one transaction.

    Skips contracts where con_id is None — unqualified contracts bypass the
    unique constraint (SQL NULL != NULL) and cannot be used by the analysis pipeline.

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        snapshot: The pydantic OptionChainSnapshot returned by ChainFetcher.

    Returns:
        The auto-generated primary key of the new chain_snapshots row.
    """
    db_snapshot = ChainSnapshot(
        underlying=snapshot.underlying,
        underlying_price=snapshot.underlying_price,
        captured_at=snapshot.timestamp,  # OptionChainSnapshot.timestamp → captured_at
    )
    session.add(db_snapshot)
    await session.flush()  # populate db_snapshot.id before inserting contracts

    for c in snapshot.contracts:
        if c.con_id is None:
            continue  # skip unqualified contracts (see models.py comment on con_id)
        session.add(
            OptionContractRecord(
                snapshot_id=db_snapshot.id,
                symbol=c.symbol,
                expiry=c.expiry,
                strike=c.strike,
                right=c.right,
                con_id=c.con_id,
                bid=c.bid,
                ask=c.ask,
                last=c.last,
                volume=c.volume,
                open_interest=c.open_interest,
                implied_vol=c.implied_vol,
                delta=c.delta,
                gamma=c.gamma,
                theta=c.theta,
                vega=c.vega,
            )
        )

    return db_snapshot.id


async def insert_tick(session: AsyncSession, tick: TickUpdate) -> int:
    """Persist one TickUpdate from the live stream.

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        tick: The pydantic TickUpdate received from TickStream.queue.

    Returns:
        The auto-generated primary key of the new option_ticks row.
    """
    db_tick = OptionTick(
        symbol=tick.symbol,
        con_id=tick.con_id,
        expiry=tick.expiry,
        strike=tick.strike,
        right=tick.right,
        received_at=tick.timestamp,  # TickUpdate.timestamp → received_at
        bid=tick.bid,
        ask=tick.ask,
        last=tick.last,
        volume=tick.volume,
        open_interest=tick.open_interest,
        last_size=tick.last_size,
        bid_size=tick.bid_size,
        ask_size=tick.ask_size,
        underlying_price=tick.underlying_price,
        implied_vol=tick.implied_vol,
        delta=tick.delta,
        gamma=tick.gamma,
        theta=tick.theta,
        vega=tick.vega,
    )
    session.add(db_tick)
    await session.flush()
    return db_tick.id


async def get_latest_snapshot(
    session: AsyncSession, underlying: str
) -> ChainSnapshot | None:
    """Fetch the most recent chain snapshot for a given underlying.

    Args:
        session: Active AsyncSession.
        underlying: Ticker symbol, e.g. "SPY".

    Returns:
        The most recent ChainSnapshot row, or None if none exist.
    """
    result = await session.execute(
        select(ChainSnapshot)
        .where(ChainSnapshot.underlying == underlying)
        .order_by(ChainSnapshot.captured_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_recent_ticks(
    session: AsyncSession, con_id: int, minutes: int = 1
) -> list[OptionTick]:
    """Fetch tick records for a contract within the last N minutes.

    Used by flow_classifier to retrieve recent activity for a contract.

    Args:
        session: Active AsyncSession.
        con_id: IBKR contract ID to filter by.
        minutes: Lookback window in minutes (default 1).

    Returns:
        List of OptionTick rows ordered by received_at ascending.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    result = await session.execute(
        select(OptionTick)
        .where(OptionTick.con_id == con_id, OptionTick.received_at >= since)
        .order_by(OptionTick.received_at.asc())
    )
    return list(result.scalars().all())
