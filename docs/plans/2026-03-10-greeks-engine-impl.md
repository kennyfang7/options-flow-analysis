# Greeks Engine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/analysis/greeks_engine.py` — a synchronous enrichment layer that attaches full Greeks (delta, gamma, theta, vega, IV) and context (days-to-expiry, moneyness) to every `ClassifiedTrade`, using IBKR's model Greeks when available and Black-Scholes as a fallback.

**Architecture:** `GreeksEngine.enrich(ClassifiedTrade) → EnrichedTrade`. `EnrichedTrade` is a pydantic subclass of `ClassifiedTrade` with four new fields (gamma, theta, vega, days_to_expiry, moneyness, iv_source). No IO, no DB changes. `UnusualDetector` accepts `EnrichedTrade` unchanged — Python subclass duck-typing handles it automatically.

**Tech Stack:** Python `math` stdlib only (no scipy). Pydantic v2 model inheritance. `pytest` for tests.

---

### Task 1: Add `risk_free_rate` to Settings

**Files:**
- Modify: `config/settings.py`
- Test: `tests/test_settings.py` (create if absent)

**Step 1: Write the failing test**

Create `tests/test_settings.py` if it doesn't exist, then add:

```python
def test_risk_free_rate_default():
    from config.settings import Settings
    s = Settings()
    assert s.risk_free_rate == 0.05

def test_risk_free_rate_override():
    from config.settings import Settings
    s = Settings(risk_free_rate=0.04)
    assert s.risk_free_rate == 0.04

def test_risk_free_rate_must_be_non_negative():
    import pytest
    from pydantic import ValidationError
    from config.settings import Settings
    with pytest.raises(ValidationError):
        Settings(risk_free_rate=-0.01)
```

**Step 2: Run to verify failure**

```
pytest tests/test_settings.py -v
```
Expected: FAIL — `Settings` has no `risk_free_rate`

**Step 3: Implement in `config/settings.py`**

In the `# Scanning Thresholds` block, add after `min_premium`:

```python
# Greeks Engine
risk_free_rate: float = Field(
    default=0.05,
    description="Annualized risk-free rate used for Black-Scholes fallback (e.g. 0.05 = 5%)",
    ge=0.0,
)
```

**Step 4: Run to verify pass**

```
pytest tests/test_settings.py -v
```
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add config/settings.py tests/test_settings.py
git commit -m "feat: add risk_free_rate setting for Greeks engine Black-Scholes fallback"
```

---

### Task 2: Black-Scholes Pure Math Helpers

**Files:**
- Create: `src/analysis/greeks_engine.py`
- Create: `tests/test_greeks_engine.py`

**Background:** Black-Scholes computes option prices and Greeks from five inputs: underlying price (S), strike (K), time to expiry in years (T), risk-free rate (r), and volatility (sigma). All helpers are module-level private functions — `_` prefix means "internal, do not import".

**Step 1: Write the failing tests**

Create `tests/test_greeks_engine.py`:

```python
from __future__ import annotations
import math
import pytest


# ---------------------------------------------------------------------------
# _norm_cdf
# ---------------------------------------------------------------------------

def test_norm_cdf_at_zero():
    from src.analysis.greeks_engine import _norm_cdf
    assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-6)

def test_norm_cdf_large_positive():
    from src.analysis.greeks_engine import _norm_cdf
    assert _norm_cdf(10.0) == pytest.approx(1.0, abs=1e-6)

def test_norm_cdf_large_negative():
    from src.analysis.greeks_engine import _norm_cdf
    assert _norm_cdf(-10.0) == pytest.approx(0.0, abs=1e-6)

def test_norm_cdf_known_value():
    from src.analysis.greeks_engine import _norm_cdf
    # N(1.96) ≈ 0.975 (well-known z-score)
    assert _norm_cdf(1.96) == pytest.approx(0.975, abs=0.001)


# ---------------------------------------------------------------------------
# _norm_pdf
# ---------------------------------------------------------------------------

def test_norm_pdf_at_zero():
    from src.analysis.greeks_engine import _norm_pdf
    expected = 1.0 / math.sqrt(2 * math.pi)
    assert _norm_pdf(0.0) == pytest.approx(expected, abs=1e-9)

def test_norm_pdf_symmetry():
    from src.analysis.greeks_engine import _norm_pdf
    assert _norm_pdf(1.0) == pytest.approx(_norm_pdf(-1.0), abs=1e-9)


