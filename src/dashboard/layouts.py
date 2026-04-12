from __future__ import annotations

from dash import dcc, html
from dash import dash_table


_SIGNAL_COLUMNS = ["Time", "Symbol", "Type", "Side", "Premium", "Reason"]
_TRADE_COLUMNS  = ["Time", "Symbol", "Type", "Side", "Premium", "Strength"]

_TABLE_STYLE_HEADER = {"backgroundColor": "#222", "color": "#eee", "fontWeight": "bold"}
_TABLE_STYLE_DATA   = {"backgroundColor": "#1a1a1a", "color": "#eee"}
_TABLE_STYLE_TABLE  = {"marginBottom": "16px", "overflowX": "auto"}


def build_layout(
    symbols: list[str],
    fast_ms: int = 5_000,
    slow_ms: int = 10_000,
) -> html.Div:
    """Build the full single-page dashboard layout.

    All component IDs used by callbacks are defined here. No IO is performed.

    IDs defined:
        fast-interval, slow-interval, alerts-store, symbol-dropdown,
        last-update, sentiment-section, signals-table, trades-table, alerts-panel

    Args:
        symbols: Ticker symbols for the dropdown. First symbol is pre-selected.
        fast_ms: Refresh interval (ms) for sentiment cards and alerts panel.
        slow_ms: Refresh interval (ms) for DB-backed signals and trades tables.

    Returns:
        Root html.Div of the complete dashboard layout.
    """
    default_symbol = symbols[0] if symbols else None

    return html.Div(
        id="root",
        style={
            "fontFamily": "monospace",
            "backgroundColor": "#111",
            "color": "#eee",
            "padding": "12px",
            "minHeight": "100vh",
        },
        children=[
            dcc.Interval(id="fast-interval", interval=fast_ms, n_intervals=0),
            dcc.Interval(id="slow-interval", interval=slow_ms, n_intervals=0),
            dcc.Store(id="alerts-store", data=[]),
            html.Div(
                id="header-row",
                style={"display": "flex", "alignItems": "center", "gap": "16px", "marginBottom": "12px"},
                children=[
                    html.H2("Options Flow", style={"margin": 0, "color": "#4FC3F7"}),
                    dcc.Dropdown(
                        id="symbol-dropdown",
                        options=[{"label": s, "value": s} for s in symbols],
                        value=default_symbol,
                        clearable=False,
                        style={"width": "120px", "color": "#111", "fontFamily": "monospace"},
                    ),
                    html.Span(id="last-update", style={"color": "#888", "fontSize": "0.85em"}),
                    html.Span(
                        id="pipeline-status",
                        style={"color": "#666", "fontSize": "0.8em", "marginLeft": "8px"},
                    ),
                ],
            ),
            html.Div(id="sentiment-section", style={"marginBottom": "16px", "flexWrap": "wrap"}),
            html.H4("Unusual Signals", style={"color": "#FF8C00", "marginBottom": "4px"}),
            dash_table.DataTable(
                id="signals-table",
                columns=[{"name": c, "id": c} for c in _SIGNAL_COLUMNS],
                data=[],
                style_header=_TABLE_STYLE_HEADER,
                style_data=_TABLE_STYLE_DATA,
                style_table=_TABLE_STYLE_TABLE,
                page_size=20,
                sort_action="native",
            ),
            html.H4("Classified Trades", style={"color": "#4FC3F7", "marginBottom": "4px"}),
            dash_table.DataTable(
                id="trades-table",
                columns=[{"name": c, "id": c} for c in _TRADE_COLUMNS],
                data=[],
                style_header=_TABLE_STYLE_HEADER,
                style_data=_TABLE_STYLE_DATA,
                style_table=_TABLE_STYLE_TABLE,
                page_size=20,
                sort_action="native",
            ),
            html.H4("Alerts", style={"color": "#FF4444", "marginBottom": "4px"}),
            html.Div(
                id="alerts-panel",
                style={
                    "maxHeight": "300px",
                    "overflowY": "auto",
                    "backgroundColor": "#1a1a1a",
                    "padding": "8px",
                    "borderRadius": "4px",
                },
            ),
        ],
    )
