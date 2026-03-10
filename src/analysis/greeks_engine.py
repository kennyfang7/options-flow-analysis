from __future__ import annotations

import math
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

from src.analysis.flow_classifier import ClassifiedTrade

if TYPE_CHECKING:
    from config.settings import Settings


# ---------------------------------------------------------------------------
# Black-Scholes math helpers (pure functions, no IO)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(
    S: float, K: float, T: float, r: float, sigma: float
) -> tuple[float, float]:
    """Compute Black-Scholes d1 and d2 intermediaries.

    Args:
        S: Underlying price.
        K: Strike price.
        T: Time to expiry in years (must be > 0).
        r: Risk-free rate (annualized decimal).
        sigma: Implied volatility (annualized decimal, must be > 0).

    Returns:
        Tuple (d1, d2).

    Raises:
        ValueError: If T <= 0, sigma <= 0, or S/K <= 0 (degenerate inputs).
    """
    if T <= 0 or sigma <= 0:
        raise ValueError(f"T and sigma must be positive; got T={T}, sigma={sigma}")
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def _bs_price(
    S: float, K: float, T: float, r: float, sigma: float, right: str
) -> float:
    """Black-Scholes option price.

    Args:
        S: Underlying price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        sigma: Implied volatility.
        right: "C" for call, "P" for put.

    Returns:
        Theoretical option price.
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    disc = math.exp(-r * T)
    if right == "C":
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(d1: float, right: str) -> float:
    """Black-Scholes delta.

    Args:
        d1: Pre-computed d1 from _d1_d2.
        right: "C" for call, "P" for put.

    Returns:
        Delta: [0, 1] for calls, [-1, 0] for puts.
    """
    cdf = _norm_cdf(d1)
    return cdf if right == "C" else cdf - 1.0


def _bs_gamma(S: float, d1: float, sigma: float, T: float) -> float:
    """Black-Scholes gamma (same for calls and puts).

    Args:
        S: Underlying price.
        d1: Pre-computed d1 from _d1_d2.
        sigma: Implied volatility.
        T: Time to expiry in years.

    Returns:
        Gamma (always positive).
    """
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def _bs_theta(
    S: float, K: float, T: float, r: float, sigma: float,
    d1: float, d2: float, right: str
) -> float:
    """Black-Scholes theta, expressed as per-calendar-day decay.

    Args:
        S: Underlying price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        sigma: Implied volatility.
        d1: Pre-computed d1 from _d1_d2.
        d2: Pre-computed d2 from _d1_d2.
        right: "C" for call, "P" for put.

    Returns:
        Theta in dollars per day (negative — options decay with time).
    """
    common = -(S * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
    disc = math.exp(-r * T)
    if right == "C":
        return (common - r * K * disc * _norm_cdf(d2)) / 365.0
    return (common + r * K * disc * _norm_cdf(-d2)) / 365.0


def _bs_vega(S: float, d1: float, T: float) -> float:
    """Black-Scholes vega per 1% change in implied volatility.

    Args:
        S: Underlying price.
        d1: Pre-computed d1 from _d1_d2.
        T: Time to expiry in years.

    Returns:
        Vega (always positive). Scaled to per 1% IV move (divide by 100).
    """
    return S * _norm_pdf(d1) * math.sqrt(T) / 100.0


def _implied_vol(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: str,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float | None:
    """Estimate implied volatility via Newton-Raphson iteration.

    Args:
        price: Observed market price of the option.
        S: Underlying price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        right: "C" for call, "P" for put.
        max_iter: Maximum iterations before giving up.
        tol: Price convergence tolerance.

    Returns:
        Implied volatility (annualized decimal), or None if non-convergent
        or inputs are degenerate (price=0, T=0, S=0, etc.).
    """
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None

    sigma = 0.30  # initial guess
    for _ in range(max_iter):
        try:
            d1, d2 = _d1_d2(S, K, T, r, sigma)
        except (ValueError, ZeroDivisionError):
            return None

        bs = _bs_price(S, K, T, r, sigma, right)
        # Raw vega (not scaled by /100) for Newton step
        raw_vega = S * _norm_pdf(d1) * math.sqrt(T)
        if abs(raw_vega) < 1e-10:
            return None

        sigma -= (bs - price) / raw_vega
        if sigma < 1e-6:
            sigma = 1e-6

        if abs(bs - price) < tol:
            return max(sigma, 1e-6)

    return None  # did not converge


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


class Moneyness(str, Enum):
    """Price-based moneyness classification for an option contract."""

    ITM = "itm"
    ATM = "atm"
    OTM = "otm"
    UNKNOWN = "unknown"  # underlying_price unavailable


def _days_to_expiry(expiry: str) -> int:
    """Compute calendar days until expiry from an YYYYMMDD string.

    Args:
        expiry: Expiration date in YYYYMMDD format (e.g. "20260320").

    Returns:
        Days remaining (0 if already expired or expiring today).
    """
    exp_date = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
    delta = (exp_date - date.today()).days
    return max(delta, 0)


def _classify_moneyness(
    underlying_price: float | None, strike: float, right: str
) -> Moneyness:
    """Classify an option as ITM, ATM, or OTM using price ratio.

    Uses a ±1% band around the strike to define ATM.

    Args:
        underlying_price: Current price of the underlying. None → UNKNOWN.
        strike: Option strike price.
        right: "C" for call, "P" for put.

    Returns:
        Moneyness enum value.
    """
    if underlying_price is None:
        return Moneyness.UNKNOWN

    ratio = underlying_price / strike  # > 1 means underlying is above strike

    if right == "C":
        if ratio > 1.01:
            return Moneyness.ITM
        if ratio < 0.99:
            return Moneyness.OTM
        return Moneyness.ATM
    else:  # Put
        if ratio < 0.99:
            return Moneyness.ITM
        if ratio > 1.01:
            return Moneyness.OTM
        return Moneyness.ATM


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class EnrichedTrade(ClassifiedTrade):
    """A ClassifiedTrade with full Greeks and context fields attached.

    Emitted by GreeksEngine.enrich(). Inherits all ClassifiedTrade fields;
    delta and implied_vol may be overridden with Black-Scholes estimates
    when IBKR's modelGreeks are unavailable.

    Attributes:
        gamma: Rate of delta change per $1 move in underlying. None when
            unavailable and BS inputs are insufficient.
        theta: Per-calendar-day decay in option value (typically negative).
        vega: Change in option value per 1% rise in implied vol.
        days_to_expiry: Calendar days until expiry at enrich() call time.
        moneyness: Price-based ITM/ATM/OTM classification.
        iv_source: Origin of implied_vol: "ibkr", "black_scholes", or "unavailable".
    """

    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    days_to_expiry: int = 0
    moneyness: Moneyness = Moneyness.UNKNOWN
    iv_source: str = "unavailable"
