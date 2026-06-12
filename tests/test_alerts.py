from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests as req_lib


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_tick():
    from src.data.tick_stream import TickUpdate
    return TickUpdate(
        symbol="SPY", con_id=12345, expiry="20261219", strike=500.0, right="C",
        timestamp=datetime.now(timezone.utc),
        bid=2.0, ask=2.5, last=2.45,
        volume=100, open_interest=1000, last_size=50,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
    )


def _make_classified_trade(tick=None, **overrides):
    from src.analysis.flow_classifier import ClassifiedTrade, TradeType, Aggressor
    tick = tick or _make_tick()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        spread_position=0.8, effective_price=2.45, last_size=500,
        premium=300_000.0, signal_strength=8.0,
        volume_delta=500, window_ticks=1, timestamp=ts, tick=tick,
    )
    defaults.update(overrides)
    return ClassifiedTrade(**defaults)


def _make_unusual_signal(**overrides):
    from src.analysis.unusual_detector import UnusualSignal, UnusualReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    trade = _make_classified_trade()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        trade_type=TradeType.BLOCK, aggressor=Aggressor.BUY,
        premium=300_000.0, volume_delta=500, signal_strength=8.0,
        delta=0.45, underlying_price=500.0, implied_vol=0.25,
        effective_price=2.45,
        reasons=[UnusualReason.PREMIUM_SIZE],
        top_reason=UnusualReason.PREMIUM_SIZE,
        flagged_at=ts,
        trade=trade,
    )
    defaults.update(overrides)
    return UnusualSignal(**defaults)


def _make_enriched_trade(**overrides):
    from src.analysis.greeks_engine import EnrichedTrade, Moneyness
    from src.analysis.flow_classifier import TradeType, Aggressor
    tick = _make_tick()
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        underlying_price=500.0, implied_vol=0.25, delta=0.45,
        trade_type=TradeType.SWEEP, aggressor=Aggressor.BUY,
        spread_position=0.8, effective_price=2.45, last_size=1500,
        premium=367_500.0, signal_strength=6.0,
        volume_delta=1500, window_ticks=3, timestamp=ts, tick=tick,
        gamma=0.01, theta=-0.05, vega=0.15,
        days_to_expiry=90, moneyness=Moneyness.OTM, iv_source="ibkr",
    )
    defaults.update(overrides)
    return EnrichedTrade(**defaults)


def _make_smart_money_signal(**overrides):
    from src.analysis.smart_money import SmartMoneySignal, SmartMoneyReason
    from src.analysis.flow_classifier import TradeType, Aggressor
    etrade = _make_enriched_trade(symbol=overrides.get("symbol", "SPY"))
    ts = datetime.now(timezone.utc)
    defaults = dict(
        symbol="SPY", con_id=12345, expiry="20261219", right="C", strike=500.0,
        trade_type=TradeType.SWEEP, aggressor=Aggressor.BUY,
        premium=367_500.0, volume_delta=1500,
        delta=0.45, days_to_expiry=90,
        moneyness=etrade.moneyness,
        implied_vol=0.25, iv_source="ibkr", underlying_price=500.0,
        reasons=[SmartMoneyReason.SWEEP_AGGRESSOR, SmartMoneyReason.UNUSUAL_VOLUME],
        top_reason=SmartMoneyReason.SWEEP_AGGRESSOR,
        confidence=0.75,
        detected_at=ts,
        trade=etrade,
    )
    defaults.update(overrides)
    return SmartMoneySignal(**defaults)


def _make_rules(**setting_overrides):
    from src.alerts.rules import AlertRules
    from config.settings import Settings
    base = dict(min_premium=100.0, unusual_premium_threshold=250_000.0)
    base.update(setting_overrides)
    return AlertRules(Settings(**base))


def _make_notifier(discord_webhook_url="", alert_email=""):
    from src.alerts.notifier import Notifier
    from config.settings import Settings
    return Notifier(Settings(
        min_premium=100.0,
        unusual_premium_threshold=250_000.0,
        discord_webhook_url=discord_webhook_url,
        alert_email=alert_email,
    ))


