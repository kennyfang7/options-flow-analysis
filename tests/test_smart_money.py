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
    s = Settings(
        min_premium=100.0,
        min_block_size=500,
        unusual_volume_multiplier=3.0,
        unusual_premium_threshold=250_000.0,
        otm_premium_threshold=100_000.0,
        near_expiry_days=7,
        smart_money_min_confidence=0.30,
        **setting_overrides,
    )
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
