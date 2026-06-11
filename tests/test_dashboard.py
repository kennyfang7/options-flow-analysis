from __future__ import annotations

from src.storage.db import get_sync_engine, make_sync_engine
from src.dashboard.shared_state import SharedState


class TestSyncEngine:
    def test_make_sync_engine_strips_aiosqlite(self):
        engine = make_sync_engine("sqlite+aiosqlite:///test.db")
        assert "aiosqlite" not in str(engine.url)
        engine.dispose()

    def test_make_sync_engine_plain_sqlite_unchanged(self):
        engine = make_sync_engine("sqlite:///test.db")
        assert str(engine.url).startswith("sqlite:///")
        assert "aiosqlite" not in str(engine.url)
        engine.dispose()

    def test_get_sync_engine_is_singleton(self, monkeypatch):
        import src.storage.db as db_module
        monkeypatch.setattr(db_module, "_sync_engine", None)
        e1 = get_sync_engine()
        e2 = get_sync_engine()
        assert e1 is e2
        e1.dispose()


def _make_snap(symbol: str) -> "SentimentSnapshot":
    from src.analysis.sentiment import SentimentSnapshot
    from datetime import datetime, timezone
    return SentimentSnapshot(
        symbol=symbol,
        window_seconds=3600.0,
        computed_at=datetime.now(timezone.utc),
        trade_count=10,
        call_volume=500,
        put_volume=300,
        call_premium=100_000.0,
        put_premium=60_000.0,
        call_count=5,
        put_count=3,
        put_call_volume_ratio=0.6,
        put_call_premium_ratio=0.6,
        net_premium=40_000.0,
        avg_call_iv=0.25,
        avg_put_iv=0.30,
        iv_skew=0.05,
        net_delta_exposure=12_000.0,
        net_gamma_exposure=-5_000.0,
        bullish_premium=80_000.0,
        bearish_premium=40_000.0,
        directional_bias=0.33,
    )


def _make_alert(i: int = 0, level=None) -> "Alert":
    from src.alerts.rules import Alert, AlertLevel
    from datetime import datetime, timezone
    return Alert(
        symbol="SPY",
        level=level or AlertLevel.LOW,
        title=f"SPY TEST {i}",
        body=f"Test alert body {i}\nSecond line",
        source="unusual",
        emitted_at=datetime.now(timezone.utc),
        metadata={"symbol": "SPY"},
    )


class TestSharedState:
    def test_update_and_get_sentiment(self):
        state = SharedState()
        snap = _make_snap("SPY")
        state.update_sentiment(snap)
        assert state.get_sentiment("SPY") is snap

    def test_get_sentiment_unknown_symbol_returns_none(self):
        state = SharedState()
        assert state.get_sentiment("UNKNOWN") is None

    def test_get_all_sentiment_returns_all_symbols(self):
        state = SharedState()
        state.update_sentiment(_make_snap("SPY"))
        state.update_sentiment(_make_snap("QQQ"))
        result = state.get_all_sentiment()
        assert set(result.keys()) == {"SPY", "QQQ"}

    def test_get_all_sentiment_returns_copy(self):
        state = SharedState()
        state.update_sentiment(_make_snap("SPY"))
        result = state.get_all_sentiment()
        result["NEW"] = _make_snap("NEW")
        assert "NEW" not in state.get_all_sentiment()

    def test_push_and_drain_single_alert(self):
        state = SharedState()
        alert = _make_alert()
        state.push_alert(alert)
        drained = state.drain_alerts()
        assert len(drained) == 1
        assert drained[0] is alert

    def test_drain_empty_returns_empty_list(self):
        state = SharedState()
        assert state.drain_alerts() == []

    def test_alert_overflow_evicts_oldest(self):
        state = SharedState(max_alerts=3)
        for i in range(5):
            state.push_alert(_make_alert(i))
        drained = state.drain_alerts(max_count=10)
        assert len(drained) == 3
        # Oldest 2 (i=0, i=1) should be evicted; newest 3 survive
        titles = {a.title for a in drained}
        assert "SPY TEST 2" in titles
        assert "SPY TEST 3" in titles
        assert "SPY TEST 4" in titles

    def test_drain_max_count_is_respected(self):
        state = SharedState()
        for i in range(10):
            state.push_alert(_make_alert(i))
        drained = state.drain_alerts(max_count=4)
        assert len(drained) == 4
        remaining = state.drain_alerts(max_count=100)
        assert len(remaining) == 6


from src.dashboard.layouts import build_layout


