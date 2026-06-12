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
        await asyncio.to_thread(self._send_email, alert)

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

        Note:
            Called via ``asyncio.to_thread`` so any future blocking smtplib
            call will not stall the event loop.

        Args:
            alert: Alert to (not yet) deliver by email.
        """
        if not self._settings.alert_email:
            return
        logger.info(
            "notifier: email to {} not implemented — configure Discord for now",
            self._settings.alert_email,
        )


if __name__ == "__main__":
    from datetime import date as _date, timedelta

    from src.analysis.flow_classifier import FlowClassifier
    from src.analysis.greeks_engine import GreeksEngine
    from src.analysis.smart_money import SmartMoneyDetector
    from src.analysis.unusual_detector import UnusualDetector
    from src.alerts.rules import AlertRules
    from src.data.tick_stream import TickUpdate

    async def _main() -> None:
        settings = Settings(
            min_premium=100.0,
            min_block_size=500,
            unusual_volume_multiplier=3.0,
            unusual_premium_threshold=250_000.0,
            otm_premium_threshold=100_000.0,
            near_expiry_days=7,
            smart_money_min_confidence=0.30,
            risk_free_rate=0.05,
            # Leave discord_webhook_url empty so smoke test doesn't fire a real webhook
            discord_webhook_url="",
        )

        classifier = FlowClassifier(settings)
        engine = GreeksEngine(settings)
        detector = UnusualDetector(settings)
        smart = SmartMoneyDetector(settings)
        rules = AlertRules(settings)
        notifier = Notifier(settings)

        future_expiry = (_date.today() + timedelta(days=60)).strftime("%Y%m%d")
        base_time = datetime.now(timezone.utc)

        # Scenario: big block buy that triggers PREMIUM_SIZE
        tick = TickUpdate(
            symbol="SPY", con_id=91000, expiry=future_expiry,
            strike=500.0, right="C",
            timestamp=base_time,
            bid=1.38, ask=1.62, last=1.60,
            volume=2000, open_interest=3000, last_size=2000,
            underlying_price=500.0, implied_vol=0.25, delta=0.45,
        )
        trade = classifier.classify(tick)
        alerts_sent = 0
        if trade:
            enriched = engine.enrich(trade)
            unusual_sig = await detector.detect(enriched)
            smart_sig = smart.score(enriched)

            if unusual_sig:
                alert = rules.evaluate_unusual(unusual_sig)
                logger.info(
                    "[unusual] {} level={} title={}",
                    unusual_sig.symbol, alert.level.value, alert.title,
                )
                await notifier.send(alert)
                alerts_sent += 1

            if smart_sig:
                alert = rules.evaluate_smart_money(smart_sig)
                logger.info(
                    "[smart_money] {} level={} conf={:.0%} title={}",
                    smart_sig.symbol, alert.level.value, smart_sig.confidence, alert.title,
                )
                await notifier.send(alert)
                alerts_sent += 1
        else:
            logger.info("trade below min_premium threshold — no alerts")

        logger.success(
            "Smoke test complete. {} alert(s) evaluated (discord skipped — no webhook configured).",
            alerts_sent,
        )

    asyncio.run(_main())
