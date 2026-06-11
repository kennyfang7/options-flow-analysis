from __future__ import annotations
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (expanded in Task 2)
# ---------------------------------------------------------------------------

def _make_trade(
    trade_type_str="block",
    aggressor_str="buy",
    moneyness_str="otm",
    days_to_expiry=90,
    premium=10_000.0,
    volume_delta=100,
    delta=0.25,
    implied_vol=0.30,
    underlying_price=500.0,
    symbol="SPY",
    right="C",
):
    """Build a minimal EnrichedTrade for SmartMoneyDetector tests."""
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.data.tick_stream import TickUpdate

    trade_type_map = {
        "sweep": TradeType.SWEEP, "split": TradeType.SPLIT,
        "block": TradeType.BLOCK, "unknown": TradeType.UNKNOWN,
    }
    aggressor_map = {
        "buy": Aggressor.BUY, "sell": Aggressor.SELL, "neutral": Aggressor.NEUTRAL,
    }
    moneyness_map = {
        "itm": Moneyness.ITM, "atm": Moneyness.ATM,
        "otm": Moneyness.OTM, "unknown": Moneyness.UNKNOWN,
    }

    ts = datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc)
    tick = TickUpdate(
        symbol=symbol, con_id=99001, expiry="20260620", strike=520.0, right=right,
        timestamp=ts, bid=1.0, ask=1.5, last=1.45,
        volume=volume_delta, open_interest=1000, last_size=volume_delta,
        underlying_price=underlying_price, implied_vol=implied_vol, delta=delta,
    )
    return EnrichedTrade(
        symbol=symbol,
        con_id=99001,
        expiry="20260620",
        right=right,
        strike=520.0,
        underlying_price=underlying_price,
        implied_vol=implied_vol,
        delta=delta,
        trade_type=trade_type_map[trade_type_str],
        aggressor=aggressor_map[aggressor_str],
        spread_position=0.8 if aggressor_str == "buy" else 0.2,
        effective_price=premium / max(volume_delta * 100, 1) if premium is not None else None,
        last_size=volume_delta,
        premium=premium,
        signal_strength=3.0,
        volume_delta=volume_delta,
        window_ticks=3 if trade_type_str == "sweep" else 1,
        timestamp=ts,
        tick=tick,
        gamma=0.005,
        theta=None,
        vega=None,
        days_to_expiry=days_to_expiry,
        moneyness=moneyness_map[moneyness_str],
        iv_source="ibkr",
    )


def _make_detector(**setting_overrides):
    from src.analysis.smart_money import SmartMoneyDetector
    from config.settings import Settings
    base = dict(
        min_premium=100.0,
        min_block_size=500,
        unusual_volume_multiplier=3.0,
        unusual_premium_threshold=250_000.0,
        otm_premium_threshold=100_000.0,
        near_expiry_days=7,
        smart_money_min_confidence=0.30,
    )
    base.update(setting_overrides)
    s = Settings(**base)
    return SmartMoneyDetector(s)


# ---------------------------------------------------------------------------
# Task 1: Model construction tests
# ---------------------------------------------------------------------------

def test_smart_money_signal_construction():
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.analysis.greeks_engine import Moneyness

    trade = _make_trade()
    sig = SmartMoneySignal(
        symbol="SPY",
        con_id=99001,
        expiry="20260620",
        right="C",
        strike=520.0,
        trade_type=TradeType.BLOCK,
        aggressor=Aggressor.BUY,
        premium=300_000.0,
        volume_delta=100,
        delta=0.25,
        days_to_expiry=5,
        moneyness=Moneyness.OTM,
        implied_vol=0.30,
        iv_source="ibkr",
        underlying_price=500.0,
        reasons=[SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.BIG_OTM_BET,
        confidence=0.45,
        detected_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade=trade,
    )
    assert sig.symbol == "SPY"
    assert sig.confidence == pytest.approx(0.45)
    assert sig.top_reason.value == "big_otm_bet"


def test_smart_money_signal_model_dump_excludes_trade():
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    from src.analysis.greeks_engine import Moneyness

    trade = _make_trade()
    sig = SmartMoneySignal(
        symbol="SPY", con_id=99001, expiry="20260620", right="C", strike=520.0,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        premium=300_000.0, volume_delta=100, delta=0.25,
        days_to_expiry=5, moneyness=Moneyness.OTM, implied_vol=0.30,
        iv_source="ibkr", underlying_price=500.0,
        reasons=[SmartMoneyReason.BIG_OTM_BET],
        top_reason=SmartMoneyReason.BIG_OTM_BET,
        confidence=0.45,
        detected_at=datetime(2026, 3, 11, 14, 30, tzinfo=timezone.utc),
        trade=trade,
    )
    data = sig.model_dump()
    assert "trade" not in data
    assert "symbol" in data
    assert "confidence" in data


def test_smart_money_reason_enum_values():
    from src.analysis.smart_money import SmartMoneyReason
    assert SmartMoneyReason.SWEEP_AGGRESSOR.value == "sweep_aggressor"
    assert SmartMoneyReason.BIG_OTM_BET.value == "big_otm_bet"
    assert SmartMoneyReason.NEAR_EXPIRY_OTM.value == "near_expiry_otm"
    assert SmartMoneyReason.UNUSUAL_VOLUME.value == "unusual_volume"
    assert SmartMoneyReason.LARGE_BLOCK.value == "large_block"


# ---------------------------------------------------------------------------
# Task 2: SmartMoneyDetector — individual checks
# ---------------------------------------------------------------------------

# --- SWEEP_AGGRESSOR ---

def test_sweep_aggressor_fires_on_sweep_buy():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.SWEEP_AGGRESSOR in sig.reasons


def test_sweep_aggressor_fires_on_sweep_sell():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="sell")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.SWEEP_AGGRESSOR in sig.reasons