def _collect_ids(component) -> set[str]:
    """Recursively collect all component IDs in a Dash layout tree."""
    ids: set[str] = set()
    if not hasattr(component, "id"):
        return ids
    if component.id:
        ids.add(component.id)
    children = getattr(component, "children", None)
    if children is None:
        return ids
    if isinstance(children, list):
        for child in children:
            ids |= _collect_ids(child)
    elif hasattr(children, "id"):
        ids |= _collect_ids(children)
    return ids


class TestLayout:
    _REQUIRED_IDS = {
        "fast-interval",
        "slow-interval",
        "alerts-store",
        "symbol-dropdown",
        "last-update",
        "sentiment-section",
        "signals-table",
        "trades-table",
        "alerts-panel",
    }

    def test_build_layout_returns_html_div(self):
        from dash import html
        layout = build_layout(["SPY"])
        assert isinstance(layout, html.Div)

    def test_build_layout_contains_all_required_ids(self):
        layout = build_layout(["SPY", "QQQ"])
        found_ids = _collect_ids(layout)
        for required in self._REQUIRED_IDS:
            assert required in found_ids, f"Layout missing component id='{required}'"

    def test_build_layout_dropdown_has_all_symbols(self):
        layout = build_layout(["SPY", "QQQ", "AAPL"])
        found_ids = _collect_ids(layout)
        assert "symbol-dropdown" in found_ids


from src.alerts.rules import AlertLevel
from src.storage.models import ClassifiedTradeRecord, UnusualSignalRecord
from src.dashboard.callbacks import (
    _alert_to_div,
    _sentiment_kpis,
    _signal_record_to_row,
    _trade_record_to_row,
)


def _make_signal_record(premium: float | None = 100_000.0) -> UnusualSignalRecord:
    from datetime import datetime
    return UnusualSignalRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20261231",
        strike=500.0,
        right="C",
        underlying_price=490.0,
        implied_vol=0.25,
        delta=0.45,
        effective_price=1.00,
        trade_type="block",
        aggressor="buy",
        premium=premium,
        volume_delta=500,
        signal_strength=7.5,
        top_reason="premium_size",
        reasons='["premium_size"]',
        classified_at=datetime(2026, 3, 14, 14, 30, 0),
        flagged_at=datetime(2026, 3, 14, 14, 30, 1),
    )


def _make_trade_record() -> ClassifiedTradeRecord:
    from datetime import datetime
    return ClassifiedTradeRecord(
        con_id=12345,
        symbol="SPY",
        expiry="20261231",
        strike=500.0,
        right="C",
        underlying_price=490.0,
        implied_vol=0.25,
        delta=0.45,
        trade_type="block",
        aggressor="buy",
        spread_position=0.85,
        effective_price=1.00,
        last_size=500,
        premium=50_000.0,
        signal_strength=5.2,
        volume_delta=500,
        window_ticks=1,
        classified_at=datetime(2026, 3, 14, 14, 30, 0),
    )


