from __future__ import annotations

from pathlib import Path


def test_load_watchlist_reads_symbols(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\nQQQ\nAAPL\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ", "AAPL"]


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


def test_load_watchlist_strips_indented_comments(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text("SPY\n  # indented comment\nQQQ\n")
    from scripts.run_scanner import load_watchlist
    assert load_watchlist(str(wl)) == ["SPY", "QQQ"]


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
