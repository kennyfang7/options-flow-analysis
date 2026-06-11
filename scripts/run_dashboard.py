# scripts/run_dashboard.py
from __future__ import annotations

import argparse
import asyncio
import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.dashboard.shared_state import SharedState

from config.settings import settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the dashboard script.

    Args:
        argv: Argument list. If None, reads from sys.argv.

    Returns:
        Parsed namespace with 'symbols', 'port', and 'debug'.
    """
    parser = argparse.ArgumentParser(
        description="Launch the Options Flow Analysis dashboard.",
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        metavar="SYMBOL",
        help="Ticker symbols to watch. Reads from watchlist if omitted.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Port for the Dash UI (default: 8050).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Dash debug mode.",
    )
    return parser.parse_args(argv)


def start_pipeline_thread(
    state: "SharedState",
    symbols: list[str],
) -> threading.Thread:
    """Start the asyncio analysis pipeline in a background daemon thread.

    The pipeline feeds alerts and sentiment into SharedState for Dash
    callbacks to read. The thread runs until the process exits.

    Args:
        state: SharedState instance shared with the Dash app.
        symbols: Ticker symbols to watch.

    Returns:
        The started daemon Thread.
    """
    def _run() -> None:
        try:
            asyncio.run(_pipeline(state, symbols))
        except Exception as exc:
            logger.exception("Dashboard pipeline thread crashed")
            state.update_pipeline_status(f"Crashed: {exc}")

    thread = threading.Thread(target=_run, daemon=True, name="pipeline")
    thread.start()
    return thread


async def _pipeline(
    state: "SharedState",
    symbols: list[str],
) -> None:
    """Async pipeline that feeds SharedState from the IBKR tick stream.

    Mirrors run_scanner.run_pipeline but also pushes alerts and sentiment
    snapshots into SharedState for the Dash UI.

    Args:
        state: Shared state bridge between the asyncio pipeline and Dash.
        symbols: Underlying ticker symbols to watch.
    """
    from src.alerts.notifier import Notifier
    from src.alerts.rules import AlertRules
    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.analysis.sentiment import SentimentAggregator
    from src.analysis.smart_money import SmartMoneyDetector
    from src.analysis.unusual_detector import UnusualDetector
    from src.connection.ibkr_client import IBKRClient
    from src.data.chain_fetcher import ChainFetcher
    from src.data.scanner import MarketScanner
    from src.data.tick_stream import MAX_MKT_DATA_LINES, TickStream
    from src.storage.db import get_session, init_db
    from src.storage.queries import (
        insert_chain_snapshot,
        insert_classified_trade,
        insert_unusual_signal,
        load_chain_snapshot,
    )

    state.update_pipeline_status("Connecting to IB Gateway...")
    await init_db()

    # All components require settings — pass singleton explicitly
    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)
    unusual = UnusualDetector(settings)
    sentiment = SentimentAggregator(settings)
    smart_money = SmartMoneyDetector(settings)
    rules = AlertRules(settings)
    notifier = Notifier(settings)
    from src.utils.earnings import EarningsCalendar
    earnings_cal = EarningsCalendar()

    async with IBKRClient() as client:
        await client.verify_connection()
        state.update_pipeline_status("Connected — fetching option chains...")

        if not symbols:
            scanner = MarketScanner(client)
            results = await scanner.scan_unusual_volume()
            symbols = list(dict.fromkeys(r.symbol for r in results))
            logger.info("Scanner discovered {} symbols: {}", len(symbols), symbols)

        await earnings_cal.prefetch(symbols)
        logger.info("Earnings calendar pre-fetched for {} symbols.", len(symbols))

        fetcher = ChainFetcher(client)
        purge_interval = 3600.0

        # Enter TickStream before symbol loop; subscribe per-symbol
        # with underlying_price and enforce the 95-contract cap.
        async with TickStream(client) as stream:
            for symbol in symbols:
                try:
                    # Try DB cache first — avoid redundant IBKR chain fetches on restart
                    async with get_session() as session:
                        snapshot = await load_chain_snapshot(session, symbol)

                    if snapshot is not None:
                        logger.info(
                            "Using cached chain snapshot for {} ({} contracts)",
                            symbol, len(snapshot.contracts),
                        )
                    else:
                        snapshot = await fetcher.fetch_chain(symbol)
                        async with get_session() as session:
                            await insert_chain_snapshot(session, snapshot)

                    qualified = [c for c in snapshot.contracts if c.con_id]

                    remaining = MAX_MKT_DATA_LINES - stream.subscribed_count
                    if len(qualified) > remaining:
                        logger.warning(
                            "Symbol {}: truncating {} contracts to {} (cap remaining={})",
                            symbol, len(qualified), remaining, remaining,
                        )
                        qualified = qualified[:remaining]

                    if qualified:
                        await stream.subscribe(
                            qualified, underlying_price=snapshot.underlying_price
                        )

                    for c in snapshot.contracts:
                        if c.con_id is not None and c.open_interest is not None:
                            unusual._oi_cache[c.con_id] = c.open_interest
                except Exception:
                    logger.exception("Failed to fetch chain for {} in dashboard pipeline", symbol)

                if stream.subscribed_count >= MAX_MKT_DATA_LINES:
                    logger.warning(
                        "Market data cap ({}) reached. Skipping remaining symbols.",
                        MAX_MKT_DATA_LINES,
                    )
                    break

            if stream.subscribed_count == 0:
                logger.error(
                    "No contracts subscribed in dashboard pipeline — "
                    "check watchlist and IBKR connection."
                )
                state.update_pipeline_status(
                    "Failed: 0 contracts subscribed — check IBKR connection and watchlist"
                )
                return

            state.update_pipeline_status(
                f"Running — {stream.subscribed_count} contracts subscribed"
            )
            logger.success("Dashboard pipeline running ({} contracts).", stream.subscribed_count)
            last_purge = asyncio.get_running_loop().time()
            ticks_seen = 0
            trades_classified = 0

            while True:
                try:
                    tick = await asyncio.wait_for(stream.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    now = asyncio.get_running_loop().time()
                    if now - last_purge >= purge_interval:
                        classifier.purge_stale()
                        unusual.purge_stale()
                        sentiment.purge_stale()
                        last_purge = now
                    continue

                ticks_seen += 1
                trade = classifier.classify(tick)
                if trade is None:
                    if ticks_seen % 100 == 0:
                        state.update_pipeline_status(
                            f"Running — {stream.subscribed_count} contracts | "
                            f"{ticks_seen} ticks, {trades_classified} trades classified"
                        )
                    continue

                trades_classified += 1
                state.update_pipeline_status(
                    f"Running — {stream.subscribed_count} contracts | "
                    f"{ticks_seen} ticks, {trades_classified} trades classified"
                )

                enriched = greeks.enrich(trade)
                dte = await earnings_cal.get_days_to_earnings(enriched.symbol)
                if dte is not None:
                    enriched = enriched.model_copy(update={"days_to_earnings": dte})
                sentiment.update(enriched)

                snap = sentiment.snapshot(enriched.symbol)
                if snap is not None:
                    state.update_sentiment(snap)

                try:
                    async with get_session() as session:
                        await insert_classified_trade(session, enriched)
                except Exception:
                    logger.exception("Failed to persist classified trade in dashboard pipeline")

                signal_result = await unusual.detect(enriched)
                if signal_result is not None:
                    try:
                        async with get_session() as session:
                            await insert_unusual_signal(session, signal_result)
                    except Exception:
                        logger.exception("Failed to persist unusual signal in dashboard pipeline")
                    alert = rules.evaluate_unusual(signal_result)
                    state.push_alert(alert)
                    await notifier.send(alert)

                sm_signal = smart_money.score(enriched)
                if sm_signal is not None:
                    alert = rules.evaluate_smart_money(sm_signal)
                    state.push_alert(alert)
                    await notifier.send(alert)


if __name__ == "__main__":
    from src.dashboard.app import create_app
    from src.dashboard.shared_state import SharedState
    from src.storage.db import init_db
    from src.utils.watchlist import WatchlistManager

    args = parse_args()
    symbols = (
        [s.upper() for s in args.symbols]
        if args.symbols
        else WatchlistManager(settings.watchlist_path).active_symbols()
    )

    # Ensure DB tables exist before Dash callbacks can fire
    asyncio.run(init_db())

    state = SharedState()
    start_pipeline_thread(state, symbols)

    app = create_app(state, symbols=symbols or ["SPY"])
    logger.info("Starting dashboard on port {}", args.port)
    app.run(debug=args.debug, port=args.port)