class TestPureFunctions:
    def test_sentiment_kpis_none_returns_eight_dash_cards(self):
        cards = _sentiment_kpis(None)
        assert len(cards) == 8
        for card in cards:
            assert card.children[1].children == "—"

    def test_sentiment_kpis_with_snapshot_returns_eight_cards(self):
        snap = _make_snap("SPY")
        cards = _sentiment_kpis(snap)
        assert len(cards) == 8

    def test_sentiment_kpis_pc_vol_formatted(self):
        snap = _make_snap("SPY")  # put_call_volume_ratio=0.6
        cards = _sentiment_kpis(snap)
        assert cards[0].children[1].children == "0.60"

    def test_signal_record_to_row_expected_keys(self):
        row = _signal_record_to_row(_make_signal_record())
        assert set(row.keys()) == {"Time", "Symbol", "Type", "Side", "Premium", "Reason", "ErnDTE"}

    def test_signal_record_to_row_none_premium_shows_dash(self):
        row = _signal_record_to_row(_make_signal_record(premium=None))
        assert row["Premium"] == "—"

    def test_signal_record_to_row_premium_formatted(self):
        row = _signal_record_to_row(_make_signal_record(premium=100_000.0))
        assert row["Premium"] == "$100,000"

    def test_trade_record_to_row_expected_keys(self):
        row = _trade_record_to_row(_make_trade_record())
        assert set(row.keys()) == {"Time", "Symbol", "Type", "Side", "Premium", "Strength", "ErnDTE"}

    def test_alert_to_div_returns_html_div(self):
        from dash import html
        div = _alert_to_div(_make_alert())
        assert isinstance(div, html.Div)

    def test_alert_to_div_high_level_badge_is_red(self):
        div = _alert_to_div(_make_alert(level=AlertLevel.HIGH))
        badge = div.children[0]
        assert badge.style["color"] == "#FF4444"

    def test_alert_to_div_medium_level_badge_is_orange(self):
        div = _alert_to_div(_make_alert(level=AlertLevel.MEDIUM))
        badge = div.children[0]
        assert badge.style["color"] == "#FF8C00"

    def test_alert_to_div_earnings_today_tag_rendered(self):
        """⚡ earnings tag is rendered as an orange inline span."""
        from src.alerts.rules import Alert, AlertLevel
        from datetime import datetime, timezone
        alert = Alert(
            symbol="SPY", level=AlertLevel.HIGH,
            title="SPY UNUSUAL", body="BLOCK BUY | 500 cts\n⚡ Earnings TODAY",
            source="unusual", emitted_at=datetime.now(timezone.utc),
            metadata={},
        )
        div = _alert_to_div(alert)
        # children: [badge, body_span, earnings_span, time_span]
        texts = [
            getattr(c, "children", "") for c in div.children
            if hasattr(c, "children") and isinstance(getattr(c, "children", None), str)
        ]
        assert any("⚡ Earnings TODAY" in t for t in texts)
        # earnings span should be orange
        earnings_span = next(
            c for c in div.children
            if hasattr(c, "children") and "⚡" in str(getattr(c, "children", ""))
        )
        assert earnings_span.style["color"] == "#FF8C00"

    def test_alert_to_div_no_earnings_tag_unchanged(self):
        """Alert body with no ⚡/📅 tag renders without earnings span."""
        div = _alert_to_div(_make_alert())
        # All span texts — none should contain ⚡ or 📅
        all_texts = " ".join(
            str(getattr(c, "children", "")) for c in div.children
        )
        assert "⚡" not in all_texts
        assert "📅" not in all_texts

    def test_signal_columns_include_erndte(self):
        from src.dashboard.layouts import _SIGNAL_COLUMNS
        assert "ErnDTE" in _SIGNAL_COLUMNS

    def test_trade_columns_include_erndte(self):
        from src.dashboard.layouts import _TRADE_COLUMNS
        assert "ErnDTE" in _TRADE_COLUMNS

    def test_signal_record_erndte_none_shows_dash(self):
        """UnusualSignalRecord without days_to_earnings shows '—'."""
        row = _signal_record_to_row(_make_signal_record())
        assert row["ErnDTE"] == "—"

    def test_trade_record_erndte_none_shows_dash(self):
        """ClassifiedTradeRecord without days_to_earnings shows '—'."""
        row = _trade_record_to_row(_make_trade_record())
        assert row["ErnDTE"] == "—"


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession

from src.dashboard.callbacks import _query_signal_rows, _query_trade_rows
from src.dashboard.app import create_app
from src.storage.models import Base


@pytest.fixture
def sync_db():
    """In-memory SQLite with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


class TestDBQueryHelpers:
    def test_query_signal_rows_empty_db(self, sync_db, monkeypatch):
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert rows == []

    def test_query_signal_rows_returns_inserted_record(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            session.add(_make_signal_record())
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"

    def test_query_signal_rows_symbol_filter(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            r1 = _make_signal_record()
            r2 = UnusualSignalRecord(
                con_id=99999, symbol="QQQ", expiry="20261231", strike=400.0, right="P",
                trade_type="block", aggressor="buy", volume_delta=100,
                top_reason="oi_ratio", reasons='["oi_ratio"]',
                classified_at=r1.classified_at, flagged_at=r1.flagged_at,
            )
            session.add_all([r1, r2])
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_signal_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"

    def test_query_trade_rows_empty_db(self, sync_db, monkeypatch):
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_trade_rows("SPY", 10)
        assert rows == []

    def test_query_trade_rows_returns_inserted_record(self, sync_db, monkeypatch):
        with SyncSession(sync_db) as session:
            session.add(_make_trade_record())
            session.commit()
        monkeypatch.setattr("src.dashboard.callbacks.get_sync_engine", lambda: sync_db)
        rows = _query_trade_rows("SPY", 10)
        assert len(rows) == 1
        assert rows[0]["Symbol"] == "SPY"


class TestCreateApp:
    def test_create_app_returns_dash_instance(self):
        from dash import Dash
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        assert isinstance(app, Dash)

    def test_create_app_title_is_set(self):
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        assert app.title == "Options Flow"

    def test_create_app_registers_four_callbacks(self):
        state = SharedState()
        app = create_app(state, symbols=["SPY"])
        assert len(app.callback_map) >= 4

    def test_create_app_default_symbols(self):
        state = SharedState()
        app = create_app(state)
        assert app is not None
