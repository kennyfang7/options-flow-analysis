from __future__ import annotations

from dash import Dash

from src.dashboard.callbacks import setup_callbacks
from src.dashboard.layouts import build_layout
from src.dashboard.shared_state import SharedState


def create_app(state: SharedState, symbols: list[str] | None = None) -> Dash:
    """Create and configure the Options Flow Dash application.

    Builds the single-page layout and registers all dcc.Interval callbacks.
    Does NOT start the server — call app.run_server() from the orchestration
    layer or from the __main__ block below.

    Args:
        state: SharedState bridging the asyncio pipeline and Dash.
        symbols: Watchlist symbols for the dropdown. Defaults to ["SPY"].

    Returns:
        Configured Dash application, ready to call run_server() on.
    """
    if symbols is None:
        symbols = ["SPY"]

    from config.settings import settings

    app = Dash(__name__, title="Options Flow")
    app.layout = build_layout(
        symbols,
        fast_ms=int(settings.dashboard_refresh_fast * 1000),
        slow_ms=int(settings.dashboard_refresh_slow * 1000),
    )
    setup_callbacks(app, state)
    return app


if __name__ == "__main__":
    import sys

    state = SharedState()
    symbols = sys.argv[1:] or ["SPY", "QQQ", "AAPL"]
    app = create_app(state, symbols=symbols)
    app.run_server(debug=True, port=8050)
