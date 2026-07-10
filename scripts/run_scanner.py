# scripts/run_scanner.py
from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from config.settings import settings
from src.utils.watchlist import WatchlistManager


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the scanner script.

    Args:
        argv: Argument list. If None, reads from sys.argv.

    Returns:
        Parsed namespace with a 'symbols' list.
    """
    parser = argparse.ArgumentParser(
        description="Run the real-time options flow analysis pipeline.",
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        metavar="SYMBOL",
        help="Ticker symbols to watch. Reads from watchlist if omitted.",
    )
    return parser.parse_args(argv)


async def run_pipeline(symbols: list[str]) -> None:
    """Connect to IBKR and stream option ticks through the full analysis pipeline.

    Connects to IBKR TWS/Gateway, fetches option chains for each symbol,
    subscribes to real-time tick stream, and routes each tick through:
    FlowClassifier → GreeksEngine → SentimentAggregator, UnusualDetector,
    SmartMoneyDetector → AlertRules → Notifier. Classified trades and unusual
    signals are persisted to the database. Runs until interrupted.

    If symbols is empty the IBKR market scanner discovers hot symbols first.

    Args:
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
    from src.connection.rate_limiter import RateLimiter
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

    # One shared limiter so the 48 msg/sec budget is enforced across all IBKR components.
    limiter = RateLimiter()

    async with IBKRClient() as client:
        await client.verify_connection()

        if not symbols:
            scanner = MarketScanner(client, limiter)
            results = await scanner.scan_unusual_volume()
            # deduplicate while preserving rank order
            symbols = list(dict.fromkeys(r.symbol for r in results))
            logger.info("Scanner discovered {} symbols: {}", len(symbols), symbols)

        await earnings_cal.prefetch(symbols)
        logger.info("Earnings calendar pre-fetched for {} symbols.", len(symbols))

        fetcher = ChainFetcher(client, limiter)
        purge_interval = 3600.0  # prune stale windows every hour

        # Enter TickStream before symbol loop so we can subscribe
        # per-symbol with underlying_price and enforce the 95-contract cap.
        async with TickStream(client, limiter) as stream:
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

                    # Enforce MAX_MKT_DATA_LINES cap before subscribing
                    remaining = MAX_MKT_DATA_LINES - stream.subscribed_count
                    if len(qualified) > remaining:
                        logger.warning(
                            "Symbol {}: truncating {} contracts to {} (cap remaining={})",
                            symbol, len(qualified), remaining, remaining,
                        )
                        qualified = qualified[:remaining]

                    if qualified:
                        # Pass underlying_price so premium calculations work
                        await stream.subscribe(
                            qualified, underlying_price=snapshot.underlying_price
                        )

                    # Seed OI cache so UnusualDetector has baseline values
                    unusual.seed_oi_cache(snapshot.contracts)

                    logger.info(
                        "Subscribed {} contracts for {} ({} total)",
                        len(qualified), symbol, stream.subscribed_count,
                    )
                except Exception:
                    logger.exception("Failed to fetch/subscribe chain for {}", symbol)

                if stream.subscribed_count >= MAX_MKT_DATA_LINES:
                    logger.warning(
                        "Market data cap ({}) reached. Skipping remaining symbols.",
                        MAX_MKT_DATA_LINES,
                    )
                    break

            if stream.subscribed_count == 0:
                logger.error(
                    "No contracts subscribed — check watchlist and IBKR connection."
                )
                return

            logger.success(
                "Pipeline running ({} contracts). Press Ctrl+C to stop.",
                stream.subscribed_count,
            )
            last_purge = asyncio.get_running_loop().time()

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

                trade = classifier.classify(tick)
                if trade is None:
                    continue

                enriched = greeks.enrich(trade)
                dte = await earnings_cal.get_days_to_earnings(enriched.symbol)
                if dte is not None:
                    enriched = enriched.model_copy(update={"days_to_earnings": dte})
                sentiment.update(enriched)

                try:
                    async with get_session() as session:
                        await insert_classified_trade(session, enriched)
                except Exception:
                    logger.exception("Failed to persist classified trade")

                signal_result = await unusual.detect(enriched)
                if signal_result is not None:
                    try:
                        async with get_session() as session:
                            await insert_unusual_signal(session, signal_result)
                    except Exception:
                        logger.exception("Failed to persist unusual signal")
                    alert = rules.evaluate_unusual(signal_result)
                    await notifier.send(alert)

                sm_signal = smart_money.score(enriched)
                if sm_signal is not None:
                    alert = rules.evaluate_smart_money(sm_signal)
                    await notifier.send(alert)


if __name__ == "__main__":
    args = parse_args()
    symbols = (
        [s.upper() for s in args.symbols]
        if args.symbols
        else WatchlistManager(settings.watchlist_path).active_symbols()
    )
    try:
        asyncio.run(run_pipeline(symbols))
    except KeyboardInterrupt:
        logger.info("Scanner stopped by user.")