# ---------------------------------------------------------------------------
# _bs_price
# ---------------------------------------------------------------------------

def test_bs_price_call_atm():
    """ATM call should have known approximate value."""
    from src.analysis.greeks_engine import _bs_price
    # S=100, K=100, T=1yr, r=0.05, sigma=0.20
    # Classic BS: C ≈ 10.45
    price = _bs_price(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20, right="C")
    assert price == pytest.approx(10.45, abs=0.05)

def test_bs_price_put_call_parity():
    """C - P = S - K*e^(-rT) (put-call parity)."""
    from src.analysis.greeks_engine import _bs_price
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
    call = _bs_price(S, K, T, r, sigma, "C")
    put = _bs_price(S, K, T, r, sigma, "P")
    parity = S - K * math.exp(-r * T)
    assert (call - put) == pytest.approx(parity, abs=1e-6)

def test_bs_price_deep_itm_call():
    """Deep ITM call price ≈ S - K*e^(-rT)."""
    from src.analysis.greeks_engine import _bs_price
    # S=200, K=100 — very deep ITM
    price = _bs_price(S=200.0, K=100.0, T=1.0, r=0.05, sigma=0.20, right="C")
    intrinsic = 200.0 - 100.0 * math.exp(-0.05)
    assert price == pytest.approx(intrinsic, abs=1.0)

def test_bs_price_deep_otm_call_is_small():
    """Deep OTM call should be nearly worthless."""
    from src.analysis.greeks_engine import _bs_price
    price = _bs_price(S=100.0, K=200.0, T=0.1, r=0.05, sigma=0.20, right="C")
    assert price < 0.01


# ---------------------------------------------------------------------------
# _bs_delta, _bs_gamma, _bs_theta, _bs_vega
# ---------------------------------------------------------------------------

def test_bs_delta_atm_call_near_half():
    """ATM call delta ≈ 0.5 for short T."""
    from src.analysis.greeks_engine import _bs_delta, _d1_d2
    T = 30 / 365
    d1, _ = _d1_d2(S=100.0, K=100.0, T=T, r=0.05, sigma=0.20)
    delta = _bs_delta(d1, "C")
    assert 0.50 < delta < 0.60  # slightly above 0.5 due to r*T drift

def test_bs_delta_call_plus_put_equals_one():
    """For same inputs: delta_call - delta_put = 1 (in absolute terms)."""
    from src.analysis.greeks_engine import _bs_delta, _d1_d2
    d1, _ = _d1_d2(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20)
    delta_call = _bs_delta(d1, "C")
    delta_put = _bs_delta(d1, "P")
    assert (delta_call - delta_put) == pytest.approx(1.0, abs=1e-9)

def test_bs_gamma_positive():
    from src.analysis.greeks_engine import _bs_gamma, _d1_d2
    d1, _ = _d1_d2(100.0, 100.0, 1.0, 0.05, 0.20)
    gamma = _bs_gamma(S=100.0, d1=d1, sigma=0.20, T=1.0)
    assert gamma > 0

def test_bs_theta_call_negative():
    """Theta is negative — options decay with time."""
    from src.analysis.greeks_engine import _bs_theta, _d1_d2
    d1, d2 = _d1_d2(100.0, 100.0, 1.0, 0.05, 0.20)
    theta = _bs_theta(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20, d1=d1, d2=d2, right="C")
    assert theta < 0

def test_bs_vega_positive():
    """Vega is always positive."""
    from src.analysis.greeks_engine import _bs_vega, _d1_d2
    d1, _ = _d1_d2(100.0, 100.0, 1.0, 0.05, 0.20)
    vega = _bs_vega(S=100.0, d1=d1, T=1.0)
    assert vega > 0


# ---------------------------------------------------------------------------
# _implied_vol
# ---------------------------------------------------------------------------

def test_implied_vol_recovers_input_sigma():
    """Given BS price with sigma=0.20, _implied_vol should return ~0.20."""
    from src.analysis.greeks_engine import _bs_price, _implied_vol
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20
    price = _bs_price(S, K, T, r, sigma, "C")
    recovered = _implied_vol(price=price, S=S, K=K, T=T, r=r, right="C")
    assert recovered == pytest.approx(0.20, abs=0.001)

