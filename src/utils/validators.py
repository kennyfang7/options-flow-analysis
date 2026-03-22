from __future__ import annotations

"""Pure validation helpers for IBKR market data.

All functions are stateless and have no I/O side-effects.  They are intended
to be called from pydantic field_validators and from the data-layer entry-point
gates in chain_fetcher and tick_stream.

Conventions
-----------
- Functions named ``is_*`` return a ``bool``.  ``True`` means the value is
  acceptable; ``False`` means it should be rejected or coerced to ``None``.
- Functions named ``clamp_*`` return the original value unchanged when it is
  within bounds, otherwise ``None``.
- ``sanitize_right`` normalises a raw IBKR right character to ``"C"`` or ``"P"``.
"""

import re
from datetime import date


# ---------------------------------------------------------------------------
# Price / spread helpers
# ---------------------------------------------------------------------------


def is_price_valid(value: float | None) -> bool:
    """Return True if value is None or non-negative.

    None is acceptable (means data not yet available from IBKR).
    Negative prices are never valid for options.

    Args:
        value: Raw price field (bid, ask, last, etc.).

    Returns:
        True if valid or None; False if negative.
    """
    if value is None:
        return True
    return value >= 0.0


def is_bid_ask_consistent(bid: float | None, ask: float | None) -> bool:
    """Return True if the bid/ask spread is not inverted.

    A spread is considered inverted only when both values are present and
    bid > ask.  Missing values are acceptable.

    Args:
        bid: Best bid price.
        ask: Best ask price.

    Returns:
        True if the spread is valid; False if bid > ask.
    """
    if bid is None or ask is None:
        return True
    return bid <= ask


def has_any_price(
    bid: float | None,
    ask: float | None,
    last: float | None,
) -> bool:
    """Return True if at least one price field is present.

    Used to detect completely empty ticks that carry no actionable data.

    Args:
        bid: Best bid price.
        ask: Best ask price.
        last: Last traded price.

    Returns:
        True if at least one price field is not None.
    """
    return bid is not None or ask is not None or last is not None


# ---------------------------------------------------------------------------
# Contract identity helpers
# ---------------------------------------------------------------------------


def is_strike_valid(strike: float) -> bool:
    """Return True if the strike is strictly positive.

    A strike of 0 or negative is never a valid option strike price.

    Args:
        strike: Option strike price.

    Returns:
        True if strike > 0.
    """
    return strike > 0.0


_EXPIRY_RE = re.compile(r"^\d{8}$")


def is_expiry_valid(expiry: str) -> bool:
    """Return True if expiry is a valid YYYYMMDD date string.

    Checks format (8 digits) and calendar validity (no month 13, etc.).

    Args:
        expiry: Expiration date string in YYYYMMDD format.

    Returns:
        True if the string is a parseable YYYYMMDD date.
    """
    if not _EXPIRY_RE.match(expiry):
        return False
    try:
        date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
        return True
    except ValueError:
        return False


def is_con_id_valid(con_id: int | None) -> bool:
    """Return True if con_id is None (not yet qualified) or a positive integer.

    IBKR uses con_id=0 as a sentinel meaning "unqualified contract".  Zero is
    not a valid contract ID and must be treated as missing.

    Args:
        con_id: IBKR contract identifier.

    Returns:
        True if con_id is None or > 0; False if con_id == 0.
    """
    if con_id is None:
        return True
    return con_id > 0


# ---------------------------------------------------------------------------
# Volume / open interest
# ---------------------------------------------------------------------------


def is_volume_valid(volume: int | None) -> bool:
    """Return True if volume is None or non-negative.

    Args:
        volume: Session cumulative volume.

    Returns:
        True if valid or None; False if negative.
    """
    if volume is None:
        return True
    return volume >= 0


# ---------------------------------------------------------------------------
# Greeks / IV helpers
# ---------------------------------------------------------------------------

_IV_MAX: float = 50.0   # 5000 % — deliberately generous upper bound


def is_implied_vol_valid(iv: float | None) -> bool:
    """Return True if implied vol is None or within [0, 50].

    The upper bound of 50.0 (5000 % IV) is generous enough to cover extreme
    meme-stock scenarios while still catching obviously corrupt values.

    Args:
        iv: Implied volatility as a decimal (e.g. 0.25 = 25 %).

    Returns:
        True if iv is None or 0 <= iv <= 50.0.
    """
    if iv is None:
        return True
    return 0.0 <= iv <= _IV_MAX


def is_delta_valid(delta: float | None) -> bool:
    """Return True if delta is None or within [-1, 1].

    Option delta is always bounded by [-1.0, 1.0] for standard contracts.

    Args:
        delta: Delta greek.

    Returns:
        True if delta is None or -1.0 <= delta <= 1.0.
    """
    if delta is None:
        return True
    return -1.0 <= delta <= 1.0


def clamp_implied_vol(iv: float | None) -> float | None:
    """Return iv unchanged if within bounds, else None.

    Silent coercion is appropriate here because the contract remains usable
    even without an IV value — the pipeline falls back to Black-Scholes.

    Args:
        iv: Implied volatility as a decimal.

    Returns:
        iv if 0 <= iv <= 50.0, else None.  None input returns None.
    """
    if iv is None:
        return None
    return iv if 0.0 <= iv <= _IV_MAX else None


def clamp_delta(delta: float | None) -> float | None:
    """Return delta unchanged if within [-1, 1], else None.

    Args:
        delta: Delta greek.

    Returns:
        delta if -1.0 <= delta <= 1.0, else None.  None input returns None.
    """
    if delta is None:
        return None
    return delta if -1.0 <= delta <= 1.0 else None


# ---------------------------------------------------------------------------
# Right normalisation
# ---------------------------------------------------------------------------


def sanitize_right(right: str) -> str:
    """Normalise option right to uppercase "C" or "P".

    Args:
        right: Raw right character from IBKR (e.g. "c", "C", "P", "p").

    Returns:
        "C" or "P".

    Raises:
        ValueError: If the value is not a valid call/put indicator.
    """
    normalised = right.upper() if right else ""
    if normalised not in ("C", "P"):
        raise ValueError(f"Invalid option right: {right!r}. Must be 'C' or 'P'.")
    return normalised
