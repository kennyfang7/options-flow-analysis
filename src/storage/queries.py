from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.analysis.flow_classifier import ClassifiedTrade
from src.analysis.unusual_detector import UnusualSignal
from src.data.chain_fetcher import OptionChainSnapshot
from src.data.tick_stream import TickUpdate
from src.storage.models import ChainSnapshot, ClassifiedTradeRecord, OptionContractRecord, OptionTick, UnusualSignalRecord


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC for SQLite storage.

    SQLite serializes datetimes as ISO strings. Mixing aware ("+00:00" suffix)
    and naive strings breaks lexicographic ORDER BY and time-window filters.
    Always strip tzinfo after converting to UTC so all stored values are uniform.

    Revisit when migrating to PostgreSQL (which has native timestamptz support).

    Args:
        dt: Datetime to normalize. May be aware or naive.

    Returns:
        Naive UTC datetime (tzinfo=None).
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


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
        captured_at=_to_naive_utc(snapshot.timestamp),  # OptionChainSnapshot.timestamp → captured_at
    )
    session.add(db_snapshot)
    await session.flush()  # populate db_snapshot.id before inserting contracts

    for c in snapshot.contracts:
        if c.con_id is None:
            logger.debug(
                "insert_chain_snapshot: skipping unqualified contract {} {} {:.0f}{} (con_id is None)",
                c.symbol, c.expiry, c.strike, c.right,
            )
            continue
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
        received_at=_to_naive_utc(tick.timestamp),  # TickUpdate.timestamp → received_at
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


async def load_chain_snapshot(
    session: AsyncSession,
    underlying: str,
    *,
    max_age_hours: float = 8.0,
) -> OptionChainSnapshot | None:
    """Load the latest chain snapshot from DB if it was captured today (ET).

    Eagerly loads the contracts relationship and reconstructs the full
    OptionChainSnapshot pydantic model. Returns None if no snapshot exists
    or the most recent one is stale (captured on a different ET calendar day
    or older than max_age_hours).

    Args:
        session: Active AsyncSession.
        underlying: Ticker symbol, e.g. "SPY".
        max_age_hours: Maximum age in hours for the snapshot to be
            considered fresh. Defaults to 8.0 (one full trading day).

    Returns:
        Reconstructed OptionChainSnapshot, or None if cache miss.
    """
    from src.data.chain_fetcher import OptionContract

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(timezone.utc).astimezone(et)

    result = await session.execute(
        select(ChainSnapshot)
        .options(selectinload(ChainSnapshot.contracts))
        .where(ChainSnapshot.underlying == underlying)
        .order_by(ChainSnapshot.captured_at.desc())
        .limit(1)
    )
    row: ChainSnapshot | None = result.scalar_one_or_none()
    if row is None:
        logger.debug("load_chain_snapshot: no snapshot found for {}", underlying)
        return None

    # SQLite quirk: DateTime(timezone=True) may return naive datetimes via aiosqlite.
    # Treat naive as UTC before converting to ET.
    captured_at = row.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_et = captured_at.astimezone(et)

    # Staleness check 1: must be same ET calendar day
    if captured_et.date() != now_et.date():
        logger.info(
            "load_chain_snapshot: stale snapshot for {} (captured {} ET, today {})",
            underlying, captured_et.date(), now_et.date(),
        )
        return None

    # Staleness check 2: must be within max_age_hours
    age = now_et - captured_et
    if age > timedelta(hours=max_age_hours):
        logger.info(
            "load_chain_snapshot: snapshot for {} too old ({:.1f}h > {:.1f}h)",
            underlying, age.total_seconds() / 3600, max_age_hours,
        )
        return None

    contracts = [
        OptionContract(
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
        for c in row.contracts
    ]

    snapshot = OptionChainSnapshot(
        underlying=row.underlying,
        underlying_price=row.underlying_price,
        timestamp=captured_at,
        contracts=contracts,
    )
    logger.success(
        "load_chain_snapshot: loaded cached snapshot for {} ({} contracts, captured {})",
        underlying, len(contracts), captured_et.strftime("%Y-%m-%d %H:%M ET"),
    )
    return snapshot


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
    since = _to_naive_utc(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    result = await session.execute(
        select(OptionTick)
        .where(OptionTick.con_id == con_id, OptionTick.received_at >= since)
        .order_by(OptionTick.received_at.asc())
    )
    return list(result.scalars().all())


async def insert_classified_trade(
    session: AsyncSession, trade: ClassifiedTrade
) -> int:
    """Persist a ClassifiedTrade emitted by FlowClassifier.

    The 'tick' field on ClassifiedTrade is intentionally excluded —
    raw tick data is persisted separately via insert_tick().

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        trade: The ClassifiedTrade returned by FlowClassifier.classify().

    Returns:
        The auto-generated primary key of the new classified_trades row.
    """
    record = ClassifiedTradeRecord(
        con_id=trade.con_id,
        symbol=trade.symbol,
        expiry=trade.expiry,
        strike=trade.strike,
        right=trade.right,
        underlying_price=trade.underlying_price,
        implied_vol=trade.implied_vol,
        delta=trade.delta,
        trade_type=trade.trade_type.value,
        aggressor=trade.aggressor.value,
        spread_position=trade.spread_position,
        effective_price=trade.effective_price,
        last_size=trade.last_size,
        premium=trade.premium,
        signal_strength=trade.signal_strength,
        volume_delta=trade.volume_delta,
        window_ticks=trade.window_ticks,
        days_to_earnings=getattr(trade, "days_to_earnings", None),
        classified_at=_to_naive_utc(trade.timestamp),
    )
    session.add(record)
    await session.flush()
    return record.id


async def insert_unusual_signal(
    session: AsyncSession, signal: UnusualSignal
) -> int:
    """Persist an UnusualSignal emitted by UnusualDetector.

    reasons is stored as a JSON array string for SQLite compatibility.
    All datetime fields are normalized to naive UTC via _to_naive_utc() before
    storage — revisit when migrating to PostgreSQL (which has native timestamptz).

    Args:
        session: Active AsyncSession (caller manages commit/rollback).
        signal: The UnusualSignal returned by UnusualDetector.detect().

    Returns:
        The auto-generated primary key of the new unusual_signals row.
    """
    record = UnusualSignalRecord(
        con_id=signal.con_id,
        symbol=signal.symbol,
        expiry=signal.expiry,
        strike=signal.strike,
        right=signal.right,
        underlying_price=signal.underlying_price,
        implied_vol=signal.implied_vol,
        delta=signal.delta,
        effective_price=signal.effective_price,
        trade_type=signal.trade_type.value,
        aggressor=signal.aggressor.value,
        premium=signal.premium,
        volume_delta=signal.volume_delta,
        signal_strength=signal.signal_strength,
        top_reason=signal.top_reason.value,
        reasons=json.dumps([r.value for r in signal.reasons]),
        days_to_earnings=getattr(signal.trade, "days_to_earnings", None),
        classified_at=_to_naive_utc(signal.trade.timestamp),
        flagged_at=_to_naive_utc(signal.flagged_at),
    )
    session.add(record)
    await session.flush()
    return record.id