def test_implied_vol_put_recovers_input_sigma():
    from src.analysis.greeks_engine import _bs_price, _implied_vol
    S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.05, 0.25
    price = _bs_price(S, K, T, r, sigma, "P")
    recovered = _implied_vol(price=price, S=S, K=K, T=T, r=r, right="P")
    assert recovered == pytest.approx(0.25, abs=0.001)

def test_implied_vol_returns_none_for_zero_price():
    """Zero price means no vol can be found."""
    from src.analysis.greeks_engine import _implied_vol
    result = _implied_vol(price=0.0, S=100.0, K=100.0, T=1.0, r=0.05, right="C")
    assert result is None
```

**Step 2: Run to verify failure**

```
pytest tests/test_greeks_engine.py -v
```
Expected: FAIL — `greeks_engine` module does not exist

**Step 3: Implement helpers in `src/analysis/greeks_engine.py`**

```python
from __future__ import annotations

import math
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
        ValueError: If T <= 0 or sigma <= 0.
        ZeroDivisionError: If S/K <= 0 (degenerate inputs).
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
```

**Step 4: Run tests to verify pass**

```
pytest tests/test_greeks_engine.py -v -k "norm or bs or implied"
```
Expected: all BS helper tests PASS

**Step 5: Commit**

```bash
git add src/analysis/greeks_engine.py tests/test_greeks_engine.py
git commit -m "feat: add Black-Scholes math helpers to greeks_engine"
```

---

### Task 3: Days-to-Expiry Helper + Moneyness Enum

**Files:**
- Modify: `src/analysis/greeks_engine.py`
- Modify: `tests/test_greeks_engine.py`

**Step 1: Write the failing tests**

Append to `tests/test_greeks_engine.py`:

```python
# ---------------------------------------------------------------------------
# _days_to_expiry
# ---------------------------------------------------------------------------

def test_days_to_expiry_future():
    from src.analysis.greeks_engine import _days_to_expiry
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=30)).strftime("%Y%m%d")
    assert _days_to_expiry(future) == 30

def test_days_to_expiry_today_is_zero():
    from src.analysis.greeks_engine import _days_to_expiry
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    assert _days_to_expiry(today) == 0

def test_days_to_expiry_past_is_zero():
    """Expired contracts return 0, not negative."""
    from src.analysis.greeks_engine import _days_to_expiry
    assert _days_to_expiry("20200101") == 0


# ---------------------------------------------------------------------------
# _classify_moneyness
# ---------------------------------------------------------------------------

def test_moneyness_call_itm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    # S > K for a call → ITM
    assert _classify_moneyness(underlying_price=510.0, strike=500.0, right="C") == Moneyness.ITM

def test_moneyness_call_otm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=490.0, strike=500.0, right="C") == Moneyness.OTM

def test_moneyness_call_atm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    # Within ±1%
    assert _classify_moneyness(underlying_price=501.0, strike=500.0, right="C") == Moneyness.ATM

def test_moneyness_put_itm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    # S < K for a put → ITM
    assert _classify_moneyness(underlying_price=490.0, strike=500.0, right="P") == Moneyness.ITM

def test_moneyness_put_otm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=510.0, strike=500.0, right="P") == Moneyness.OTM

def test_moneyness_unknown_when_no_underlying():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=None, strike=500.0, right="C") == Moneyness.UNKNOWN
```

**Step 2: Run to verify failure**

```
pytest tests/test_greeks_engine.py -v -k "days or moneyness"
```
Expected: FAIL

**Step 3: Add to `src/analysis/greeks_engine.py`** (after the BS helpers section, before any classes):

```python
# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

from datetime import date
from enum import Enum


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
```

**Step 4: Run to verify pass**

```
pytest tests/test_greeks_engine.py -v -k "days or moneyness"
```
Expected: PASS

**Step 5: Commit**

```bash
git add src/analysis/greeks_engine.py tests/test_greeks_engine.py
git commit -m "feat: add Moneyness enum, _days_to_expiry, _classify_moneyness helpers"
```

---

### Task 4: EnrichedTrade Pydantic Model

**Files:**
- Modify: `src/analysis/greeks_engine.py`
- Modify: `tests/test_greeks_engine.py`

**Background:** `EnrichedTrade` is a pydantic subclass of `ClassifiedTrade`. It inherits all existing fields (including `delta` and `implied_vol`) and adds the full Greek set plus context fields. Since it IS-A `ClassifiedTrade`, `UnusualDetector.detect()` accepts it without changes.

**Step 1: Write the failing tests**

Append to `tests/test_greeks_engine.py`:

```python
# ---------------------------------------------------------------------------
# EnrichedTrade model
# ---------------------------------------------------------------------------

