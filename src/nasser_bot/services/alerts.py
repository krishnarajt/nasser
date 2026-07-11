from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from telegram.constants import ParseMode

from nasser_bot.config import Config
from nasser_bot.formatting import bold, h
from nasser_bot.services.health import HealthItem, HealthService


LOG = logging.getLogger(__name__)

STATUS_ICONS = {"crit": "🔴", "warn": "🟠", "unknown": "⚪", "ok": "🟢"}
DETAIL_LIMIT = 200


def short_detail(detail: str) -> str:
    text = " ".join(detail.split())
    if len(text) <= DETAIL_LIMIT:
        return text
    return f"{text[: DETAIL_LIMIT - 3]}..."


@dataclass
class AlertState:
    status: str
    label: str
    sent_at: float


class AlertService:
    def __init__(self, config: Config, health: HealthService) -> None:
        self._config = config
        self._health = health
        self._state: dict[str, AlertState] = {}

    async def check_and_send(self, bot: Any, known_chat_ids: set[int]) -> None:
        chat_ids = self._target_chat_ids(known_chat_ids)
        if not chat_ids:
            return
        report = await self._health.report()
        now = time.monotonic()
        problems = {item.key: item for item in report.problems}

        outbound: list[str] = []
        for key, item in problems.items():
            previous = self._state.get(key)
            # Details like "93.4%" fluctuate every sample, so only a status
            # transition or the repeat interval triggers a new alert.
            changed = previous is None or previous.status != item.status
            repeat = previous is not None and now - previous.sent_at >= self._config.alert_repeat_seconds
            if changed or repeat:
                outbound.append(_problem_line(item))
                self._state[key] = AlertState(status=item.status, label=item.label, sent_at=now)

        recovered = [key for key in self._state if key not in problems]
        for key in recovered:
            state = self._state.pop(key)
            outbound.append(f"🟢 {bold(state.label)} recovered (was {h(state.status.upper())})")

        if not outbound:
            return

        text = "\n".join([f"🔔 {bold(f'{self._config.nas_name} alert')}", "", *outbound[:20]])
        if len(outbound) > 20:
            text = f"{text}\n+{len(outbound) - 20} more"
        for chat_id in chat_ids:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                LOG.exception("Failed to send alert to chat_id=%s", chat_id)

    def _target_chat_ids(self, known_chat_ids: set[int]) -> set[int]:
        return set(self._config.alert_chat_ids or self._config.allowed_chat_ids or known_chat_ids)


async def alert_loop(application: Any) -> None:
    config: Config = application.bot_data["config"]
    service: AlertService = application.bot_data["alerts"]
    while True:
        try:
            # Re-checked every cycle so the Telegram settings menu can toggle
            # alerts and retime the loop without a restart.
            if config.alerts_enabled:
                known_chat_ids = set(application.bot_data.get("known_chat_ids") or set())
                await service.check_and_send(application.bot, known_chat_ids)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Alert loop failed")
        await asyncio.sleep(max(60, int(config.alert_interval_seconds)))


def _problem_line(item: HealthItem) -> str:
    icon = STATUS_ICONS.get(item.status, "⚪")
    return f"{icon} {bold(item.label)}: {h(short_detail(item.detail))}"

