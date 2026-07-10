from __future__ import annotations

"""Watchlist manager for persistent, runtime-editable ticker lists.

Provides :class:`WatchlistManager` — a JSON-backed store for ticker symbols
with per-symbol metadata (enabled flag, group label, notes).  A plain-text
``.txt`` file (one symbol per line) is also accepted for backward
compatibility with the legacy ``load_watchlist`` helper.

Typical usage::

    wm = WatchlistManager("config/watchlist.json")
    wm.add("SPY", group="etfs")
    wm.add("AAPL", group="tech", notes="Earnings play")
    wm.save()

    # In a long-running process — reload on file change without restart
    changed = wm.reload_if_changed()
    symbols = wm.active_symbols()
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from loguru import logger
from pydantic import BaseModel, Field, field_validator


_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class WatchlistEntry(BaseModel):
    """A single symbol record in the watchlist.

    Args:
        symbol: Uppercase ticker symbol (1–6 alpha chars, e.g. ``"SPY"``).
        enabled: Whether this symbol is included in active scans.
        group: Freeform category label (e.g. ``"etfs"``, ``"tech"``).
        added_at: UTC timestamp when the entry was created.
        notes: Optional freeform annotation.
    """

    symbol: str
    enabled: bool = True
    group: str = "default"
    added_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC time when the symbol was added",
    )
    notes: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def symbol_must_be_valid(cls, v: object) -> str:
        """Normalise to uppercase and enforce IBKR ticker format.

        Args:
            v: Raw input value for the symbol field.

        Returns:
            Uppercase ticker string.

        Raises:
            ValueError: If the value is not a string or fails the regex.
        """
        if not isinstance(v, str):
            raise ValueError(f"symbol must be a string, got {type(v).__name__!r}")
        upper = v.strip().upper()
        if not _SYMBOL_RE.match(upper):
            raise ValueError(
                f"Invalid ticker symbol {v!r}. Must be 1–6 uppercase letters (A–Z)."
            )
        return upper


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class WatchlistManager:
    """Manages a persistent, runtime-editable watchlist of ticker symbols.

    Persists to a JSON file with the following structure::

        {
          "symbols": [
            {"symbol": "SPY", "enabled": true, "group": "etfs", ...},
            ...
          ]
        }

    A plain-text ``.txt`` watchlist (one symbol per line, ``#`` comments)
    is accepted on :meth:`load` for backward compatibility.  :meth:`save`
    always writes JSON regardless of the source format.

    Args:
        path: Path to the watchlist file (JSON or plain text).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, WatchlistEntry] = {}
        self._mtime: float = 0.0
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the watchlist from disk.

        Handles JSON (``.json``) and plain-text (``.txt``) files.  Starts
        with an empty list if the file does not exist.
        """
        if not self._path.exists():
            logger.debug("Watchlist not found at {}, starting empty", self._path)
            self._entries = {}
            self._mtime = 0.0
            return

        self._mtime = self._path.stat().st_mtime

        if self._path.suffix.lower() == ".json":
            self._load_json()
        else:
            self._load_txt()

    def _load_json(self) -> None:
        """Parse a JSON watchlist file."""
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            logger.exception("Failed to parse watchlist JSON at {}", self._path)
            self._entries = {}
            return

        entries: dict[str, WatchlistEntry] = {}
        for item in data.get("symbols", []):
            try:
                entry = WatchlistEntry.model_validate(item)
                entries[entry.symbol] = entry
            except Exception as exc:
                logger.warning("Skipping invalid watchlist entry {!r}: {}", item, exc)

        self._entries = entries
        logger.info("Loaded {} symbols from {}", len(self._entries), self._path)

    def _load_txt(self) -> None:
        """Parse a legacy plain-text watchlist (one symbol per line)."""
        entries: dict[str, WatchlistEntry] = {}
        for line in self._path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                entry = WatchlistEntry(symbol=s)
                entries[entry.symbol] = entry
            except Exception as exc:
                logger.warning("Skipping invalid symbol {!r}: {}", s, exc)

        self._entries = entries
        logger.info(
            "Loaded {} symbols from {} (plain text)", len(self._entries), self._path
        )

    def save(self) -> None:
        """Persist the current watchlist to disk as JSON.

        Saves to the configured path (converting ``.txt`` suffix to ``.json``
        if necessary so the richer format is preserved). Uses atomic write
        (temp file + rename) to prevent corruption on crash.
        """
        save_path = (
            self._path.with_suffix(".json")
            if self._path.suffix.lower() != ".json"
            else self._path
        )
        save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "symbols": [
                e.model_dump(mode="json") for e in self._entries.values()
            ]
        }
        tmp_path = save_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
            tmp_path.replace(save_path)
        except OSError:
            logger.exception("Failed to save watchlist to {}", save_path)
            tmp_path.unlink(missing_ok=True)
            raise
        self._mtime = save_path.stat().st_mtime
        logger.info("Saved {} symbols to {}", len(self._entries), save_path)

    def reload_if_changed(self) -> bool:
        """Reload from disk if the file has been modified since last load.

        Intended for use inside long-running processes that need to pick up
        watchlist edits without restarting.

        Returns:
            True if the file was modified and the watchlist was reloaded;
            False otherwise.
        """
        if not self._path.exists():
            return False
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            self.load()
            return True
        return False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        symbol: str,
        *,
        group: str = "default",
        notes: str = "",
        enabled: bool = True,
    ) -> WatchlistEntry:
        """Add or update a symbol in the watchlist.

        If the symbol already exists, its group/notes/enabled fields are
        updated in-place.  ``added_at`` is preserved on updates.

        Args:
            symbol: Ticker symbol to add (case-insensitive; normalised to
                uppercase).
            group: Category label for the symbol.
            notes: Optional freeform annotation.
            enabled: Whether the symbol is immediately active.

        Returns:
            The created or updated :class:`WatchlistEntry`.

        Raises:
            ValueError: If the symbol fails the ticker validation regex.
        """
        # Validate via WatchlistEntry to get consistent normalisation
        candidate = WatchlistEntry(
            symbol=symbol,
            group=group,
            notes=notes,
            enabled=enabled,
        )
        existing = self._entries.get(candidate.symbol)
        if existing is not None:
            # Preserve original added_at on update
            candidate = candidate.model_copy(update={"added_at": existing.added_at})

        self._entries[candidate.symbol] = candidate
        logger.debug(
            "Added {} to watchlist (group={}, enabled={})",
            candidate.symbol, group, enabled,
        )
        return candidate

    def remove(self, symbol: str) -> bool:
        """Remove a symbol from the watchlist.

        Args:
            symbol: Ticker symbol to remove (case-insensitive).

        Returns:
            True if the symbol was present and removed; False if not found.
        """
        sym = symbol.strip().upper()
        if sym in self._entries:
            del self._entries[sym]
            logger.debug("Removed {} from watchlist", sym)
            return True
        return False

    def enable(self, symbol: str) -> bool:
        """Mark a symbol as enabled for active scanning.

        Args:
            symbol: Ticker to enable (case-insensitive).

        Returns:
            True if the symbol was found and updated; False if not found.
        """
        sym = symbol.strip().upper()
        if sym in self._entries:
            self._entries[sym] = self._entries[sym].model_copy(
                update={"enabled": True}
            )
            return True
        return False

    def disable(self, symbol: str) -> bool:
        """Mark a symbol as disabled without removing it from the watchlist.

        Useful for temporarily pausing a symbol without losing its metadata.

        Args:
            symbol: Ticker to disable (case-insensitive).

        Returns:
            True if the symbol was found and updated; False if not found.
        """
        sym = symbol.strip().upper()
        if sym in self._entries:
            self._entries[sym] = self._entries[sym].model_copy(
                update={"enabled": False}
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, symbol: str) -> WatchlistEntry | None:
        """Retrieve a single entry by symbol.

        Args:
            symbol: Ticker to look up (case-insensitive).

        Returns:
            The :class:`WatchlistEntry` or ``None`` if not found.
        """
        return self._entries.get(symbol.strip().upper())

    def all_symbols(self) -> list[str]:
        """Return all symbols regardless of enabled status.

        Returns:
            Ordered list of symbol strings.
        """
        return list(self._entries.keys())

    def active_symbols(self) -> list[str]:
        """Return only enabled symbols.

        Returns:
            List of symbol strings for all entries where ``enabled=True``.
        """
        return [e.symbol for e in self._entries.values() if e.enabled]

    def symbols_by_group(self, group: str) -> list[str]:
        """Return active symbols belonging to a specific group.

        Args:
            group: Group name to filter by.

        Returns:
            List of enabled symbol strings in that group.
        """
        return [
            e.symbol
            for e in self._entries.values()
            if e.enabled and e.group == group
        ]

    def groups(self) -> list[str]:
        """Return a sorted list of unique group names in the watchlist.

        Returns:
            Sorted list of group name strings.
        """
        return sorted({e.group for e in self._entries.values()})

    # ------------------------------------------------------------------
    # Python data model
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[WatchlistEntry]:
        return iter(self._entries.values())

    def __contains__(self, symbol: object) -> bool:
        if isinstance(symbol, str):
            return symbol.strip().upper() in self._entries
        return False

    def __repr__(self) -> str:
        return (
            f"WatchlistManager(path={str(self._path)!r}, "
            f"total={len(self._entries)}, "
            f"active={len(self.active_symbols())})"
        )


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "config/watchlist.json"
    wm = WatchlistManager(path)
    print(f"Watchlist: {wm}")
    print(f"Active symbols: {wm.active_symbols()}")
    print(f"Groups: {wm.groups()}")
