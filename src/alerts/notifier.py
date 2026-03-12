from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import requests
from loguru import logger

from config.settings import Settings
from src.alerts.rules import Alert, AlertLevel


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMBED_COLORS: dict[AlertLevel, int] = {
    AlertLevel.HIGH:   0xFF0000,  # red
    AlertLevel.MEDIUM: 0xFF8C00,  # dark orange
    AlertLevel.LOW:    0xFFD700,  # gold
}


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class Notifier:
    """Delivers Alert objects to configured notification endpoints.

    Currently supports Discord webhooks (via HTTP POST with requests).
    Email alerting is a no-op stub — logs and returns immediately.

    Both send paths silently skip when the relevant setting is empty,
    so the class is safe to instantiate regardless of configuration.
    Delivery failures are logged at ERROR level; they do NOT raise.

    Args:
        settings: Application settings (discord_webhook_url, alert_email).

    Example:
        notifier = Notifier(settings)
        await notifier.send(alert)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, alert: Alert) -> None:
        """Deliver an alert to all configured endpoints.

        Runs the Discord POST in a thread via asyncio.to_thread to avoid
        blocking the event loop. Email is stubbed.

        Args:
            alert: Alert from AlertRules.evaluate_unusual() or
                evaluate_smart_money().
        """
        await asyncio.to_thread(self._send_discord, alert)
        self._send_email(alert)

    def _send_discord(self, alert: Alert) -> None:
        """POST alert as a Discord embed to the configured webhook URL.

        Skips silently when discord_webhook_url is empty.
        Logs ERROR on non-2xx responses or network exceptions — does not raise.

        Args:
            alert: Alert to deliver.
        """
        url = self._settings.discord_webhook_url
        if not url:
            logger.debug("notifier: discord_webhook_url not set — skipping")
            return

        payload = {
            "username": "Options Flow",
            "embeds": [
                {
                    "title": alert.title,
                    "description": alert.body,
                    "color": _EMBED_COLORS[alert.level],
                    "timestamp": alert.emitted_at.isoformat(),
                }
            ],
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                logger.info("notifier: discord sent — {}", alert.title)
            else:
                logger.error(
                    "notifier: discord HTTP {} — {}",
                    resp.status_code,
                    resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.error("notifier: discord exception — {}", exc)

    def _send_email(self, alert: Alert) -> None:
        """Email stub — not implemented.

        Logs an info message when alert_email is configured, then returns.
        Full SMTP implementation deferred to a future iteration.

        Args:
            alert: Alert to (not yet) deliver by email.
        """
        if not self._settings.alert_email:
            return
        logger.info(
            "notifier: email to {} not implemented — configure Discord for now",
            self._settings.alert_email,
        )
