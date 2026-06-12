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
    assert 0.50 < delta < 0.60

def test_bs_delta_call_plus_put_equals_one():
    """delta_call - delta_put = 1."""
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
    assert _classify_moneyness(underlying_price=510.0, strike=500.0, right="C") == Moneyness.ITM

def test_moneyness_call_otm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=490.0, strike=500.0, right="C") == Moneyness.OTM

def test_moneyness_call_atm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=501.0, strike=500.0, right="C") == Moneyness.ATM

def test_moneyness_put_itm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=490.0, strike=500.0, right="P") == Moneyness.ITM

def test_moneyness_put_otm():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=510.0, strike=500.0, right="P") == Moneyness.OTM

def test_moneyness_unknown_when_no_underlying():
    from src.analysis.greeks_engine import _classify_moneyness, Moneyness
    assert _classify_moneyness(underlying_price=None, strike=500.0, right="C") == Moneyness.UNKNOWN


# ---------------------------------------------------------------------------
# EnrichedTrade model
# ---------------------------------------------------------------------------

from datetime import datetime, timezone


def _make_classified_trade(**overrides):
    """Helper: build a minimal ClassifiedTrade for testing."""
    from src.analysis.flow_classifier import ClassifiedTrade, TradeType, Aggressor
    from conftest import make_tick

    tick = make_tick(
        expiry="20260620", bid=10.0, ask=10.50, last=10.25,
        volume=500, last_size=100, implied_vol=0.20, delta=0.52,
        gamma=0.01, theta=-0.05, vega=0.40,
    )
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20260620", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.20, delta=0.52,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.85, effective_price=10.25, last_size=100,
        premium=102_500.0, signal_strength=6.0, volume_delta=100,
        window_ticks=1,
        timestamp=tick.timestamp,
        tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


def test_enriched_trade_has_extra_greek_fields():
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness

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
        timestamp=datetime.now(timezone.utc),
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
        timestamp=datetime.now(timezone.utc),
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
    """If IBKR gives IV+delta but not gamma/theta/vega, compute missing ones via BS."""
    from src.analysis.greeks_engine import GreeksEngine
    from src.data.tick_stream import TickUpdate
    from config.settings import Settings
    from datetime import date, timedelta

    future_expiry = (date.today() + timedelta(days=90)).strftime("%Y%m%d")
    tick = TickUpdate(
        symbol="SPY", con_id=22222, expiry=future_expiry, strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
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