def test_sweep_aggressor_skips_neutral_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="neutral")
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.SWEEP_AGGRESSOR not in sig.reasons


def test_sweep_aggressor_skips_non_sweep_trade_type():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="block", aggressor_str="buy")
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.SWEEP_AGGRESSOR not in sig.reasons


# --- BIG_OTM_BET ---

def test_big_otm_bet_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.BIG_OTM_BET in sig.reasons


def test_big_otm_bet_skips_itm():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="itm", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_sell_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="sell",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_small_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="otm", aggressor_str="buy",
        premium=10_000.0, volume_delta=100,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_unknown_moneyness():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        moneyness_str="unknown", aggressor_str="buy",
        premium=150_000.0, volume_delta=1000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


def test_big_otm_bet_skips_none_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(moneyness_str="otm", aggressor_str="buy", premium=None, volume_delta=100)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.BIG_OTM_BET not in sig.reasons


# --- NEAR_EXPIRY_OTM ---

def test_near_expiry_otm_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.NEAR_EXPIRY_OTM in sig.reasons


def test_near_expiry_otm_fires_at_boundary():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=7, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.NEAR_EXPIRY_OTM in sig.reasons


def test_near_expiry_otm_skips_over_threshold():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=8, moneyness_str="otm", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_itm():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="itm", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_unknown_moneyness():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="unknown", aggressor_str="buy",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


def test_near_expiry_otm_skips_sell_aggressor():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        days_to_expiry=5, moneyness_str="otm", aggressor_str="sell",
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.NEAR_EXPIRY_OTM not in sig.reasons


# --- UNUSUAL_VOLUME ---

def test_unusual_volume_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    # unusual_volume_multiplier=3.0, min_block_size=500 → threshold=1500
    trade = _make_trade(volume_delta=1500, aggressor_str="neutral")
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.UNUSUAL_VOLUME in sig.reasons


def test_unusual_volume_skips_below_threshold():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(volume_delta=1499, aggressor_str="neutral")
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.UNUSUAL_VOLUME not in sig.reasons


# --- LARGE_BLOCK ---

def test_large_block_fires():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        trade_type_str="block", premium=300_000.0, volume_delta=2000,
    )
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.LARGE_BLOCK in sig.reasons


def test_large_block_skips_sweep_type():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(
        trade_type_str="sweep", aggressor_str="neutral",
        premium=300_000.0, volume_delta=2000,
    )
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


def test_large_block_skips_small_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="block", premium=10_000.0, volume_delta=100)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


def test_large_block_skips_none_premium():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="block", premium=None, volume_delta=2000)
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.LARGE_BLOCK not in sig.reasons


# --- Returns None when no reasons fire ---

def test_no_reasons_returns_none():
    det = _make_detector()
    trade = _make_trade(
        trade_type_str="block",
        aggressor_str="buy",
        moneyness_str="otm",
        days_to_expiry=90,
        premium=10_000.0,
        volume_delta=100,
    )
    sig = det.score(trade)
    assert sig is None


# --- Confidence calculation ---

