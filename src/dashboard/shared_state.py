from __future__ import annotations

import queue

from loguru import logger

from src.alerts.rules import Alert
from src.analysis.sentiment import SentimentSnapshot


class SharedState:
    """Thread-safe state shared between the asyncio pipeline and Dash/Flask.

    The asyncio pipeline (producer thread) writes via update_sentiment() and
    push_alert(). Dash callbacks (Flask consumer thread) read via
    get_sentiment(), get_all_sentiment(), and drain_alerts().

    Thread safety:
        _sentiment dict: CPython GIL ensures atomic assignment of immutable
        Pydantic values. SentimentSnapshot is never mutated after construction.
        _alert_queue: queue.Queue is fully thread-safe; uses put_nowait/
        get_nowait to avoid blocking either thread.

    Args:
        max_alerts: Capacity of the alert queue. Oldest alert is dropped
            when a new alert arrives and the queue is full. Defaults to
            settings.dashboard_max_alerts when None.
    """

    def __init__(self, max_alerts: int | None = None) -> None:
        if max_alerts is None:
            from config.settings import settings
            max_alerts = settings.dashboard_max_alerts
        self._sentiment: dict[str, SentimentSnapshot] = {}
        self._alert_queue: queue.Queue[Alert] = queue.Queue(maxsize=max_alerts)

    def update_sentiment(self, snapshot: SentimentSnapshot) -> None:
        """Store the latest SentimentSnapshot for a symbol.

        Called from the asyncio pipeline thread. Overwrites any previous
        snapshot for the same symbol.

        Args:
            snapshot: Latest SentimentSnapshot from SentimentAggregator.
        """
        self._sentiment[snapshot.symbol] = snapshot

    def get_sentiment(self, symbol: str) -> SentimentSnapshot | None:
        """Return the most recent SentimentSnapshot for a symbol.

        Called from Dash callback thread.

        Args:
            symbol: Ticker symbol, e.g. "SPY".

        Returns:
            Latest SentimentSnapshot, or None if no data for this symbol.
        """
        return self._sentiment.get(symbol)

    def get_all_sentiment(self) -> dict[str, SentimentSnapshot]:
        """Return a shallow copy of all current sentiment snapshots.

        Called from Dash callback thread.

        Returns:
            Dict mapping symbol -> SentimentSnapshot for all known symbols.
        """
        return dict(self._sentiment)

    def push_alert(self, alert: Alert) -> None:
        """Enqueue an alert for display in the dashboard.

        Non-blocking. If the queue is full, the oldest alert is dropped
        to make room for the new one.

        Called from the asyncio pipeline thread.

        Args:
            alert: Alert to enqueue.
        """
        try:
            self._alert_queue.put_nowait(alert)
        except queue.Full:
            try:
                self._alert_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._alert_queue.put_nowait(alert)
            except queue.Full:
                logger.warning("shared_state: alert queue still full after eviction; dropping alert")

    def drain_alerts(self, max_count: int = 50) -> list[Alert]:
        """Remove and return up to max_count alerts from the queue.

        Non-blocking. Returns an empty list when the queue is empty.
        Called from the Dash callback thread.

        Args:
            max_count: Maximum number of alerts to return.

        Returns:
            List of Alert objects, oldest first.
        """
        result: list[Alert] = []
        while len(result) < max_count:
            try:
                result.append(self._alert_queue.get_nowait())
            except queue.Empty:
                break
        return result
