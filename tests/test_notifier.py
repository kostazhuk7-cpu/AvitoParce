from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from avito_parser.analytics import FlipCandidate
from avito_parser.config import AppConfig
from avito_parser.models import FlipAlert
from avito_parser.notifier import TelegramNotifier, MAX_ALERTS_PER_HOUR


def _make_flip_candidate(
    title: str = "iPhone 16 Pro Max 256GB",
    buy_price: int = 120000,
    estimated_resale: int = 150000,
    roi_percent: float = 25.0,
    discount_percent: float = 20.0,
    net_profit: int = 30000,
    url: str = "https://www.avito.ru/item/12345",
    condition: str = "Новое",
    red_flags: list[str] | None = None,
    flag_count: int = 0,
    commission: int = 0,
    item_id: int = 12345,
) -> FlipCandidate:
    return FlipCandidate(
        title=title,
        url=url,
        condition=condition,
        buy_price=buy_price,
        estimated_resale=estimated_resale,
        commission=commission,
        net_profit=net_profit,
        roi_percent=roi_percent,
        discount_percent=discount_percent,
        flag_count=flag_count,
        red_flags=red_flags if red_flags is not None else [],
        item_id=item_id,
    )


def _make_config(
    token: str | None = "000:test_token",
    chat_id: str | None = "123456",
) -> AppConfig:
    return AppConfig(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
    )


class TestFormatMessage:
    def test_quick_flip_message_contains_all_fields(self):
        notifier = TelegramNotifier(_make_config())
        candidate = _make_flip_candidate(
            title="MacBook Pro M3",
            buy_price=80000,
            estimated_resale=100000,
            roi_percent=25.0,
            discount_percent=20.0,
            net_profit=20000,
            condition="Отличное",
        )
        msg = notifier._format_message(candidate, "quick", "moskva")

        assert "⚡" in msg
        assert "MacBook Pro M3" in msg
        assert "80 000" in msg
        assert "100 000" in msg
        assert "25.00%" in msg
        assert "20.0%" in msg
        assert "20 000" in msg
        assert "Отличное" in msg
        assert "moskva" in msg
        assert "avito.ru/item/12345" in msg

    def test_deep_flip_message_uses_money_emoji(self):
        notifier = TelegramNotifier(_make_config())
        candidate = _make_flip_candidate(title="Sony A7 IV")
        msg = notifier._format_message(candidate, "deep", "spb")

        assert "💰" in msg
        assert "Sony A7 IV" in msg

    def test_negative_roi_no_plus_sign(self):
        notifier = TelegramNotifier(_make_config())
        candidate = _make_flip_candidate(roi_percent=-5.0)
        msg = notifier._format_message(candidate, "quick", "moskva")

        assert "+-5.00%" not in msg
        assert "-5.00%" in msg

    def test_message_includes_red_flags_when_present(self):
        notifier = TelegramNotifier(_make_config())
        candidate = _make_flip_candidate(
            red_flags=["no_photos", "low_rating"],
            flag_count=2,
        )
        msg = notifier._format_message(candidate, "deep", "moskva")

        assert "no_photos" in msg
        assert "low_rating" in msg
        assert "2" in msg

    def test_message_no_red_flags_section_when_empty(self):
        notifier = TelegramNotifier(_make_config())
        candidate = _make_flip_candidate(red_flags=[], flag_count=0)
        msg = notifier._format_message(candidate, "quick", "moskva")

        assert "⚠️" not in msg


class TestThrottle:
    def test_check_throttle_allows_up_to_limit(self):
        notifier = TelegramNotifier(_make_config())
        now = time.time()
        notifier._recent_timestamps = [now - i for i in range(MAX_ALERTS_PER_HOUR - 1)]

        assert notifier._check_throttle() is True

    def test_check_throttle_blocks_at_limit(self):
        notifier = TelegramNotifier(_make_config())
        now = time.time()
        notifier._recent_timestamps = [now - i for i in range(MAX_ALERTS_PER_HOUR)]

        assert notifier._check_throttle() is False

    def test_check_throttle_clears_old_timestamps(self):
        notifier = TelegramNotifier(_make_config())
        now = time.time()
        old = now - 3700  # more than 1 hour ago
        notifier._recent_timestamps = [old] * MAX_ALERTS_PER_HOUR

        assert notifier._check_throttle() is True
        assert len(notifier._recent_timestamps) == 0


class TestMissingToken:
    def test_send_flip_alert_returns_false_when_token_missing(self):
        notifier = TelegramNotifier(_make_config(token=None))
        candidate = _make_flip_candidate()
        alert = FlipAlert(flip_candidate=candidate, alert_type="quick", city="moskva")

        result = notifier.send_flip_alert(alert)
        assert result is False

    def test_send_flip_alert_returns_false_when_chat_id_missing(self):
        notifier = TelegramNotifier(_make_config(chat_id=None))
        candidate = _make_flip_candidate()
        alert = FlipAlert(flip_candidate=candidate, alert_type="deep", city="spb")

        result = notifier.send_flip_alert(alert)
        assert result is False

    def test_missing_token_does_not_crash(self):
        notifier = TelegramNotifier(AppConfig())
        candidate = _make_flip_candidate()
        alert = FlipAlert(flip_candidate=candidate, alert_type="quick", city="moskva")

        result = notifier.send_flip_alert(alert)
        assert result is False


class TestSendSuccess:
    def test_send_flip_alert_calls_telegram_api(self):
        notifier = TelegramNotifier(_make_config())
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        notifier._bot = mock_bot

        candidate = _make_flip_candidate(title="Test Item")
        alert = FlipAlert(flip_candidate=candidate, alert_type="quick", city="moskva")

        result = notifier.send_flip_alert(alert)

        assert result is True
        mock_bot.send_message.assert_called_once()
        kwargs = mock_bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == "123456"
        assert kwargs["parse_mode"] == "HTML"
        assert kwargs["disable_web_page_preview"] is True
        assert "Test Item" in kwargs["text"]

    def test_send_flip_alert_returns_false_on_api_error(self):
        notifier = TelegramNotifier(_make_config())
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("API error"))
        notifier._bot = mock_bot

        candidate = _make_flip_candidate()
        alert = FlipAlert(flip_candidate=candidate, alert_type="deep", city="moskva")

        result = notifier.send_flip_alert(alert)
        assert result is False


class TestThrottleIntegration:
    def test_send_flip_alert_throttles_after_limit(self):
        notifier = TelegramNotifier(_make_config())
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        notifier._bot = mock_bot

        # Fill timestamps to the limit
        now = time.time()
        notifier._recent_timestamps = [now] * MAX_ALERTS_PER_HOUR

        candidate = _make_flip_candidate()
        alert = FlipAlert(flip_candidate=candidate, alert_type="quick", city="moskva")

        result = notifier.send_flip_alert(alert)
        assert result is False
        mock_bot.send_message.assert_not_called()