def test_confidence_single_reason_sweep_aggressor():
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy")
    sig = det.score(trade)
    assert sig is not None
    # SWEEP_AGGRESSOR weight = 0.40
    assert sig.confidence == pytest.approx(0.40)


def test_confidence_capped_at_one():
    det = _make_detector()
    # SWEEP_AGGRESSOR(0.40)+BIG_OTM_BET(0.45)+NEAR_EXPIRY_OTM(0.35)+UNUSUAL_VOLUME(0.35)=1.55→capped 1.0
    trade = _make_trade(
        trade_type_str="sweep", aggressor_str="buy", moneyness_str="otm",
        days_to_expiry=5, premium=150_000.0, volume_delta=1500,
    )
    sig = det.score(trade)
    assert sig is not None
    assert sig.confidence == pytest.approx(1.0)


def test_confidence_below_min_returns_none():
    # volume_delta=200 keeps UNUSUAL_VOLUME from firing (200 < 1500 threshold)
    # Only LARGE_BLOCK fires: confidence=0.30 < min_confidence=0.45 → None
    det = _make_detector(smart_money_min_confidence=0.45)
    trade = _make_trade(
        trade_type_str="block", aggressor_str="neutral",
        premium=300_000.0, volume_delta=200,
    )
    sig = det.score(trade)
    assert sig is None


# --- top_reason priority ---

def test_top_reason_sweep_beats_unusual_volume():
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector()
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy", volume_delta=1500)
    sig = det.score(trade)
    assert sig is not None
    assert sig.top_reason == SmartMoneyReason.SWEEP_AGGRESSOR


# --- purge_stale ---

def test_purge_stale_always_returns_zero():
    det = _make_detector()
    assert det.purge_stale() == 0
    assert det.purge_stale(max_age_seconds=60.0) == 0


# ---------------------------------------------------------------------------
# PRE_EARNINGS reason tests
# ---------------------------------------------------------------------------

def test_pre_earnings_fires_within_window():
    """days_to_earnings=3 (< default pre_earnings_days=5) → PRE_EARNINGS fires."""
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector(pre_earnings_days=5)
    trade = _make_trade()
    trade = trade.model_copy(update={"days_to_earnings": 3})
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.PRE_EARNINGS in sig.reasons


def test_pre_earnings_fires_on_earnings_day():
    """days_to_earnings=0 (earnings today) → PRE_EARNINGS fires."""
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector(pre_earnings_days=5)
    trade = _make_trade()
    trade = trade.model_copy(update={"days_to_earnings": 0})
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.PRE_EARNINGS in sig.reasons
    assert sig.days_to_earnings == 0


def test_pre_earnings_none_does_not_fire():
    """days_to_earnings=None → PRE_EARNINGS does NOT fire."""
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector(pre_earnings_days=5)
    # _make_trade defaults to days_to_earnings=None (not set)
    trade = _make_trade()
    assert trade.days_to_earnings is None
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.PRE_EARNINGS not in sig.reasons


def test_pre_earnings_beyond_window_does_not_fire():
    """days_to_earnings=10 with pre_earnings_days=5 → does NOT fire."""
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector(pre_earnings_days=5)
    trade = _make_trade()
    trade = trade.model_copy(update={"days_to_earnings": 10})
    sig = det.score(trade)
    if sig is not None:
        assert SmartMoneyReason.PRE_EARNINGS not in sig.reasons


def test_pre_earnings_sweep_aggressor_wins_priority():
    """SWEEP_AGGRESSOR has higher priority than PRE_EARNINGS → top_reason=SWEEP_AGGRESSOR."""
    from src.analysis.smart_money import SmartMoneyReason
    det = _make_detector(pre_earnings_days=5)
    # Sweep trade with earnings in 2 days — both reasons should fire
    trade = _make_trade(trade_type_str="sweep", aggressor_str="buy")
    trade = trade.model_copy(update={"days_to_earnings": 2})
    sig = det.score(trade)
    assert sig is not None
    assert SmartMoneyReason.SWEEP_AGGRESSOR in sig.reasons
    assert SmartMoneyReason.PRE_EARNINGS in sig.reasons
    assert sig.top_reason == SmartMoneyReason.SWEEP_AGGRESSOR


def test_pre_earnings_signal_carries_days_to_earnings():
    """SmartMoneySignal.days_to_earnings is populated from trade."""
    det = _make_detector(pre_earnings_days=5)
    trade = _make_trade()
    trade = trade.model_copy(update={"days_to_earnings": 4})
    sig = det.score(trade)
    assert sig is not None
    assert sig.days_to_earnings == 4