# ---------------------------------------------------------------------------
# AlertLevel
# ---------------------------------------------------------------------------

def test_alert_level_values():
    from src.alerts.rules import AlertLevel
    assert AlertLevel.LOW.value == "low"
    assert AlertLevel.MEDIUM.value == "medium"
    assert AlertLevel.HIGH.value == "high"


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

def test_alert_construction():
    from src.alerts.rules import Alert, AlertLevel
    alert = Alert(
        symbol="SPY",
        level=AlertLevel.HIGH,
        title="SPY UNUSUAL HIGH",
        body="BLOCK BUY | 500 contracts | $300,000",
        source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY", "premium": 300_000.0},
    )
    assert alert.symbol == "SPY"
    assert alert.level == AlertLevel.HIGH
    assert alert.source == "unusual"


def test_alert_metadata_is_json_serializable():
    from src.alerts.rules import Alert, AlertLevel
    alert = Alert(
        symbol="SPY", level=AlertLevel.MEDIUM,
        title="T", body="B", source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY", "premium": 300_000.0, "top_reason": "premium_size"},
    )
    # Must not raise
    serialized = json.dumps(alert.metadata)
    data = json.loads(serialized)
    assert data["symbol"] == "SPY"


# ---------------------------------------------------------------------------
# AlertRules.evaluate_unusual — level determination
# ---------------------------------------------------------------------------

def test_evaluate_unusual_premium_size_is_high():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.PREMIUM_SIZE,
        reasons=[UnusualReason.PREMIUM_SIZE],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_unusual_oi_ratio_is_medium():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.OI_RATIO,
        reasons=[UnusualReason.OI_RATIO],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_unusual_otm_premium_is_medium():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.OTM_PREMIUM,
        reasons=[UnusualReason.OTM_PREMIUM],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_unusual_signal_strength_is_low():
    from src.alerts.rules import AlertLevel
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        top_reason=UnusualReason.SIGNAL_STRENGTH,
        reasons=[UnusualReason.SIGNAL_STRENGTH],
    )
    alert = rules.evaluate_unusual(sig)
    assert alert.level == AlertLevel.LOW


# ---------------------------------------------------------------------------
# AlertRules.evaluate_unusual — title / body / source
# ---------------------------------------------------------------------------

def test_evaluate_unusual_title_contains_symbol_and_level():
    from src.analysis.unusual_detector import UnusualReason
    rules = _make_rules()
    sig = _make_unusual_signal(
        symbol="AAPL",
        top_reason=UnusualReason.PREMIUM_SIZE,
        reasons=[UnusualReason.PREMIUM_SIZE],
    )
    alert = rules.evaluate_unusual(sig)
    assert "AAPL" in alert.title
    assert "HIGH" in alert.title


def test_evaluate_unusual_body_contains_premium():
    rules = _make_rules()
    sig = _make_unusual_signal(premium=300_000.0)
    alert = rules.evaluate_unusual(sig)
    assert "300,000" in alert.body or "300000" in alert.body


def test_evaluate_unusual_source_is_unusual():
    rules = _make_rules()
    sig = _make_unusual_signal()
    alert = rules.evaluate_unusual(sig)
    assert alert.source == "unusual"


def test_evaluate_unusual_metadata_has_expected_keys():
    rules = _make_rules()
    sig = _make_unusual_signal()
    alert = rules.evaluate_unusual(sig)
    for key in ("symbol", "trade_type", "aggressor", "premium", "volume_delta", "top_reason"):
        assert key in alert.metadata


# ---------------------------------------------------------------------------
# AlertRules.evaluate_smart_money — level determination
# ---------------------------------------------------------------------------

def test_evaluate_smart_money_high_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_smart_money_medium_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.55)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.MEDIUM


def test_evaluate_smart_money_low_confidence():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.35)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.LOW


