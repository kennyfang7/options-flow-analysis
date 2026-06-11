from __future__ import annotations

from pathlib import Path

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
