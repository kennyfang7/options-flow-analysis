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
        s.upper()
        for line in p.read_text().splitlines()
        if (s := line.strip()) and not s.startswith("#")
    ]
    logger.info("Loaded {} symbols from {}", len(symbols), path)
    return symbols
