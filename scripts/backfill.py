from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from config.settings import settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the backfill script.

    Args:
        argv: Argument list. If None, reads from sys.argv.

    Returns:
        Parsed namespace with a 'symbols' list.
    """
    parser = argparse.ArgumentParser(
        description="Snapshot option chains for the watchlist and persist to DB.",
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        metavar="SYMBOL",
        help="Ticker symbols to backfill. Reads from watchlist if omitted.",
    )
    return parser.parse_args(argv)


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    """Return symbols from CLI args or fall back to the watchlist file.

    Args:
        args: Parsed CLI namespace.

    Returns:
        List of uppercase ticker symbols.
    """
    if args.symbols:
        return [s.upper() for s in args.symbols]
    from src.utils.watchlist import WatchlistManager
    return WatchlistManager(settings.watchlist_path).active_symbols()


async def backfill(symbols: list[str]) -> None:
    """Fetch and persist option chain snapshots for all given symbols.

    Connects to IBKR, fetches the current option chain for each symbol
    sequentially (respecting IBKR rate limits), and saves snapshots to DB.

    Args:
        symbols: Underlying ticker symbols to snapshot (e.g. ["SPY", "QQQ"]).
    """
    from src.connection.ibkr_client import IBKRClient
    from src.data.chain_fetcher import ChainFetcher
    from src.storage.db import get_session, init_db
    from src.storage.queries import insert_chain_snapshot

    await init_db()

    async with IBKRClient() as client:
        await client.verify_connection()
        fetcher = ChainFetcher(client)

        saved = 0
        failed = 0

        for symbol in symbols:
            try:
                snapshot = await fetcher.fetch_chain(symbol)
                async with get_session() as session:
                    await insert_chain_snapshot(session, snapshot)
                saved += 1
                logger.info(
                    "Saved {} contracts for {}",
                    len(snapshot.contracts), symbol,
                )
            except Exception:
                failed += 1
                logger.exception("Failed to backfill {}", symbol)

    logger.info("Backfill complete: {} succeeded, {} failed", saved, failed)


if __name__ == "__main__":
    args = parse_args()
    symbols = _resolve_symbols(args)
    if not symbols:
        logger.warning(
            "No symbols to backfill. Pass symbols as args or populate the watchlist."
        )
    else:
        asyncio.run(backfill(symbols))
