from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
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
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chain_snapshots.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
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
    """

    __tablename__ = "option_ticks"
    __table_args__ = (
        Index("ix_option_ticks_con_id_received_at", "con_id", "received_at"),
        Index("ix_option_ticks_symbol_received_at", "symbol", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    con_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expiry: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    right: Mapped[str] = mapped_column(String(1), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

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