from datetime import datetime, timezone


def _make_classified_trade(**overrides):
    """Helper: build a minimal ClassifiedTrade for testing."""
    from src.analysis.flow_classifier import ClassifiedTrade, TradeType, Aggressor
    from src.data.tick_stream import TickUpdate

    tick = TickUpdate(
        symbol="SPY", con_id=12345, expiry="20260620", strike=500.0, right="C",
        timestamp=datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc),
        bid=10.0, ask=10.50, last=10.25, volume=500, open_interest=1000,
        last_size=100, underlying_price=500.0,
        implied_vol=0.20, delta=0.52, gamma=0.01, theta=-0.05, vega=0.40,
    )
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20260620", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.20, delta=0.52,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.85, effective_price=10.25, last_size=100,
        premium=102_500.0, signal_strength=6.0, volume_delta=100,
        window_ticks=1,
        timestamp=datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc),
        tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


def test_enriched_trade_has_extra_greek_fields():
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import ClassifiedTrade, TradeType, Aggressor
    from src.data.tick_stream import TickUpdate

    trade = _make_classified_trade()
    tick = trade.tick

    enriched = EnrichedTrade(
        **trade.model_dump(),
        tick=tick,
        gamma=0.01,
        theta=-0.05,
        vega=0.40,
        days_to_expiry=102,
        moneyness=Moneyness.ATM,
        iv_source="ibkr",
    )

    assert enriched.gamma == pytest.approx(0.01)
    assert enriched.theta == pytest.approx(-0.05)
    assert enriched.vega == pytest.approx(0.40)
    assert enriched.days_to_expiry == 102
    assert enriched.moneyness == Moneyness.ATM
    assert enriched.iv_source == "ibkr"


def test_enriched_trade_is_classified_trade_subclass():
    from src.analysis.greeks_engine import EnrichedTrade
    from src.analysis.flow_classifier import ClassifiedTrade
    assert issubclass(EnrichedTrade, ClassifiedTrade)


def test_enriched_trade_tick_excluded_from_serialization():
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness

    trade = _make_classified_trade()
    enriched = EnrichedTrade(
        **trade.model_dump(), tick=trade.tick,
        gamma=0.01, theta=-0.05, vega=0.40,
        days_to_expiry=102, moneyness=Moneyness.ATM, iv_source="ibkr",
    )
    dumped = enriched.model_dump()
    assert "tick" not in dumped
    assert "gamma" in dumped
    assert "moneyness" in dumped
```

**Step 2: Run to verify failure**

```
pytest tests/test_greeks_engine.py -v -k "enriched_trade"
```
Expected: FAIL — `EnrichedTrade` not defined

**Step 3: Add `EnrichedTrade` to `src/analysis/greeks_engine.py`** (after the helper functions, before the GreeksEngine class):

```python
from pydantic import BaseModel, Field


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
```

**Step 4: Run to verify pass**

```
pytest tests/test_greeks_engine.py -v -k "enriched_trade"
```
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/analysis/greeks_engine.py tests/test_greeks_engine.py
git commit -m "feat: add EnrichedTrade pydantic model as ClassifiedTrade subclass"
```

---

### Task 5: GreeksEngine Class + enrich()

**Files:**
- Modify: `src/analysis/greeks_engine.py`
- Modify: `tests/test_greeks_engine.py`

**Background:** `enrich()` is synchronous. It follows a two-pass strategy:
1. **IBKR pass**: read Greeks directly off `trade.tick` (gamma, theta, vega) and `trade` (delta, implied_vol). These come from IBKR's `modelGreeks` field.
2. **Black-Scholes fallback**: if IV is still None, compute it from `effective_price`. Then compute any remaining None Greeks using d1/d2.

If `underlying_price` is None or `T=0`, skip BS entirely — not enough inputs.

**Step 1: Write the failing tests**

Append to `tests/test_greeks_engine.py`:

