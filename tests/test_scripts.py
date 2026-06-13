from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.watchlist import WatchlistManager


def test_scanner_watchlist_reads_symbols(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\nQQQ\nAAPL\n")
    assert WatchlistManager(str(wl)).active_symbols() == ["SPY", "QQQ", "AAPL"]


def test_scanner_watchlist_strips_comments(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n# comment\nQQQ\n")
    assert WatchlistManager(str(wl)).active_symbols() == ["SPY", "QQQ"]


def test_scanner_watchlist_skips_blank_lines(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n\nQQQ\n\n")
    assert WatchlistManager(str(wl)).active_symbols() == ["SPY", "QQQ"]


def test_scanner_watchlist_missing_file_returns_empty(tmp_path: Path) -> None:
    assert WatchlistManager(str(tmp_path / "nonexistent.txt")).active_symbols() == []


def test_scanner_watchlist_uppercases_symbols(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("spy\nqqq\n")
    assert WatchlistManager(str(wl)).active_symbols() == ["SPY", "QQQ"]


def test_scanner_watchlist_strips_indented_comments(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n  # indented comment\nQQQ\n")
    assert WatchlistManager(str(wl)).active_symbols() == ["SPY", "QQQ"]


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
    from unittest.mock import MagicMock
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\nQQQ\n")
    import scripts.backfill as _backfill_mod
    fake_settings = MagicMock()
    fake_settings.watchlist_path = str(wl)
    monkeypatch.setattr(_backfill_mod, "settings", fake_settings)
    from scripts.backfill import parse_args, _resolve_symbols
    args = parse_args([])
    result = _resolve_symbols(args)
    assert set(result) == {"SPY", "QQQ"}


def test_scanner_parse_args_no_symbols() -> None:
    from scripts.run_scanner import parse_args
    args = parse_args([])
    assert args.symbols == []


def test_scanner_parse_args_with_symbols() -> None:
    from scripts.run_scanner import parse_args
    args = parse_args(["SPY", "AAPL"])
    assert args.symbols == ["SPY", "AAPL"]


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


def test_earnings_calendar_importable_from_src_utils() -> None:
    """EarningsCalendar is importable from src.utils."""
    from src.utils import EarningsCalendar
    assert EarningsCalendar is not None


def test_earnings_calendar_instantiates_without_network() -> None:
    """EarningsCalendar() can be created without hitting any network."""
    from src.utils.earnings import EarningsCalendar
    cal = EarningsCalendar()
    assert cal is not None


def test_run_scanner_source_references_earnings_calendar() -> None:
    """run_scanner.py source contains EarningsCalendar wiring."""
    import inspect
    from scripts import run_scanner
    source = inspect.getsource(run_scanner.run_pipeline)
    assert "EarningsCalendar" in source
    assert "earnings_cal.prefetch" in source
    assert "earnings_cal.get_days_to_earnings" in source


def test_run_dashboard_source_references_earnings_calendar() -> None:
    """run_dashboard.py source contains EarningsCalendar wiring."""
    import inspect
    from scripts import run_dashboard
    source = inspect.getsource(run_dashboard._pipeline)
    assert "EarningsCalendar" in source
    assert "earnings_cal.prefetch" in source
    assert "earnings_cal.get_days_to_earnings" in source


def test_start_pipeline_thread_returns_daemon_thread() -> None:
    from unittest.mock import patch
    from src.dashboard.shared_state import SharedState
    from scripts.run_dashboard import start_pipeline_thread

    state = SharedState()

    async def _quick(state, symbols):
        return  # exits immediately so the thread ends cleanly

    with patch("scripts.run_dashboard._pipeline", _quick):
        thread = start_pipeline_thread(state, ["SPY"])

    assert thread.daemon is True
    thread.join(timeout=2.0)  # wait for the thread to finish cleanly


# ---------------------------------------------------------------------------
# T3: Script entry points
# ---------------------------------------------------------------------------

def test_run_scanner_main_catches_keyboard_interrupt(monkeypatch) -> None:
    """run_scanner __main__ try/except catches KeyboardInterrupt without re-raising."""
    import sys
    import runpy
    from unittest.mock import patch

    monkeypatch.setattr(sys, "argv", ["run_scanner.py", "SPY"])

    # Patch asyncio.run to raise KeyboardInterrupt, simulating Ctrl+C during the pipeline.
    # The __main__ block wraps asyncio.run in try/except KeyboardInterrupt; if the catch
    # is absent or broken, KeyboardInterrupt propagates out of run_module.
    with patch("asyncio.run", side_effect=KeyboardInterrupt("user Ctrl+C")):
        try:
            runpy.run_module("scripts.run_scanner", run_name="__main__", alter_sys=False)
        except KeyboardInterrupt:
            pytest.fail("KeyboardInterrupt escaped __main__ — catch is missing or broken")


@pytest.mark.asyncio
async def test_init_db_propagates_errors() -> None:
    """init_db() engine errors propagate (run_dashboard __main__ lets them fail loud)."""
    from src.storage.db import init_db
    from src.storage.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine
    from unittest.mock import patch

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    with patch.object(Base.metadata, "create_all", side_effect=RuntimeError("simulated disk full")):
        with pytest.raises(RuntimeError, match="simulated disk full"):
            await init_db(engine=engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_resilience_symbol_fetch_failure_does_not_kill_loop() -> None:
    """Fetch failure for one symbol must not prevent subsequent symbols from being processed."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.data.chain_fetcher import OptionChainSnapshot

    processed_symbols: list[str] = []

    class FakeStream:
        def __init__(self, *a, **kw): self.subscribed_count = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def subscribe(self, contracts, **kw): self.subscribed_count += len(contracts)

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def verify_connection(self): pass

    async def fake_fetch_chain(symbol: str):
        if symbol == "BAD":
            raise RuntimeError("Simulated network error")
        processed_symbols.append(symbol)
        return OptionChainSnapshot(
            underlying=symbol, underlying_price=500.0,
            timestamp=datetime.now(timezone.utc), contracts=[],
        )

    class FakeFetcher:
        def __init__(self, *a, **kw): pass
        async def fetch_chain(self, symbol: str): return await fake_fetch_chain(symbol)

    @asynccontextmanager
    async def fake_session():
        yield MagicMock()

    fake_earnings = MagicMock(
        prefetch=AsyncMock(),
        get_days_to_earnings=AsyncMock(return_value=None),
    )

    # src.connection.__init__ exports `ibkr_client` (singleton) which shadows the
    # submodule name under getattr; use sys.modules to reach the actual module.
    import importlib
    ibkr_mod = importlib.import_module("src.connection.ibkr_client")

    with patch.object(ibkr_mod, "IBKRClient", FakeClient), \
         patch("src.data.tick_stream.TickStream", FakeStream), \
         patch("src.data.chain_fetcher.ChainFetcher", FakeFetcher), \
         patch("src.storage.db.init_db", AsyncMock()), \
         patch("src.storage.db.get_session", fake_session), \
         patch("src.storage.queries.load_chain_snapshot", AsyncMock(return_value=None)), \
         patch("src.storage.queries.insert_chain_snapshot", AsyncMock()), \
         patch("src.utils.earnings.EarningsCalendar", return_value=fake_earnings):

        from scripts.run_scanner import run_pipeline
        # subscribed_count stays 0 (no contracts) → run_pipeline returns early without
        # entering the streaming loop, allowing the test to complete synchronously.
        await run_pipeline(["BAD", "GOOD"])

    assert "GOOD" in processed_symbols, "GOOD must be processed despite BAD failing"
    assert "BAD" not in processed_symbols
