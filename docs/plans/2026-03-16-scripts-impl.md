# Scripts Entry Points Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the three entry point scripts (`run_scanner.py`, `run_dashboard.py`, `backfill.py`) that wire all previously-built modules into runnable programs.

**Architecture:** Three standalone CLI scripts. `run_scanner.py` runs the full async tick pipeline (scan → chain fetch → TickStream → classify → enrich → detect → alert → DB). `backfill.py` snapshots chains for a watchlist and saves to DB. `run_dashboard.py` runs the pipeline in a background daemon thread while serving Dash on the main thread. Shared helper `load_watchlist()` lives in `run_scanner.py` and is imported by `backfill.py`.

**Architecture notes from review:**
- All 7 pipeline components (`FlowClassifier`, `GreeksEngine`, `SentimentAggregator`, `UnusualDetector`, `SmartMoneyDetector`, `AlertRules`, `Notifier`) require `settings: Settings` — pass the singleton explicitly.
- `TickStream.subscribe()` raises `TickStreamError` if subscriptions would exceed `MAX_MKT_DATA_LINES=95`. Enter `TickStream` before the symbol loop and subscribe per-symbol with cap enforcement.
- Pass `underlying_price=snapshot.underlying_price` to `stream.subscribe()` per symbol so premium calculations work downstream.
- Both pipelines need `purge_stale()` on `FlowClassifier`, `UnusualDetector`, and `SentimentAggregator` every hour.

**Tech Stack:** Python asyncio, argparse, threading, all src/ modules, pytest (no new dependencies).

---

### Task 1: Test file scaffold + `load_watchlist`

**Files:**
- Create: `tests/test_scripts.py`
- Modify: `scripts/run_scanner.py`

**Step 1: Write the failing test**

```python
# tests/test_scripts.py
from __future__ import annotations

from pathlib import Path
import pytest


def test_load_watchlist_reads_symbols(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\nQQQ\nAAPL\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ", "AAPL"]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_scripts.py::test_load_watchlist_reads_symbols -v`
Expected: FAIL with ImportError (run_scanner.py is empty)

**Step 3: Implement `load_watchlist` in `scripts/run_scanner.py`**

```python
# scripts/run_scanner.py
from __future__ import annotations

from pathlib import Path

from loguru import logger

from config.settings import settings


def load_watchlist(path: str) -> list[str]:
    """Load ticker symbols from a newline-separated watchlist file.

    Args:
        path: Path to the watchlist file.

    Returns:
        List of uppercase ticker symbols; empty lines and # comments stripped.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Watchlist not found at {}, using empty list", path)
        return []
    symbols = [
        line.strip().upper()
        for line in p.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    logger.info("Loaded {} symbols from {}", len(symbols), path)
    return symbols
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_scripts.py::test_load_watchlist_reads_symbols -v`
Expected: PASS

**Step 5: Add the remaining watchlist tests**

Append to `tests/test_scripts.py`:

```python
def test_load_watchlist_strips_comments(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n# comment\nQQQ\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ"]


def test_load_watchlist_skips_blank_lines(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n\nQQQ\n\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ"]


def test_load_watchlist_missing_file_returns_empty(tmp_path: Path) -> None:
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(tmp_path / "nonexistent.txt")) == []


def test_load_watchlist_uppercases_symbols(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("spy\nqqq\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ"]
```

**Step 6: Run all tests so far**

Run: `pytest tests/test_scripts.py -v`
Expected: 5 PASS

**Step 7: Commit**

```bash
git add tests/test_scripts.py scripts/run_scanner.py
git commit -m "feat: scaffold test_scripts.py and add load_watchlist helper"
```

---

### Task 2: `scripts/backfill.py`

**Files:**
- Modify: `scripts/backfill.py`
- Modify: `tests/test_scripts.py`

**Step 1: Write the failing tests**

Append to `tests/test_scripts.py`:

```python
def test_backfill_parse_args_no_symbols() -> None:
    from scripts.backfill import parse_args
    args = parse_args([])
    assert args.symbols == []


def test_backfill_parse_args_with_symbols() -> None:
    from scripts.backfill import parse_args
    args = parse_args(["SPY", "QQQ"])
    assert args.symbols == ["SPY", "QQQ"]


def test_backfill_resolve_symbols_uses_cli_args() -> None:
    from scripts.backfill import parse_args, _resolve_symbols
    args = parse_args(["spy", "qqq"])
    assert _resolve_symbols(args) == ["SPY", "QQQ"]


def test_backfill_resolve_symbols_falls_back_to_watchlist(
    tmp_path: Path, monkeypatch
) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\nQQQ\n")
    from config.settings import settings
    monkeypatch.setattr(settings, "watchlist_path", str(wl))
    from scripts.backfill import parse_args, _resolve_symbols
    args = parse_args([])
    assert _resolve_symbols(args) == ["SPY", "QQQ"]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scripts.py -k backfill -v`
Expected: FAIL with ImportError

**Step 3: Implement `scripts/backfill.py`**

```python
# scripts/backfill.py
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
    from scripts.run_scanner import load_watchlist
    return load_watchlist(settings.watchlist_path)


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
```

