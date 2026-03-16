from __future__ import annotations

from pathlib import Path
import pytest


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