```python
# ---------------------------------------------------------------------------
# GreeksEngine.enrich()
# ---------------------------------------------------------------------------

def test_enrich_uses_ibkr_greeks_when_available():
    """When IBKR provides all Greeks, use them directly (no BS)."""
    from src.analysis.greeks_engine import GreeksEngine
    from config.settings import Settings

    engine = GreeksEngine(Settings(min_premium=100.0))
    trade = _make_classified_trade()  # tick has delta=0.52, gamma=0.01, theta=-0.05, vega=0.40, iv=0.20

    enriched = engine.enrich(trade)

    assert enriched.delta == pytest.approx(0.52)
    assert enriched.gamma == pytest.approx(0.01)
    assert enriched.theta == pytest.approx(-0.05)
    assert enriched.vega == pytest.approx(0.40)
    assert enriched.implied_vol == pytest.approx(0.20)
    assert enriched.iv_source == "ibkr"


def test_enrich_computes_iv_via_bs_when_ibkr_iv_missing():
    """When IBKR IV is None but price and underlying are present, compute via BS."""
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate
    from config.settings import Settings
    from datetime import date, timedelta

    future_expiry = (date.today() + timedelta(days=365)).strftime("%Y%m%d")

    tick = TickUpdate(
        symbol="SPY", con_id=99999, expiry=future_expiry, strike=100.0, right="C",
        timestamp=datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc),
        bid=10.0, ask=11.0, last=10.45, volume=100, last_size=50,
        underlying_price=100.0, implied_vol=None, delta=None,
        gamma=None, theta=None, vega=None,
    )
    trade = _make_classified_trade(
        con_id=99999, expiry=future_expiry, strike=100.0,
        underlying_price=100.0, implied_vol=None, delta=None,
        effective_price=10.45, premium=52_250.0, tick=tick,
    )

    engine = GreeksEngine(Settings(min_premium=100.0, risk_free_rate=0.05))
    enriched = engine.enrich(trade)

    assert enriched.iv_source == "black_scholes"
    assert enriched.implied_vol is not None
    assert 0.05 < enriched.implied_vol < 1.0
    assert enriched.delta is not None
    assert enriched.gamma is not None


def test_enrich_iv_source_unavailable_when_no_underlying():
    """When underlying_price is None, BS can't run → iv_source='unavailable'."""
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate
    from config.settings import Settings
    from datetime import date, timedelta

    future_expiry = (date.today() + timedelta(days=30)).strftime("%Y%m%d")
    tick = TickUpdate(
        symbol="SPY", con_id=11111, expiry=future_expiry, strike=500.0, right="C",
        timestamp=datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc),
        bid=None, ask=None, last=None, volume=None, last_size=50,
        underlying_price=None, implied_vol=None, delta=None,
        gamma=None, theta=None, vega=None,
    )
    trade = _make_classified_trade(
        con_id=11111, expiry=future_expiry, underlying_price=None,
        implied_vol=None, delta=None, effective_price=5.0,
        premium=25_000.0, tick=tick,
    )

    engine = GreeksEngine(Settings(min_premium=100.0))
    enriched = engine.enrich(trade)

    assert enriched.iv_source == "unavailable"
    assert enriched.implied_vol is None
    assert enriched.delta is None


def test_enrich_moneyness_atm():
    from src.analysis.greeks_engine import GreeksEngine, Moneyness
    from config.settings import Settings

    engine = GreeksEngine(Settings(min_premium=100.0))
    trade = _make_classified_trade(underlying_price=500.0, strike=500.0, right="C")
    enriched = engine.enrich(trade)
    assert enriched.moneyness == Moneyness.ATM


def test_enrich_moneyness_otm_call():
    from src.analysis.greeks_engine import GreeksEngine, Moneyness
    from config.settings import Settings

    engine = GreeksEngine(Settings(min_premium=100.0))
    trade = _make_classified_trade(underlying_price=480.0, strike=500.0, right="C")
    enriched = engine.enrich(trade)
    assert enriched.moneyness == Moneyness.OTM


def test_enrich_days_to_expiry_positive():
    from src.analysis.greeks_engine import GreeksEngine
    from config.settings import Settings
    from datetime import date, timedelta

    future_expiry = (date.today() + timedelta(days=45)).strftime("%Y%m%d")
    tick = _make_classified_trade().tick
    trade = _make_classified_trade(expiry=future_expiry, tick=tick)

    engine = GreeksEngine(Settings(min_premium=100.0))
    enriched = engine.enrich(trade)
    assert enriched.days_to_expiry == 45


def test_enrich_returns_enriched_trade_subclass():
    from src.analysis.greeks_engine import GreeksEngine, EnrichedTrade
    from src.analysis.flow_classifier import ClassifiedTrade
    from config.settings import Settings

    engine = GreeksEngine(Settings(min_premium=100.0))
    enriched = engine.enrich(_make_classified_trade())
    assert isinstance(enriched, EnrichedTrade)
    assert isinstance(enriched, ClassifiedTrade)


def test_enrich_partial_ibkr_greeks_fills_remainder_via_bs():
    """If IBKR gives IV but not gamma/theta/vega, compute the missing ones via BS."""
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate
    from config.settings import Settings
    from datetime import date, timedelta

    future_expiry = (date.today() + timedelta(days=90)).strftime("%Y%m%d")
    tick = TickUpdate(
        symbol="SPY", con_id=22222, expiry=future_expiry, strike=500.0, right="C",
        timestamp=datetime(2026, 3, 10, 14, 30, tzinfo=timezone.utc),
        bid=10.0, ask=11.0, last=10.50, volume=100, last_size=50,
        underlying_price=500.0,
        implied_vol=0.25, delta=0.52, gamma=None, theta=None, vega=None,
    )
    trade = _make_classified_trade(
        con_id=22222, expiry=future_expiry, implied_vol=0.25, delta=0.52,
        effective_price=10.50, premium=52_500.0, tick=tick,
    )

    engine = GreeksEngine(Settings(min_premium=100.0))
    enriched = engine.enrich(trade)

    assert enriched.iv_source == "ibkr"
    assert enriched.gamma is not None
    assert enriched.theta is not None
    assert enriched.vega is not None
```

