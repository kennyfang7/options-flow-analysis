from __future__ import annotations

import math
from datetime import date, datetime, timezone
from enum import Enum

from loguru import logger

from config.settings import Settings
from src.analysis.flow_classifier import ClassifiedTrade


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
        sigma = max(1e-6, min(sigma, 10.0))  # IV > 1000% is not physical

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
    today_utc = datetime.now(timezone.utc).date()
    delta = (exp_date - today_utc).days
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
    days_to_earnings: int | None = None


# ---------------------------------------------------------------------------
# GreeksEngine
# ---------------------------------------------------------------------------


class GreeksEngine:
    """Synchronous Greeks enrichment layer for ClassifiedTrade objects.

    Uses IBKR's modelGreeks (already on TickUpdate) as the primary source.
    Falls back to Black-Scholes computation when IBKR data is absent.

    No IO is performed — safe to call on the hot path between
    FlowClassifier.classify() and UnusualDetector.detect().

    Example:
        engine = GreeksEngine(settings)
        enriched = engine.enrich(trade)
        signal = await detector.detect(enriched)

    Args:
        settings: Application settings (uses risk_free_rate for BS fallback).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def enrich(self, trade: ClassifiedTrade) -> EnrichedTrade:
        """Attach full Greeks and context to a ClassifiedTrade.

        Strategy (for each Greek):
          1. Use IBKR-provided value from trade.tick if non-None.
          2. If IV is available but gamma/theta/vega are None, compute via BS.
          3. If IV is None but effective_price and underlying are available,
             compute IV via Newton-Raphson, then derive all Greeks via BS.
          4. Leave as None if inputs are insufficient.

        Args:
            trade: ClassifiedTrade from FlowClassifier.classify().

        Returns:
            EnrichedTrade with Greeks, moneyness, and days_to_expiry populated.
        """
        tick = trade.tick
        r = self._settings.risk_free_rate

        # --- Step 1: Collect IBKR values ---
        delta = trade.delta
        implied_vol = trade.implied_vol
        gamma: float | None = tick.gamma
        theta: float | None = tick.theta
        vega: float | None = tick.vega
        iv_source = "ibkr" if implied_vol is not None else "unavailable"

        # --- Step 2: Black-Scholes fallback ---
        S = trade.underlying_price
        K = trade.strike
        T_days = _days_to_expiry(trade.expiry)
        T = T_days / 365.0

        bs_available = S is not None and S > 0 and K > 0 and T > 0

        if bs_available:
            # 2a. Compute IV from option price if IBKR didn't provide it
            if implied_vol is None and trade.effective_price is not None:
                computed_iv = _implied_vol(
                    price=trade.effective_price,
                    S=S,  # type: ignore[arg-type]
                    K=K,
                    T=T,
                    r=r,
                    right=trade.right,
                )
                if computed_iv is not None:
                    implied_vol = computed_iv
                    iv_source = "black_scholes"

            # 2b. Derive any missing Greeks from IV via BS
            if implied_vol is not None and implied_vol > 0:
                try:
                    d1, d2 = _d1_d2(S, K, T, r, implied_vol)  # type: ignore[arg-type]
                    if delta is None:
                        delta = _bs_delta(d1, trade.right)
                    if gamma is None:
                        gamma = _bs_gamma(S, d1, implied_vol, T)  # type: ignore[arg-type]
                    if theta is None:
                        theta = _bs_theta(S, K, T, r, implied_vol, d1, d2, trade.right)  # type: ignore[arg-type]
                    if vega is None:
                        vega = _bs_vega(S, d1, T)  # type: ignore[arg-type]
                except (ValueError, ZeroDivisionError):
                    logger.debug(
                        "greeks_engine: BS fallback failed for con_id={} expiry={}",
                        trade.con_id, trade.expiry,
                    )

        # --- Step 3: Context fields ---
        moneyness = _classify_moneyness(trade.underlying_price, trade.strike, trade.right)

        # --- Step 4: Build EnrichedTrade ---
        # model_dump() excludes fields marked Field(exclude=True) on ClassifiedTrade
        # (currently: 'tick'). Those fields must be re-injected manually below.
        # CONTRACT: if future ClassifiedTrade fields gain exclude=True, add them here.
        base = trade.model_dump()
        base["delta"] = delta
        base["implied_vol"] = implied_vol

        return EnrichedTrade(
            **base,
            tick=tick,
            gamma=gamma,
            theta=theta,
            vega=vega,
            days_to_expiry=T_days,
            moneyness=moneyness,
            iv_source=iv_source,
        )

    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """No-op — GreeksEngine is stateless.

        Included for interface consistency with FlowClassifier and UnusualDetector,
        which both expose purge_stale() for the orchestration layer to call hourly.

        Returns:
            Always 0.
        """
        return 0


if __name__ == "__main__":
    import asyncio
    from datetime import datetime, timezone, timedelta
    from datetime import date as _date

    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.unusual_detector import UnusualDetector
    from src.data.tick_stream import TickUpdate

    async def main() -> None:
        settings = Settings(
            min_premium=100.0,
            unusual_premium_threshold=50_000.0,
            unusual_oi_ratio_threshold=0.50,
            unusual_signal_threshold=5.0,
            otm_delta_threshold=0.30,
            otm_premium_threshold=30_000.0,
            risk_free_rate=0.05,
        )
        classifier = FlowClassifier(settings)
        engine = GreeksEngine(settings)
        detector = UnusualDetector(settings)

        future_expiry = (_date.today() + timedelta(days=90)).strftime("%Y%m%d")
        base_time = datetime(2026, 3, 10, 14, 30, 0, tzinfo=timezone.utc)

        # Scenario 1: IBKR provides full Greeks
        logger.info("--- Scenario 1: IBKR Greeks present ---")
        for i in range(3):
            tick = TickUpdate(
                symbol="SPY", con_id=99001, expiry=future_expiry, strike=500.0, right="C",
                timestamp=base_time + timedelta(milliseconds=i * 400),
                bid=10.00, ask=10.50, last=10.45,
                volume=100 * (i + 1), open_interest=1000, last_size=100,
                underlying_price=500.0, implied_vol=0.25, delta=0.52,
                gamma=0.008, theta=-0.12, vega=0.38,
            )
            trade = classifier.classify(tick)
            if trade:
                enriched = engine.enrich(trade)
                signal = await detector.detect(enriched)
                logger.info(
                    "[S1 tick {}] iv_source={} delta={:.3f} gamma={:.4f} moneyness={} dte={} signal={}",
                    i + 1, enriched.iv_source, enriched.delta or 0,
                    enriched.gamma or 0, enriched.moneyness.value,
                    enriched.days_to_expiry, "FLAGGED" if signal else "none",
                )

        # Scenario 2: No IBKR Greeks — BS fallback
        logger.info("--- Scenario 2: BS fallback (no IBKR Greeks) ---")
        classifier2 = FlowClassifier(settings)
        for i in range(3):
            tick2 = TickUpdate(
                symbol="AAPL", con_id=99002, expiry=future_expiry, strike=200.0, right="C",
                timestamp=base_time + timedelta(seconds=10, milliseconds=i * 400),
                bid=8.00, ask=8.50, last=8.40,
                volume=200 * (i + 1), open_interest=500, last_size=200,
                underlying_price=200.0,
                implied_vol=None, delta=None, gamma=None, theta=None, vega=None,
            )
            trade2 = classifier2.classify(tick2)
            if trade2:
                enriched2 = engine.enrich(trade2)
                logger.info(
                    "[S2 tick {}] iv_source={} iv={:.1%} delta={} gamma={} moneyness={}",
                    i + 1, enriched2.iv_source,
                    enriched2.implied_vol or 0,
                    f"{enriched2.delta:.3f}" if enriched2.delta is not None else "None",
                    f"{enriched2.gamma:.5f}" if enriched2.gamma is not None else "None",
                    enriched2.moneyness.value,
                )

        logger.success("Smoke test complete.")

    asyncio.run(main())
