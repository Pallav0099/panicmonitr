from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from loguru import logger

WEBHOOK_TIMEOUT_SECONDS = 10


@dataclass(slots=True, frozen=True)
class MonitorEvent:
    """A transition worth paging on. Webhook payload = ``asdict(event)``."""

    event: str                    # "monitor_down" | "monitor_up"
    peer_node_id: str
    peer_alias: Optional[str]
    source_node_id: str           # the device emitting the notification
    source_alias: Optional[str]
    timestamp: str                # ISO 8601
    consecutive_count: int        # fail count at DOWN, success count at UP
    reason: Optional[str] = None  # last failure reason (when event == "monitor_down")

    def to_payload(self) -> dict:
        """Webhook JSON body. Flat, stable, easy to parse in any integration."""
        return asdict(self)


class Notifier(ABC):
    """Channel-agnostic alert sink. Implementations fire-and-forget."""

    @abstractmethod
    async def notify(self, event: MonitorEvent) -> None: ...

    async def shutdown(self) -> None:  # pragma: no cover - trivial default
        return None


class NullNotifier(Notifier):
    """No-op notifier used when no webhook URL is configured."""

    async def notify(self, event: MonitorEvent) -> None:
        logger.debug(
            "[notify.null] {} {} (no webhook configured)",
            event.event,
            event.peer_alias or event.peer_node_id[:12],
        )


class WebhookNotifier(Notifier):
    """POSTs the event payload as JSON to a configured URL.

    Uses stdlib urllib in ``asyncio.to_thread`` — no extra HTTP dep. This is
    enough for ntfy, generic webhook receivers, and — via their "custom
    webhook" or bot URL pattern — Discord/Slack/Telegram. Channel-specific
    formatters can sit behind this ABC later without changing the engine
    contract.
    """

    def __init__(self, url: str, timeout: float = WEBHOOK_TIMEOUT_SECONDS) -> None:
        self._url = url
        self._timeout = timeout

    async def notify(self, event: MonitorEvent) -> None:
        body = json.dumps(event.to_payload()).encode("utf-8")
        label = event.peer_alias or event.peer_node_id[:12]
        try:
            status, text = await asyncio.to_thread(self._post, body)
        except Exception as exc:
            logger.error(
                "[notify.webhook] {} -> {} failed: {}: {}",
                event.event, label, type(exc).__name__, exc,
            )
            return

        if 200 <= status < 300:
            logger.info(
                "[notify.webhook] {} -> {} delivered (status={})",
                event.event, label, status,
            )
        else:
            logger.warning(
                "[notify.webhook] {} -> {} non-2xx status={} body={}",
                event.event, label, status, text[:200],
            )

    def _post(self, body: bytes) -> tuple[int, str]:
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "panic-monitor/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.getcode(), resp.read(512).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # Non-2xx with a body attached — return the status for logging.
            body_preview = exc.read(512).decode("utf-8", errors="replace") if exc.fp else ""
            return exc.code, body_preview


def build_notifier(webhook_url: Optional[str]) -> Notifier:
    """Factory used by ``main.py``. Falls back to ``NullNotifier``."""
    if not webhook_url:
        return NullNotifier()
    return WebhookNotifier(webhook_url)


def sample_event(source_node_id: str, source_alias: Optional[str] = None) -> MonitorEvent:
    """Dummy payload used by ``--test-webhook``."""
    from src import IST

    return MonitorEvent(
        event="monitor_down",
        peer_node_id="0" * 64,
        peer_alias="test-peer",
        source_node_id=source_node_id,
        source_alias=source_alias,
        timestamp=datetime.now(IST).isoformat(),
        consecutive_count=3,
        reason="test webhook from --test-webhook",
    )
