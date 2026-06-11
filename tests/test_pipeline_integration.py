"""Step 15 — Pipeline integration tests against live IBKR TWS/Gateway.

All tests in this file require TWS or IB Gateway running locally with API
access enabled. They are excluded from the default test run and must be
invoked explicitly:

    pytest -m integration

Tests subscribe to a small SPY chain slice (1 expiry, ±2% strikes) to
minimise data usage and stay comfortably within IBKR rate limits.

Tests that depend on tick arrival (market data flowing) call ``pytest.skip``
when no data arrives within the timeout — this happens outside market hours
and is expected.
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from src.storage.models import Base


# ---------------------------------------------------------------------------
# Module-level fixtures — one TWS connection + one chain fetch per run
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def live_client():
    """Connected IBKRClient shared across all tests in this module.

    Yields:
        An active IBKRClient instance verified against TWS.
    """
    from src.connection.ibkr_client import IBKRClient

    async with IBKRClient() as client:
        await client.verify_connection()
        yield client


@pytest_asyncio.fixture(scope="module")
async def live_spy_snapshot(live_client):
    """Small SPY option chain snapshot (1 expiry, ±2% strikes) fetched once.

    Args:
        live_client: Shared live IBKRClient fixture.

    Yields:
        OptionChainSnapshot for SPY.
    """
    from src.data.chain_fetcher import ChainFetcher

    fetcher = ChainFetcher(live_client)
    snapshot = await fetcher.fetch_chain("SPY", max_expiries=1, strike_range_pct=0.02)
    return snapshot


@pytest_asyncio.fixture
async def integration_db_session() -> AsyncSession:
    """Isolated in-memory SQLite session for storage integration tests.

    Creates all tables fresh, yields the session, then disposes the engine.

    Yields:
        An active AsyncSession backed by an in-memory SQLite database.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. TickStream — basic data receipt
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tick_stream_receives_ticks(live_client, live_spy_snapshot) -> None:
    """Subscribe to a small SPY chain slice and receive at least one tick.

    Subscribes up to 10 qualified contracts and waits 30s for a tick.
    Skips (not fails) if no tick arrives — market may be closed.
    """
    from src.data.tick_stream import TickStream

    contracts = [c for c in live_spy_snapshot.contracts if c.con_id][:10]
    assert contracts, "No qualified SPY contracts in snapshot"

    async with TickStream(live_client) as stream:
        await stream.subscribe(contracts, underlying_price=live_spy_snapshot.underlying_price)
        assert stream.subscribed_count == len(contracts)

        try:
            tick = await asyncio.wait_for(stream.queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            pytest.skip("No ticks received in 30s — market may be closed")

    assert tick.symbol == "SPY"
    assert tick.con_id in {c.con_id for c in contracts}
    assert tick.right in ("C", "P")
    assert tick.strike > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tick_stream_unsubscribe_clears_count(live_client, live_spy_snapshot) -> None:
    """Unsubscribing releases all market data lines (subscribed_count returns to 0)."""
    from src.data.tick_stream import TickStream

    contracts = [c for c in live_spy_snapshot.contracts if c.con_id][:5]
    assert contracts, "No qualified SPY contracts in snapshot"

    async with TickStream(live_client) as stream:
        await stream.subscribe(contracts, underlying_price=live_spy_snapshot.underlying_price)
        assert stream.subscribed_count == len(contracts)
        await stream.unsubscribe()
        assert stream.subscribed_count == 0


# ---------------------------------------------------------------------------
# 2. FlowClassifier + GreeksEngine on live ticks
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_classifier_and_greeks_on_live_ticks(live_client, live_spy_snapshot) -> None:
    """FlowClassifier classifies a tick; GreeksEngine enriches it.

    Waits up to 45s for a classifiable trade. Validates structure of the
    resulting EnrichedTrade — not the specific values, which vary with
    market conditions.
    """
    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickStream

    contracts = [c for c in live_spy_snapshot.contracts if c.con_id][:10]
    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)

    enriched = None
    async with TickStream(live_client) as stream:
        await stream.subscribe(contracts, underlying_price=live_spy_snapshot.underlying_price)

        deadline = asyncio.get_running_loop().time() + 45.0
        while asyncio.get_running_loop().time() < deadline:
            try:
                tick = await asyncio.wait_for(stream.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            trade = classifier.classify(tick)
            if trade is not None:
                enriched = greeks.enrich(trade)
                break

    if enriched is None:
        pytest.skip("No classifiable trade in 45s — market may be closed")

    # ClassifiedTrade fields
    assert enriched.symbol == "SPY"
    assert enriched.right in ("C", "P")
    assert enriched.strike > 0
    assert enriched.aggressor.value in ("BUY", "SELL", "NEUTRAL")
    assert enriched.premium is None or enriched.premium >= 0

    # EnrichedTrade additions
    assert enriched.days_to_expiry >= 0
    assert enriched.moneyness.value in ("ITM", "ATM", "OTM", "UNKNOWN")
    assert enriched.iv_source in ("ibkr", "black_scholes", "unavailable")


# ---------------------------------------------------------------------------
# 3. Full pipeline: tick → classify → enrich → sentiment → detect → alert
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_pipeline_end_to_end(live_client, live_spy_snapshot) -> None:
    """Run the complete analysis pipeline for up to 60s with no exceptions.

    Pipeline: FlowClassifier → GreeksEngine → SentimentAggregator →
    UnusualDetector → SmartMoneyDetector → AlertRules.

    Validates:
    - Pipeline runs without raising
    - SentimentSnapshot is populated after trades are processed
    - Any emitted alerts have valid structure
    """
    from src.alerts.rules import AlertRules
    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.analysis.sentiment import SentimentAggregator
    from src.analysis.smart_money import SmartMoneyDetector
    from src.analysis.unusual_detector import UnusualDetector
    from src.data.tick_stream import TickStream

    contracts = [c for c in live_spy_snapshot.contracts if c.con_id][:10]

    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)
    unusual = UnusualDetector(settings)
    sentiment = SentimentAggregator(settings)
    smart_money = SmartMoneyDetector(settings)
    rules = AlertRules(settings)

    # Seed OI cache from the chain snapshot so unusual detection has baseline
    for c in live_spy_snapshot.contracts:
        if c.con_id is not None and c.open_interest is not None:
            unusual._oi_cache[c.con_id] = c.open_interest

    trades_processed = 0
    alerts_emitted = []

    async with TickStream(live_client) as stream:
        await stream.subscribe(contracts, underlying_price=live_spy_snapshot.underlying_price)

        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline and trades_processed < 10:
            try:
                tick = await asyncio.wait_for(stream.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            trade = classifier.classify(tick)
            if trade is None:
                continue

            trades_processed += 1
            enriched = greeks.enrich(trade)
            sentiment.update(enriched)

            signal = await unusual.detect(enriched)
            if signal is not None:
                alerts_emitted.append(rules.evaluate_unusual(signal))

            sm_signal = smart_money.score(enriched)
            if sm_signal is not None:
                alerts_emitted.append(rules.evaluate_smart_money(sm_signal))

    if trades_processed == 0:
        pytest.skip("No classifiable trades in 60s — market may be closed")

    snap = sentiment.snapshot("SPY")
    assert snap is not None, "SentimentAggregator should have a snapshot after processing trades"
    assert snap.symbol == "SPY"
    assert snap.trade_count == trades_processed

    for alert in alerts_emitted:
        assert alert.symbol == "SPY"
        assert alert.level.value in ("LOW", "MEDIUM", "HIGH")
        assert alert.title
        assert alert.body
        assert alert.emitted_at is not None


# ---------------------------------------------------------------------------
# 4. Storage round-trip with live classified trades
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_storage_round_trip(
    live_client, live_spy_snapshot, integration_db_session
) -> None:
    """Classify live ticks, persist to in-memory DB, and verify returned IDs.

    Uses an isolated in-memory SQLite session so the test never touches the
    real database. Verifies that insert_classified_trade and
    insert_unusual_signal return valid positive primary keys.
    """
    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.analysis.unusual_detector import UnusualDetector
    from src.data.tick_stream import TickStream
    from src.storage.queries import insert_classified_trade, insert_unusual_signal

    contracts = [c for c in live_spy_snapshot.contracts if c.con_id][:10]
    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)
    unusual = UnusualDetector(settings)

    for c in live_spy_snapshot.contracts:
        if c.con_id is not None and c.open_interest is not None:
            unusual._oi_cache[c.con_id] = c.open_interest

    trade_ids: list[int] = []
    signal_ids: list[int] = []

    async with TickStream(live_client) as stream:
        await stream.subscribe(contracts, underlying_price=live_spy_snapshot.underlying_price)

        deadline = asyncio.get_running_loop().time() + 45.0
        while asyncio.get_running_loop().time() < deadline and len(trade_ids) < 3:
            try:
                tick = await asyncio.wait_for(stream.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            trade = classifier.classify(tick)
            if trade is None:
                continue

            enriched = greeks.enrich(trade)
            trade_id = await insert_classified_trade(integration_db_session, enriched)
            trade_ids.append(trade_id)

            signal = await unusual.detect(enriched)
            if signal is not None:
                signal_id = await insert_unusual_signal(integration_db_session, signal)
                signal_ids.append(signal_id)

    if not trade_ids:
        pytest.skip("No classifiable trades in 45s — market may be closed")

    assert all(isinstance(i, int) and i > 0 for i in trade_ids), (
        f"Expected positive integer PKs from insert_classified_trade, got: {trade_ids}"
    )
    assert all(isinstance(i, int) and i > 0 for i in signal_ids), (
        f"Expected positive integer PKs from insert_unusual_signal, got: {signal_ids}"
    )


# ---------------------------------------------------------------------------
# 5. Scanner → chain → subscribe path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scanner_discovers_and_chain_fetches(live_client) -> None:
    """MarketScanner discovers symbols; ChainFetcher fetches the top result's chain.

    Validates the scanner → chain pipeline used by run_scanner.py when no
    watchlist symbols are provided.
    """
    from src.data.chain_fetcher import ChainFetcher
    from src.data.scanner import MarketScanner

    scanner = MarketScanner(live_client)
    results = await scanner.scan_unusual_volume(n_rows=3)
    assert results, "Scanner returned no results"

    top_symbol = results[0].symbol
    fetcher = ChainFetcher(live_client)
    snapshot = await fetcher.fetch_chain(top_symbol, max_expiries=1, strike_range_pct=0.02)

    assert snapshot.underlying == top_symbol
    assert snapshot.underlying_price > 0
    assert len(snapshot.contracts) > 0
    assert all(c.symbol == top_symbol for c in snapshot.contracts)


# ---------------------------------------------------------------------------
# 6. Dashboard pipeline thread feeds SharedState
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_pipeline_thread_updates_status() -> None:
    """Dashboard pipeline thread sets pipeline_status within 5s of starting.

    The thread calls state.update_pipeline_status("Connecting to IB Gateway…")
    before performing any async work, so this succeeds even when TWS is slow
    to respond.
    """
    from src.dashboard.shared_state import SharedState
    from scripts.run_dashboard import start_pipeline_thread

    state = SharedState()
    assert state.pipeline_status == ""  # initial state is empty

    thread = start_pipeline_thread(state, ["SPY"])

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if state.pipeline_status:
            break
        await asyncio.sleep(0.1)

    thread.join(timeout=0)  # non-blocking; daemon thread dies with process

    assert state.pipeline_status, (
        "Pipeline thread did not set pipeline_status within 5s"
    )