def test_evaluate_smart_money_boundary_070_is_high():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.70)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.HIGH


def test_evaluate_smart_money_boundary_050_is_medium():
    from src.alerts.rules import AlertLevel
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.50)
    alert = rules.evaluate_smart_money(sig)
    assert alert.level == AlertLevel.MEDIUM


# ---------------------------------------------------------------------------
# AlertRules.evaluate_smart_money — title / body / source
# ---------------------------------------------------------------------------

def test_evaluate_smart_money_title_contains_symbol_and_confidence():
    rules = _make_rules()
    sig = _make_smart_money_signal(symbol="TSLA", confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert "TSLA" in alert.title
    assert "75%" in alert.title or "0.75" in alert.title


def test_evaluate_smart_money_body_contains_top_reason():
    from src.analysis.smart_money import SmartMoneyReason
    rules = _make_rules()
    sig = _make_smart_money_signal(
        top_reason=SmartMoneyReason.SWEEP_AGGRESSOR,
    )
    alert = rules.evaluate_smart_money(sig)
    assert "sweep_aggressor" in alert.body


def test_evaluate_smart_money_source_is_smart_money():
    rules = _make_rules()
    sig = _make_smart_money_signal()
    alert = rules.evaluate_smart_money(sig)
    assert alert.source == "smart_money"


def test_evaluate_smart_money_metadata_has_confidence():
    rules = _make_rules()
    sig = _make_smart_money_signal(confidence=0.75)
    alert = rules.evaluate_smart_money(sig)
    assert "confidence" in alert.metadata
    assert alert.metadata["confidence"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Notifier — discord skips when url empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_skips_discord_when_url_empty():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        notifier = _make_notifier(discord_webhook_url="")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Notifier — discord posts when url is set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_posts_to_discord_when_url_set():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="SPY UNUSUAL HIGH", body="details",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_notifier_discord_payload_has_embeds():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="SPY UNUSUAL HIGH", body="details",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["title"] == "SPY UNUSUAL HIGH"


@pytest.mark.asyncio
async def test_notifier_discord_high_level_uses_red_color():
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        _, kwargs = mock_post.call_args
        embed = kwargs["json"]["embeds"][0]
        assert embed["color"] == 0xFF0000  # red for HIGH


@pytest.mark.asyncio
async def test_notifier_handles_discord_non_ok_status_without_raising():
    """Non-2xx responses must be logged, not raised."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = "Bad Request"
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.MEDIUM, title="T", body="B",
            source="smart_money", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        # Must not raise
        await notifier.send(alert)


@pytest.mark.asyncio
async def test_notifier_handles_discord_request_exception_without_raising():
    """Network errors must be logged, not raised."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post", side_effect=req_lib.RequestException("timeout")):
        notifier = _make_notifier(discord_webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            symbol="SPY", level=AlertLevel.LOW, title="T", body="B",
            source="smart_money", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        # Must not raise
        await notifier.send(alert)


@pytest.mark.asyncio
async def test_notifier_skips_email_when_empty():
    """No-op email must not raise and must not call requests.post."""
    from src.alerts.rules import Alert, AlertLevel
    with patch("src.alerts.notifier.requests.post") as mock_post:
        notifier = _make_notifier(discord_webhook_url="", alert_email="")
        alert = Alert(
            symbol="SPY", level=AlertLevel.LOW, title="T", body="B",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        await notifier.send(alert)
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# MultiLegBuffer + evaluate_multi_leg_strategy (ext 4)
# ---------------------------------------------------------------------------

def test_multi_leg_buffer_ignores_non_multi_leg():
    """BLOCK trade → buffer stays empty."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    trade = _make_classified_trade(trade_type=TradeType.BLOCK)
    buffer.add(trade)
    assert buffer._groups == {}


def test_multi_leg_buffer_ignores_trade_without_strategy_group():
    """MULTI_LEG trade with strategy_group=None → ignored."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    trade = _make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=None)
    buffer.add(trade)
    assert buffer._groups == {}


def test_multi_leg_buffer_accumulates_legs_in_same_group():
    """Two legs with same strategy_group → both buffered under that key."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    group = "SPY:2026-06-12T10:00:00+00:00"
    trade1 = _make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=group)
    trade2 = _make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=group, con_id=22222)
    buffer.add(trade1)
    buffer.add(trade2)
    assert len(buffer._groups[group]) == 2


def test_multi_leg_buffer_separate_groups_tracked_independently():
    """Different strategy_groups → both tracked independently."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    g1 = "SPY:2026-06-12T10:00:00+00:00"
    g2 = "SPY:2026-06-12T10:01:00+00:00"
    buffer.add(_make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=g1))
    buffer.add(_make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=g2))
    assert g1 in buffer._groups
    assert g2 in buffer._groups


def test_multi_leg_buffer_flush_returns_completed_groups():
    """Groups with old timestamps are returned and removed from the buffer."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    old_ts = datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    group = "SPY:old"
    trade = _make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=group, timestamp=old_ts)
    buffer.add(trade)
    cutoff = datetime.now(timezone.utc)
    flushed = buffer.flush_completed(cutoff)
    assert len(flushed) == 1
    assert flushed[0][0].strategy_group == group
    assert group not in buffer._groups


def test_multi_leg_buffer_does_not_flush_recent_groups():
    """Groups with fresh timestamps are NOT returned by flush_completed."""
    from src.alerts.rules import MultiLegBuffer
    from src.analysis.flow_classifier import TradeType
    buffer = MultiLegBuffer(window_seconds=2.0)
    group = "SPY:fresh"
    trade = _make_classified_trade(trade_type=TradeType.MULTI_LEG, strategy_group=group)
    buffer.add(trade)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    flushed = buffer.flush_completed(cutoff)
    assert len(flushed) == 0
    assert group in buffer._groups


def test_evaluate_multi_leg_strategy_raises_on_empty_list():
    """Empty trades list → ValueError."""
    from src.alerts.rules import AlertRules
    from config.settings import Settings
    rules = AlertRules(Settings(min_premium=100.0))
    with pytest.raises(ValueError, match="at least one trade"):
        rules.evaluate_multi_leg_strategy([])


def test_evaluate_multi_leg_strategy_returns_alert():
    """Valid trades list → Alert with source='multi_leg' and strategy in title."""
    from src.alerts.rules import AlertRules
    from src.analysis.flow_classifier import TradeType, MultiLegStrategy
    from config.settings import Settings
    rules = AlertRules(Settings(min_premium=100.0))
    trade = _make_classified_trade(
        trade_type=TradeType.MULTI_LEG,
        strategy_group="SPY:test",
        multi_leg_strategy=MultiLegStrategy.STRADDLE,
        strategy_net_premium=50_000.0,
        window_ticks=2,
    )
    alert = rules.evaluate_multi_leg_strategy([trade])
    assert alert.source == "multi_leg"
    assert alert.symbol == "SPY"
    assert "STRADDLE" in alert.title


def test_evaluate_multi_leg_strategy_level_high_when_premium_large():
    """strategy_net_premium >= unusual_premium_threshold → AlertLevel.HIGH."""
    from src.alerts.rules import AlertRules, AlertLevel
    from src.analysis.flow_classifier import TradeType, MultiLegStrategy
    from config.settings import Settings
    settings = Settings(min_premium=100.0, unusual_premium_threshold=50_000.0)
    rules = AlertRules(settings)
    trade = _make_classified_trade(
        trade_type=TradeType.MULTI_LEG,
        strategy_group="SPY:test",
        multi_leg_strategy=MultiLegStrategy.IRON_CONDOR,
        strategy_net_premium=100_000.0,
        window_ticks=4,
    )
    alert = rules.evaluate_multi_leg_strategy([trade])
    assert alert.level == AlertLevel.HIGH