**Step 2: Run to verify failure**

```
pytest tests/test_greeks_engine.py -v -k "enrich"
```
Expected: FAIL — `GreeksEngine` not defined

**Step 3: Add `GreeksEngine` to `src/analysis/greeks_engine.py`**

```python
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
        # model_dump() excludes 'tick' (Field(exclude=True) on ClassifiedTrade).
        # Override delta and implied_vol with enriched values.
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
```

**Step 4: Run the full test file**

```
pytest tests/test_greeks_engine.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/analysis/greeks_engine.py tests/test_greeks_engine.py
git commit -m "feat: implement GreeksEngine.enrich() with IBKR-first + Black-Scholes fallback"
```

---

### Task 6: Smoke Test Block + purge_stale()

**Files:**
- Modify: `src/analysis/greeks_engine.py`

**Background:** Following the project pattern, each module has a `if __name__ == "__main__"` block that runs a standalone smoke test without IBKR. Also add `purge_stale()` — even though GreeksEngine is currently stateless, having the hook keeps the orchestration layer interface consistent across all analysis modules.

**Step 1: Append to `src/analysis/greeks_engine.py`**

```python
    def purge_stale(self, max_age_seconds: float = 3600.0) -> int:
        """No-op for now — GreeksEngine is stateless.

        Included for interface consistency with FlowClassifier and UnusualDetector,
        which both expose purge_stale() for the orchestration layer to call hourly.

        Returns:
            Always 0.
        """
        return 0
```

Then add the `__main__` block at the end of the file:

```python
if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    from datetime import date

    from config.settings import Settings
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

        future_expiry = (date.today() + timedelta(days=90)).strftime("%Y%m%d")
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

    import asyncio
    asyncio.run(main())
```

**Step 2: Run the smoke test**

```
python src/analysis/greeks_engine.py
```
Expected: Two scenarios log cleanly with enriched Greeks. No exceptions.

**Step 3: Run the full test suite to verify no regressions**

```
pytest --tb=short -q
```
Expected: All tests pass (count should be ≥ 146 + new greeks tests)

**Step 4: Commit**

```bash
git add src/analysis/greeks_engine.py
git commit -m "feat: add GreeksEngine.purge_stale() and smoke test block"
```

---

### Task 7: Final Integration Check

**Step 1: Verify the full test count**

```
pytest --tb=short -q
```
Expected: ≥ 175 tests passing (146 prior + ~30 new greeks tests), 0 failures.

**Step 2: Confirm module imports cleanly**

```python
python -c "from src.analysis.greeks_engine import GreeksEngine, EnrichedTrade, Moneyness; print('OK')"
```
Expected: `OK`

**Step 3: Commit memory update**

Update `C:\Users\kenny\.claude\projects\C--Coding-Projects-options-flow-analysis\memory\MEMORY.md` — Step 8 complete entry.

**Step 4: Final commit if anything outstanding**

```bash
git status
```
Commit any remaining unstaged changes.