**Step 4: Run all script tests**

Run: `pytest tests/test_scripts.py -v`
Expected: 9 PASS

**Step 5: Commit**

```bash
git add scripts/backfill.py tests/test_scripts.py
git commit -m "feat: implement backfill.py with chain snapshot persistence"
```

---

### Task 3: `scripts/run_scanner.py` (full implementation)

**Files:**
- Modify: `scripts/run_scanner.py`
- Modify: `tests/test_scripts.py`

**Step 1: Write failing tests for `parse_args`**

Append to `tests/test_scripts.py`:

```python
def test_scanner_parse_args_no_symbols() -> None:
    from scripts.run_scanner import parse_args
    args = parse_args([])
    assert args.symbols == []


def test_scanner_parse_args_with_symbols() -> None:
    from scripts.run_scanner import parse_args
    args = parse_args(["SPY", "AAPL"])
    assert args.symbols == ["SPY", "AAPL"]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_scripts.py::test_scanner_parse_args_no_symbols -v`
Expected: FAIL (`parse_args` not defined yet)

**Step 3: Complete `scripts/run_scanner.py`**

Replace the entire file with:

```python
# scripts/run_scanner.py
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from loguru import logger

from config.settings import settings


def load_watchlist(path: str) -> list[str]:
    """Load ticker symbols from a newline-separated watchlist file.

    Args:
        path: Path to the watchlist file.

    Returns:
        List of uppercase ticker symbols; empty lines and # comments stripped.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Watchlist not found at {}, using empty list", path)
        return []
    symbols = [
        line.strip().upper()
        for line in p.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    logger.info("Loaded {} symbols from {}", len(symbols), path)
    return symbols


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
    from src.data.chain_fetcher import ChainFetcher
    from src.data.scanner import MarketScanner
    from src.data.tick_stream import MAX_MKT_DATA_LINES, TickStream
    from src.storage.db import get_session, init_db
    from src.storage.queries import (
        insert_chain_snapshot,
        insert_classified_trade,
        insert_unusual_signal,
    )

    await init_db()

    # FIX 1: All components require settings — pass singleton explicitly
    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)
    unusual = UnusualDetector(settings)
    sentiment = SentimentAggregator(settings)
    smart_money = SmartMoneyDetector(settings)
    rules = AlertRules(settings)
    notifier = Notifier(settings)

    async with IBKRClient() as client:
        await client.verify_connection()

        if not symbols:
            scanner = MarketScanner(client)
            results = await scanner.scan_unusual_volume()
            # deduplicate while preserving rank order
            symbols = list(dict.fromkeys(r.symbol for r in results))
            logger.info("Scanner discovered {} symbols: {}", len(symbols), symbols)

        fetcher = ChainFetcher(client)
        purge_interval = 3600.0  # prune stale windows every hour

        # FIX 2+3: Enter TickStream before symbol loop so we can subscribe
        # per-symbol with underlying_price and enforce the 95-contract cap.
        async with TickStream(client) as stream:
            for symbol in symbols:
                try:
                    snapshot = await fetcher.fetch_chain(symbol)
                    qualified = [c for c in snapshot.contracts if c.con_id]

                    # FIX 2: Enforce MAX_MKT_DATA_LINES cap before subscribing
                    remaining = MAX_MKT_DATA_LINES - stream.subscribed_count
                    if len(qualified) > remaining:
                        logger.warning(
                            "Symbol {}: truncating {} contracts to {} (cap remaining={})",
                            symbol, len(qualified), remaining, remaining,
                        )
                        qualified = qualified[:remaining]

                    if qualified:
                        # FIX 3: Pass underlying_price so premium calculations work
                        await stream.subscribe(
                            qualified, underlying_price=snapshot.underlying_price
                        )

                    # Seed OI cache so UnusualDetector has baseline values
                    for c in snapshot.contracts:
                        if c.con_id is not None and c.open_interest is not None:
                            unusual._oi_cache[c.con_id] = c.open_interest

                    async with get_session() as session:
                        await insert_chain_snapshot(session, snapshot)

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
            last_purge = asyncio.get_event_loop().time()

            while True:
                try:
                    tick = await asyncio.wait_for(stream.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    now = asyncio.get_event_loop().time()
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
    symbols = [s.upper() for s in args.symbols] if args.symbols else load_watchlist(
        settings.watchlist_path
    )
    try:
        asyncio.run(run_pipeline(symbols))
    except KeyboardInterrupt:
        logger.info("Scanner stopped by user.")
```

**Step 4: Run all tests**

Run: `pytest tests/test_scripts.py -v`
Expected: 11 PASS

**Step 5: Commit**

```bash
git add scripts/run_scanner.py tests/test_scripts.py
git commit -m "feat: implement run_scanner.py full pipeline entry point"
```

---

### Task 4: `scripts/run_dashboard.py`

**Files:**
- Modify: `scripts/run_dashboard.py`
- Modify: `tests/test_scripts.py`

**Step 1: Write failing tests**

Append to `tests/test_scripts.py`:

