from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


class ChainSnapshot(Base):
    """One row per ChainFetcher.fetch_chain() call.

    Stores the point-in-time context for a full option chain capture.
    Related OptionContractRecord rows hold the individual contracts.
    """

    __tablename__ = "chain_snapshots"
    __table_args__ = (
        Index("ix_chain_snapshots_underlying_captured_at", "underlying", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String, nullable=False)
    underlying_price: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # maps from OptionChainSnapshot.timestamp

    contracts: Mapped[list[OptionContractRecord]] = relationship(
        "OptionContractRecord", back_populates="snapshot", cascade="all, delete-orphan"
    )


class OptionContractRecord(Base):
    """One row per OptionContract within a ChainSnapshot.

    All nullable fields mirror the source OptionContract pydantic model.
    Note: 'mid' is intentionally omitted — compute it as (bid + ask) / 2 on reads.
    """

    __tablename__ = "option_contracts"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "con_id", name="uq_snapshot_contract"),
        CheckConstraint("strike > 0", name="ck_option_contracts_strike_positive"),
        CheckConstraint("right IN ('C', 'P')", name="ck_option_contracts_right_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chain_snapshots.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
    # NULL con_ids bypass the unique constraint — insert layer must skip None con_ids
    con_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)

    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)

    snapshot: Mapped[ChainSnapshot] = relationship(
        "ChainSnapshot", back_populates="contracts"
    )



class OptionTick(Base):
    """One row per TickUpdate received from TickStream.queue.

    Stores raw streaming tick data for downstream analysis (flow_classifier,
    unusual_detector, etc.). Note: 'mid' is intentionally omitted.
    Intentionally has no foreign key to option_contracts — ticks can arrive
    for contracts not yet captured in a chain snapshot.
    """

    __tablename__ = "option_ticks"
    __table_args__ = (
        Index("ix_option_ticks_con_id_received_at", "con_id", "received_at"),
        Index("ix_option_ticks_symbol_received_at", "symbol", "received_at"),
        CheckConstraint("strike > 0", name="ck_option_ticks_strike_positive"),
        CheckConstraint("right IN ('C', 'P')", name="ck_option_ticks_right_valid"),
        CheckConstraint("con_id > 0", name="ck_option_ticks_con_id_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # maps from TickUpdate.timestamp

    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ask_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)


class ClassifiedTradeRecord(Base):
    """One row per ClassifiedTrade emitted by FlowClassifier.

    Persisted by the orchestration layer via insert_classified_trade().
    The 'tick' field from ClassifiedTrade is intentionally omitted —
    raw tick data lives in option_ticks; this table stores the derived result.

    trade_type and aggressor are stored as plain strings (enum values)
    for SQLite compatibility. PostgreSQL migration can use native enums.
    classified_at maps from ClassifiedTrade.timestamp (when trade occurred,
    not when classify() ran).
    """

    __tablename__ = "classified_trades"
    __table_args__ = (
        Index("ix_classified_trades_symbol_at", "symbol", "classified_at"),
        Index("ix_classified_trades_con_id_at", "con_id", "classified_at"),
        Index("ix_classified_trades_symbol_aggressor_at", "symbol", "aggressor", "classified_at"),
        CheckConstraint("strike > 0", name="ck_classified_trades_strike_positive"),
        CheckConstraint("right IN ('C', 'P')", name="ck_classified_trades_right_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)

    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    trade_type: Mapped[str] = mapped_column(String, nullable=False)
    aggressor: Mapped[str] = mapped_column(String, nullable=False)
    spread_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    window_ticks: Mapped[int] = mapped_column(Integer, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UnusualSignalRecord(Base):
    """One row per UnusualSignal emitted by UnusualDetector.

    Persisted by the orchestration layer via insert_unusual_signal().
    trade_type, aggressor, top_reason stored as plain strings (enum values)
    for SQLite compatibility.
    reasons stored as a JSON array string, e.g. '["premium_size","oi_ratio"]'.

    classified_at = trade.timestamp (when the originating trade occurred).
    flagged_at = when UnusualDetector.detect() was called.
    No FK to classified_trades — consistent with project pattern (avoids
    persistence ordering constraint; join on (con_id, classified_at) instead).
    """

    __tablename__ = "unusual_signals"
    __table_args__ = (
        Index("ix_unusual_signals_symbol_flagged_at", "symbol", "flagged_at"),
        Index("ix_unusual_signals_con_id_flagged_at", "con_id", "flagged_at"),
        CheckConstraint("strike > 0", name="ck_unusual_signals_strike_positive"),
        CheckConstraint("right IN ('C', 'P')", name="ck_unusual_signals_right_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)

    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    trade_type: Mapped[str] = mapped_column(String, nullable=False)     # TradeType.value
    aggressor: Mapped[str] = mapped_column(String, nullable=False)       # Aggressor.value
    premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)

    top_reason: Mapped[str] = mapped_column(String, nullable=False)     # UnusualReason.value
    reasons: Mapped[str] = mapped_column(String, nullable=False)        # JSON array
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
