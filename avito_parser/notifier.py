from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from avito_parser.analytics import FlipCandidate
    from avito_parser.config import AppConfig
    from avito_parser.models import FlipAlert

logger = logging.getLogger(__name__)

ALERT_EMOJI: dict[str, str] = {
    "quick": "⚡",
    "deep": "💰",
}

MAX_ALERTS_PER_HOUR = 10
THROTTLE_WINDOW_SECONDS = 3600


def _fmt_rub(value: int) -> str:
    return f"{value:,}".replace(",", " ")


class TelegramNotifier:
    def __init__(self, config: AppConfig) -> None:
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        self._recent_timestamps: list[float] = []
        self._bot = None

    @property
    def _is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _get_bot(self):
        if self._bot is None:
            from telegram import Bot
            self._bot = Bot(token=self._token)
        return self._bot

    def _check_throttle(self) -> bool:
        now = time.time()
        cutoff = now - THROTTLE_WINDOW_SECONDS
        self._recent_timestamps = [ts for ts in self._recent_timestamps if ts > cutoff]
        if len(self._recent_timestamps) >= MAX_ALERTS_PER_HOUR:
            logger.info(
                "Telegram throttle: %d alerts sent in the last hour, blocking",
                len(self._recent_timestamps),
            )
            return False
        return True

    def _format_message(
        self,
        candidate: FlipCandidate,
        alert_type: Literal["quick", "deep"],
        city: str,
    ) -> str:
        emoji = ALERT_EMOJI.get(alert_type, "")
        roi_sign = "+" if candidate.roi_percent >= 0 else ""

        lines = [
            f"{emoji} <b>Флип-сделка: {candidate.title}</b>",
            "",
            f"🏙 Город: {city}",
            f"💵 Цена покупки: {_fmt_rub(candidate.buy_price)} ₽",
            f"💲 Оценка перепродажи: {_fmt_rub(candidate.estimated_resale)} ₽",
            f"📈 ROI: {roi_sign}{candidate.roi_percent:.2f}%",
            f"📉 Скидка: {candidate.discount_percent:.1f}%",
            f"🧾 Чистая прибыль: {_fmt_rub(candidate.net_profit)} ₽",
            f"📦 Состояние: {candidate.condition}",
        ]

        if candidate.red_flags:
            lines.append(f"⚠️ Флаги ({candidate.flag_count}): {', '.join(candidate.red_flags)}")

        lines.append("")
        lines.append(f"🔗 <a href=\"{candidate.url}\">Открыть на Avito</a>")

        return "\n".join(lines)

    def send_flip_alert(self, flip_alert: FlipAlert) -> bool:
        if not self._is_configured:
            logger.warning("Telegram notifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
            return False

        if not self._check_throttle():
            return False

        candidate = flip_alert.flip_candidate
        message = self._format_message(candidate, flip_alert.alert_type, flip_alert.city)

        try:
            asyncio.run(
                self._get_bot().send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            )
            self._recent_timestamps.append(time.time())
            logger.info("Telegram alert sent: %s (%s)", candidate.title, flip_alert.alert_type)
            return True
        except Exception as exc:
            logger.error("Failed to send Telegram alert: %s", exc)
            return False