```python
def test_dashboard_parse_args_defaults() -> None:
    from scripts.run_dashboard import parse_args
    args = parse_args([])
    assert args.symbols == []
    assert args.port == 8050
    assert args.debug is False


def test_dashboard_parse_args_custom_port() -> None:
    from scripts.run_dashboard import parse_args
    args = parse_args(["--port", "9000"])
    assert args.port == 9000


def test_dashboard_parse_args_with_symbols_and_debug() -> None:
    from scripts.run_dashboard import parse_args
    args = parse_args(["SPY", "QQQ", "--debug"])
    assert args.symbols == ["SPY", "QQQ"]
    assert args.debug is True


def test_start_pipeline_thread_returns_daemon_thread() -> None:
    from unittest.mock import patch
    from src.dashboard.shared_state import SharedState
    from scripts.run_dashboard import start_pipeline_thread

    state = SharedState()

    async def _quick(s, syms):
        return  # exits immediately so the thread ends cleanly

    with patch("scripts.run_dashboard._pipeline", _quick):
        thread = start_pipeline_thread(state, ["SPY"])

    assert thread.daemon is True
    thread.join(timeout=2.0)  # wait for the thread to finish cleanly
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scripts.py -k dashboard -v`
Expected: FAIL with ImportError

**Step 3: Implement `scripts/run_dashboard.py`**

```python
# scripts/run_dashboard.py
from __future__ import annotations

import argparse
import asyncio
import threading

from loguru import logger

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
    state: "SharedState",  # type: ignore[name-defined]
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
        asyncio.run(_pipeline(state, symbols))

    thread = threading.Thread(target=_run, daemon=True, name="pipeline")
    thread.start()
    return thread


async def _pipeline(
    state: "SharedState",  # type: ignore[name-defined]
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
    )

    await init_db()

    # FIX 1: All components require settings — pass singleton explicitly
    classifier = FlowClassifier(settings)
    greeks = GreeksEngine(settings)
    unusual = UnusualDetector(settings)
    sentiment = SentimentAggregator(settings)
    smart_money = SmartMoneyDetector(settings)
    rules = AlertRules(settings)
    notifier = Notifier(settings)

    async with IBKRClient() as client:
        await client.verify_connection()

        if not symbols:
            scanner = MarketScanner(client)
            results = await scanner.scan_unusual_volume()
            symbols = list(dict.fromkeys(r.symbol for r in results))
            logger.info("Scanner discovered {} symbols: {}", len(symbols), symbols)

        fetcher = ChainFetcher(client)
        purge_interval = 3600.0

        # FIX 2+3: Enter TickStream before symbol loop; subscribe per-symbol
        # with underlying_price and enforce the 95-contract cap.
        async with TickStream(client) as stream:
            for symbol in symbols:
                try:
                    snapshot = await fetcher.fetch_chain(symbol)
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

                    async with get_session() as session:
                        await insert_chain_snapshot(session, snapshot)
                except Exception:
                    logger.exception("Failed to fetch chain for {} in dashboard pipeline", symbol)

                if stream.subscribed_count >= MAX_MKT_DATA_LINES:
                    logger.warning(
                        "Market data cap ({}) reached. Skipping remaining symbols.",
                        MAX_MKT_DATA_LINES,
                    )
                    break

            logger.success("Dashboard pipeline running ({} contracts).", stream.subscribed_count)
            last_purge = asyncio.get_event_loop().time()

            while True:
                try:
                    tick = await asyncio.wait_for(stream.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # FIX 4: Purge stale windows in dashboard pipeline too
                    now = asyncio.get_event_loop().time()
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
    from scripts.run_scanner import load_watchlist
    from src.dashboard.app import create_app
    from src.dashboard.shared_state import SharedState

    args = parse_args()
    symbols = (
        [s.upper() for s in args.symbols]
        if args.symbols
        else load_watchlist(settings.watchlist_path)
    )

    state = SharedState()
    start_pipeline_thread(state, symbols)

    app = create_app(state, symbols=symbols or ["SPY"])
    logger.info("Starting dashboard on port {}", args.port)
    app.run_server(debug=args.debug, port=args.port)
```

**Step 4: Run all script tests**

Run: `pytest tests/test_scripts.py -v`
Expected: 15 PASS

**Step 5: Run full test suite — verify no regressions**

Run: `pytest -m "not integration" -v`
Expected: 344 PASS (329 existing + 15 new)

**Step 6: Commit**

```bash
git add scripts/run_dashboard.py tests/test_scripts.py
git commit -m "feat: implement run_dashboard.py with background pipeline thread and Dash entry point"
```

---

## Usage Reference

```bash
# Backfill chains for the watchlist
python scripts/backfill.py

# Backfill specific symbols
python scripts/backfill.py SPY QQQ AAPL

# Run live scanner (watchlist symbols)
python scripts/run_scanner.py

# Run live scanner (specific symbols)
python scripts/run_scanner.py SPY QQQ

# Launch dashboard (port 8050)
python scripts/run_dashboard.py SPY QQQ

# Launch dashboard on custom port with debug mode
python scripts/run_dashboard.py SPY --port 9000 --debug
```
