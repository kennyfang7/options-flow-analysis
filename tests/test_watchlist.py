from __future__ import annotations

"""Tests for src.utils.watchlist — WatchlistEntry and WatchlistManager."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.utils.watchlist import WatchlistEntry, WatchlistManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_json(symbols: list[dict]) -> str:
    """Serialise a list of symbol dicts into the expected JSON format."""
    return json.dumps({"symbols": symbols}, indent=2)


# ---------------------------------------------------------------------------
# WatchlistEntry validation
# ---------------------------------------------------------------------------


class TestWatchlistEntry:
    def test_valid_symbol_uppercases(self):
        e = WatchlistEntry(symbol="aapl")
        assert e.symbol == "AAPL"

    def test_valid_symbol_strips_whitespace(self):
        e = WatchlistEntry(symbol="  spy  ")
        assert e.symbol == "SPY"

    @pytest.mark.parametrize(
        "sym",
        ["SPY", "AAPL", "A", "GOOGL", "BRK", "TSM"],
    )
    def test_valid_tickers(self, sym: str):
        e = WatchlistEntry(symbol=sym)
        assert e.symbol == sym

    @pytest.mark.parametrize(
        "bad",
        ["", "1234", "TOOLONG", "SP Y", "SP-Y", "SP.Y", "123ABC"],
    )
    def test_invalid_symbols_raise(self, bad: str):
        with pytest.raises(ValidationError):
            WatchlistEntry(symbol=bad)

    def test_non_string_raises(self):
        with pytest.raises(ValidationError):
            WatchlistEntry(symbol=42)  # type: ignore[arg-type]

    def test_defaults(self):
        e = WatchlistEntry(symbol="SPY")
        assert e.enabled is True
        assert e.group == "default"
        assert e.notes == ""
        assert isinstance(e.added_at, datetime)
        assert e.added_at.tzinfo is not None

    def test_custom_fields(self):
        e = WatchlistEntry(symbol="AAPL", enabled=False, group="tech", notes="Watch")
        assert e.enabled is False
        assert e.group == "tech"
        assert e.notes == "Watch"


# ---------------------------------------------------------------------------
# WatchlistManager — empty / missing file
# ---------------------------------------------------------------------------


class TestWatchlistManagerEmpty:
    def test_missing_file_starts_empty(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "missing.json")
        assert len(wm) == 0
        assert wm.active_symbols() == []
        assert wm.all_symbols() == []

    def test_repr(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        r = repr(wm)
        assert "WatchlistManager" in r
        assert "total=0" in r
        assert "active=0" in r


# ---------------------------------------------------------------------------
# WatchlistManager — CRUD
# ---------------------------------------------------------------------------


class TestWatchlistManagerCRUD:
    def test_add_normalises_symbol(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        entry = wm.add("aapl")
        assert entry.symbol == "AAPL"
        assert "AAPL" in wm

    def test_add_returns_entry(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        entry = wm.add("SPY", group="etfs", notes="Broad market")
        assert isinstance(entry, WatchlistEntry)
        assert entry.group == "etfs"
        assert entry.notes == "Broad market"

    def test_add_invalid_symbol_raises(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        with pytest.raises(ValidationError):
            wm.add("TOOLONG")

    def test_add_updates_existing_preserves_added_at(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        first = wm.add("AAPL", group="tech")
        original_ts = first.added_at
        updated = wm.add("AAPL", group="big-tech", notes="updated")
        assert updated.group == "big-tech"
        assert updated.notes == "updated"
        assert updated.added_at == original_ts
        assert len(wm) == 1  # no duplicate

    def test_remove_returns_true_when_found(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        assert wm.remove("spy") is True
        assert "SPY" not in wm

    def test_remove_returns_false_when_not_found(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        assert wm.remove("NOPE") is False

    def test_enable_unknown_returns_false(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        assert wm.enable("NOPE") is False

    def test_enable_marks_symbol_active(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY", enabled=False)
        assert wm.enable("spy") is True
        assert wm.get("SPY").enabled is True  # type: ignore[union-attr]

    def test_disable_unknown_returns_false(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        assert wm.disable("NOPE") is False

    def test_disable_marks_symbol_inactive(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        assert wm.disable("spy") is True
        assert wm.get("SPY").enabled is False  # type: ignore[union-attr]

    def test_get_returns_none_for_unknown(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        assert wm.get("NOPE") is None

    def test_get_returns_entry(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("AAPL", group="tech")
        entry = wm.get("aapl")
        assert entry is not None
        assert entry.symbol == "AAPL"
        assert entry.group == "tech"


# ---------------------------------------------------------------------------
# WatchlistManager — queries
# ---------------------------------------------------------------------------


class TestWatchlistManagerQueries:
    def test_all_symbols_includes_disabled(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        wm.add("AAPL", enabled=False)
        assert set(wm.all_symbols()) == {"SPY", "AAPL"}

    def test_active_symbols_excludes_disabled(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        wm.add("AAPL", enabled=False)
        assert wm.active_symbols() == ["SPY"]

    def test_active_symbols_empty_when_all_disabled(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY", enabled=False)
        assert wm.active_symbols() == []

    def test_symbols_by_group_only_active(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY", group="etfs")
        wm.add("QQQ", group="etfs", enabled=False)
        wm.add("AAPL", group="tech")
        result = wm.symbols_by_group("etfs")
        assert result == ["SPY"]

    def test_symbols_by_group_unknown_returns_empty(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        assert wm.symbols_by_group("nonexistent") == []

    def test_groups_sorted_unique(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY", group="etfs")
        wm.add("QQQ", group="etfs")
        wm.add("AAPL", group="tech")
        assert wm.groups() == ["etfs", "tech"]

    def test_groups_empty_when_no_entries(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        assert wm.groups() == []


# ---------------------------------------------------------------------------
# WatchlistManager — Python data model
# ---------------------------------------------------------------------------


class TestWatchlistManagerDunder:
    def test_len(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        wm.add("AAPL")
        assert len(wm) == 2

    def test_contains_string(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        assert "SPY" in wm
        assert "spy" in wm  # case-insensitive
        assert "AAPL" not in wm

    def test_contains_non_string_false(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        assert 42 not in wm

    def test_iter_yields_entries(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        wm.add("AAPL")
        entries = list(wm)
        assert all(isinstance(e, WatchlistEntry) for e in entries)
        assert {e.symbol for e in entries} == {"SPY", "AAPL"}


# ---------------------------------------------------------------------------
# WatchlistManager — JSON persistence
# ---------------------------------------------------------------------------


class TestWatchlistManagerJSON:
    def test_save_creates_json_file(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "w.json")
        wm.add("SPY")
        wm.save()
        assert (tmp_path / "w.json").exists()

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        path = tmp_path / "w.json"
        wm = WatchlistManager(path)
        wm.add("SPY", group="etfs", notes="Broad market")
        wm.add("AAPL", enabled=False, group="tech")
        wm.save()

        wm2 = WatchlistManager(path)
        assert len(wm2) == 2
        spy = wm2.get("SPY")
        assert spy is not None
        assert spy.group == "etfs"
        assert spy.notes == "Broad market"
        assert spy.enabled is True
        aapl = wm2.get("AAPL")
        assert aapl is not None
        assert aapl.enabled is False
        assert aapl.group == "tech"

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "w.json"
        wm = WatchlistManager(nested)
        wm.add("SPY")
        wm.save()
        assert nested.exists()

    def test_load_skips_invalid_entries(self, tmp_path: Path):
        path = tmp_path / "w.json"
        path.write_text(
            _make_json([
                {"symbol": "SPY", "enabled": True, "group": "default", "notes": ""},
                {"symbol": "NOT_VALID_TICKER", "enabled": True, "group": "default", "notes": ""},
                {"symbol": 12345, "enabled": True, "group": "default", "notes": ""},
            ])
        )
        wm = WatchlistManager(path)
        assert len(wm) == 1
        assert "SPY" in wm

    def test_load_corrupt_json_starts_empty(self, tmp_path: Path):
        path = tmp_path / "w.json"
        path.write_text("{{not valid json}}")
        wm = WatchlistManager(path)
        assert len(wm) == 0

    def test_save_updates_mtime(self, tmp_path: Path):
        path = tmp_path / "w.json"
        wm = WatchlistManager(path)
        wm.add("SPY")
        wm.save()
        mtime_after_save = wm._mtime
        assert mtime_after_save > 0.0


# ---------------------------------------------------------------------------
# WatchlistManager — plain-text backward compatibility
# ---------------------------------------------------------------------------


class TestWatchlistManagerTxt:
    def test_load_txt_reads_symbols(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("SPY\nAAPL\nMSFT\n")
        wm = WatchlistManager(path)
        assert set(wm.all_symbols()) == {"SPY", "AAPL", "MSFT"}

    def test_load_txt_strips_comments(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("# tech\nAAPL\n# etf\nSPY\n")
        wm = WatchlistManager(path)
        assert set(wm.all_symbols()) == {"AAPL", "SPY"}

    def test_load_txt_strips_blank_lines(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("\nSPY\n\nAAPL\n\n")
        wm = WatchlistManager(path)
        assert len(wm) == 2

    def test_load_txt_upcases_symbols(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("spy\naapl\n")
        wm = WatchlistManager(path)
        assert "SPY" in wm
        assert "AAPL" in wm

    def test_load_txt_skips_invalid(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("SPY\nNOT_VALID\n\n")
        wm = WatchlistManager(path)
        assert len(wm) == 1
        assert "SPY" in wm

    def test_save_from_txt_writes_json(self, tmp_path: Path):
        txt_path = tmp_path / "watchlist.txt"
        txt_path.write_text("SPY\nAAPL\n")
        wm = WatchlistManager(txt_path)
        wm.save()
        json_path = tmp_path / "watchlist.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        symbols = [s["symbol"] for s in data["symbols"]]
        assert set(symbols) == {"SPY", "AAPL"}

    def test_all_entries_from_txt_default_enabled(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("SPY\nAAPL\n")
        wm = WatchlistManager(path)
        assert all(e.enabled for e in wm)

    def test_all_entries_from_txt_default_group(self, tmp_path: Path):
        path = tmp_path / "watchlist.txt"
        path.write_text("SPY\n")
        wm = WatchlistManager(path)
        assert wm.get("SPY").group == "default"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# WatchlistManager — hot-reload
# ---------------------------------------------------------------------------


class TestWatchlistManagerHotReload:
    def test_reload_if_changed_returns_false_when_unchanged(self, tmp_path: Path):
        path = tmp_path / "w.json"
        wm = WatchlistManager(path)
        wm.add("SPY")
        wm.save()
        # No external change — should return False
        assert wm.reload_if_changed() is False

    def test_reload_if_changed_returns_false_for_missing_file(self, tmp_path: Path):
        wm = WatchlistManager(tmp_path / "missing.json")
        assert wm.reload_if_changed() is False

    def test_reload_if_changed_picks_up_external_edit(self, tmp_path: Path):
        path = tmp_path / "w.json"
        # Initial save
        wm = WatchlistManager(path)
        wm.add("SPY")
        wm.save()

        # Simulate an external edit by writing a new file with a different mtime
        time.sleep(0.02)  # ensure mtime differs
        path.write_text(
            _make_json([
                {"symbol": "AAPL", "enabled": True, "group": "default", "notes": ""},
            ])
        )
        # Touch mtime to be different
        new_mtime = path.stat().st_mtime + 1
        import os
        os.utime(path, (new_mtime, new_mtime))

        changed = wm.reload_if_changed()
        assert changed is True
        assert "AAPL" in wm
        assert "SPY" not in wm

    def test_reload_if_changed_updates_symbols(self, tmp_path: Path):
        path = tmp_path / "w.json"
        wm = WatchlistManager(path)
        wm.add("SPY")
        wm.save()
        original_mtime = wm._mtime

        # Write a modified version directly
        import os
        path.write_text(
            _make_json([
                {"symbol": "QQQ", "enabled": True, "group": "default", "notes": ""},
            ])
        )
        os.utime(path, (original_mtime + 1, original_mtime + 1))

        wm.reload_if_changed()
        assert "QQQ" in wm
        assert "SPY" not in wm
