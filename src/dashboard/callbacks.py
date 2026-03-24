from __future__ import annotations

from datetime import datetime, timezone

from dash import Dash, Input, Output, State, html
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from src.alerts.rules import Alert, AlertLevel
from src.analysis.sentiment import SentimentSnapshot
from src.storage.db import get_sync_engine
from src.storage.models import ClassifiedTradeRecord, UnusualSignalRecord


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_ratio(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _fmt_dollars(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


# ---------------------------------------------------------------------------
# Pure rendering functions
# ---------------------------------------------------------------------------


def _get(snap: SentimentSnapshot | None, attr: str) -> float | None:
    """Safely read a float attribute from a snapshot that may be None."""
    return getattr(snap, attr) if snap is not None else None


def _sentiment_kpis(snap: SentimentSnapshot | None) -> list[html.Div]:
    """Convert a SentimentSnapshot to a row of 8 KPI cards.

    Called from the update_sentiment callback. Returns eight html.Div
    cards regardless of whether snap is None (dashes shown for None).

    Args:
        snap: Latest SentimentSnapshot for the selected symbol, or None.

    Returns:
        List of 8 html.Div KPI cards in order: P/C Volume, P/C Premium,
        Net Premium, Directional, IV Skew, Δ Exposure, Γ Exposure, Trades.
    """
    values = [
        ("P/C Volume",  _fmt_ratio(_get(snap, "put_call_volume_ratio"))),
        ("P/C Premium", _fmt_ratio(_get(snap, "put_call_premium_ratio"))),
        ("Net Premium", _fmt_dollars(_get(snap, "net_premium"))),
        ("Directional", _fmt_pct(_get(snap, "directional_bias"))),
        ("IV Skew",     _fmt_ratio(_get(snap, "iv_skew"))),
        ("Δ Exposure",  _fmt_dollars(_get(snap, "net_delta_exposure"))),
        ("Γ Exposure",  _fmt_dollars(_get(snap, "net_gamma_exposure"))),
        ("Trades",      str(snap.trade_count) if snap is not None else "—"),
    ]
    return [
        html.Div(
            style={
                "display": "inline-block",
                "padding": "8px 16px",
                "margin": "4px",
                "backgroundColor": "#222",
                "borderRadius": "4px",
                "minWidth": "110px",
            },
            children=[
                html.Div(label, style={"fontSize": "0.75em", "color": "#888"}),
                html.Div(val,   style={"fontSize": "1.1em", "fontWeight": "bold"}),
            ],
        )
        for label, val in values
    ]


def _signal_record_to_row(r: UnusualSignalRecord) -> dict:
    """Convert an UnusualSignalRecord ORM row to a DataTable row dict.

    Args:
        r: Row from the unusual_signals table.

    Returns:
        Dict with keys: Time, Symbol, Type, Side, Premium, Reason.
    """
    return {
        "Time":    r.flagged_at.strftime("%H:%M:%S"),
        "Symbol":  r.symbol,
        "Type":    r.trade_type,
        "Side":    r.aggressor,
        "Premium": f"${r.premium:,.0f}" if r.premium is not None else "—",
        "Reason":  r.top_reason,
    }


def _trade_record_to_row(r: ClassifiedTradeRecord) -> dict:
    """Convert a ClassifiedTradeRecord ORM row to a DataTable row dict.

    Args:
        r: Row from the classified_trades table.

    Returns:
        Dict with keys: Time, Symbol, Type, Side, Premium, Strength.
    """
    return {
        "Time":     r.classified_at.strftime("%H:%M:%S"),
        "Symbol":   r.symbol,
        "Type":     r.trade_type,
        "Side":     r.aggressor,
        "Premium":  f"${r.premium:,.0f}" if r.premium is not None else "—",
        "Strength": f"{r.signal_strength:.1f}" if r.signal_strength is not None else "—",
    }


_LEVEL_COLORS: dict[AlertLevel, str] = {
    AlertLevel.HIGH:   "#FF4444",
    AlertLevel.MEDIUM: "#FF8C00",
    AlertLevel.LOW:    "#FFD700",
}


def _alert_to_div(alert: Alert) -> html.Div:
    """Render an Alert as a styled html.Div for the alerts panel.

    Args:
        alert: Alert to render.

    Returns:
        html.Div with three children: level badge, title+body, timestamp.
    """
    color = _LEVEL_COLORS.get(alert.level, "#888")
    first_line = alert.body.split("\n")[0]
    return html.Div(
        style={"padding": "4px 0", "borderBottom": "1px solid #333"},
        children=[
            html.Span(
                f"[{alert.level.value.upper()}] ",
                style={"color": color, "fontWeight": "bold"},
            ),
            html.Span(f"{alert.title} — {first_line}"),
            html.Span(
                f" {alert.emitted_at.strftime('%H:%M:%S')}",
                style={"color": "#888", "fontSize": "0.85em"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Sync DB query helpers
# ---------------------------------------------------------------------------


def _query_signal_rows(symbol: str | None, limit: int) -> list[dict]:
    """Query unusual_signals table via sync session, ordered by flagged_at desc.

    Args:
        symbol: Filter rows to this symbol if provided; None = all symbols.
        limit: Maximum number of rows to return.

    Returns:
        List of row dicts suitable for dash_table.DataTable data property.
    """
    with Session(get_sync_engine()) as session:
        q = select(UnusualSignalRecord).order_by(UnusualSignalRecord.flagged_at.desc())
        if symbol:
            q = q.where(UnusualSignalRecord.symbol == symbol)
        rows = session.execute(q.limit(limit)).scalars().all()
    return [_signal_record_to_row(r) for r in rows]


def _query_trade_rows(symbol: str | None, limit: int) -> list[dict]:
    """Query classified_trades table via sync session, ordered by classified_at desc.

    Args:
        symbol: Filter rows to this symbol if provided; None = all symbols.
        limit: Maximum number of rows to return.

    Returns:
        List of row dicts suitable for dash_table.DataTable data property.
    """
    with Session(get_sync_engine()) as session:
        q = select(ClassifiedTradeRecord).order_by(ClassifiedTradeRecord.classified_at.desc())
        if symbol:
            q = q.where(ClassifiedTradeRecord.symbol == symbol)
        rows = session.execute(q.limit(limit)).scalars().all()
    return [_trade_record_to_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Callback registration
# ---------------------------------------------------------------------------


def setup_callbacks(app: Dash, state: object) -> None:
    """Register all dcc.Interval-driven callbacks on the Dash app.

    Args:
        app: The Dash application instance.
        state: SharedState instance shared with the asyncio pipeline.
    """
    @app.callback(
        Output("sentiment-section", "children"),
        Output("last-update", "children"),
        Input("fast-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_sentiment(n_intervals: int, symbol: str) -> tuple[list, str]:
        try:
            snap = state.get_sentiment(symbol) if symbol else None
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S") + " UTC"
            status = f"Live  {ts}" if snap is not None else f"Waiting for pipeline data... ({ts})"
            return _sentiment_kpis(snap), status
        except Exception:
            logger.exception("update_sentiment: failed")
            return _sentiment_kpis(None), "Error"

    @app.callback(
        Output("signals-table", "data"),
        Input("slow-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_signals(n_intervals: int, symbol: str) -> list[dict]:
        try:
            return _query_signal_rows(symbol, settings.dashboard_max_rows)
        except Exception:
            logger.exception("update_signals: DB query failed")
            return []

    @app.callback(
        Output("trades-table", "data"),
        Input("slow-interval", "n_intervals"),
        Input("symbol-dropdown", "value"),
    )
    def update_trades(n_intervals: int, symbol: str) -> list[dict]:
        try:
            return _query_trade_rows(symbol, settings.dashboard_max_rows)
        except Exception:
            logger.exception("update_trades: DB query failed")
            return []

    @app.callback(
        Output("alerts-store", "data"),
        Output("alerts-panel", "children"),
        Input("fast-interval", "n_intervals"),
        State("alerts-store", "data"),
    )
    def update_alerts(n_intervals: int, stored: list[dict] | None) -> tuple[list[dict], list]:
        try:
            new_alerts = state.drain_alerts()
            accumulated = (stored or []) + [a.model_dump(mode="json") for a in new_alerts]
            accumulated = accumulated[-settings.dashboard_max_alerts:]
            # model_dump(mode="json") serialises datetime→ISO string and AlertLevel→str.
            # Alert(**a) reconstructs correctly because AlertLevel is a str-based Enum
            # and Pydantic v2 coerces ISO strings back to datetime automatically.
            children = [_alert_to_div(Alert(**a)) for a in reversed(accumulated)]
            return accumulated, children
        except Exception:
            logger.exception("update_alerts: failed")
            return stored or [], []
