"""Tests for avito_parser.pipeline — flip analysis pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from avito_parser.analytics import FlipCandidate, FlipsResult
from avito_parser.config import AppConfig
from avito_parser.pipeline import PipelineStats, _determine_alert_type


def _make_candidate(
    title: str = "iPhone 16 Pro Max 256GB",
    buy_price: int = 110000,
    estimated_resale: int = 150000,
    roi_percent: float = 25.0,
    discount_percent: float = 20.0,
    net_profit: int = 40000,
    flag_count: int = 0,
    red_flags: list[str] | None = None,
    url: str = "https://www.avito.ru/item/12345",
    condition: str = "Новое",
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


@pytest.fixture
def mock_browser_parser():
    """Mock BrowserParser to return predefined items."""
    with patch("avito_parser.pipeline.BrowserParser") as mock:
        instance = mock.return_value
        instance.search.return_value = []
        yield instance


class TestAlertType:
    def test_quick_flip_below_40(self):
        candidate = _make_candidate(roi_percent=25.0)
        assert _determine_alert_type(candidate) == "quick"

    def test_deep_flip_at_40(self):
        candidate = _make_candidate(roi_percent=40.0)
        assert _determine_alert_type(candidate) == "deep"

    def test_deep_flip_above_40(self):
        candidate = _make_candidate(roi_percent=85.0)
        assert _determine_alert_type(candidate) == "deep"

    def test_negative_roi_is_quick(self):
        candidate = _make_candidate(roi_percent=-5.0)
        assert _determine_alert_type(candidate) == "quick"


class TestPipelineStats:
    def test_defaults(self):
        stats = PipelineStats()
        assert stats.candidates_found == 0
        assert stats.alerts_sent == 0
        assert stats.throttled == 0

    def test_custom_values(self):
        stats = PipelineStats(candidates_found=5, alerts_sent=3, throttled=2)
        assert stats.candidates_found == 5
        assert stats.alerts_sent == 3
        assert stats.throttled == 2


class TestPipelineFilter:
    """Verify the pipeline filtering logic — roi_percent > 0, flag_count <= 1."""

    def _run(self, candidates: list) -> list:
        """Simulate run_flip_pipeline filtering without browser/telegram."""
        return [
            c for c in candidates
            if c.roi_percent > 0 and c.flag_count <= 1
        ]

    def test_passes_good_candidate(self):
        c = _make_candidate(roi_percent=25.0, flag_count=0)
        result = self._run([c])
        assert len(result) == 1

    def test_filters_zero_roi(self):
        c = _make_candidate(roi_percent=0.0, flag_count=0)
        result = self._run([c])
        assert len(result) == 0

    def test_filters_negative_roi(self):
        c = _make_candidate(roi_percent=-10.0, flag_count=0)
        result = self._run([c])
        assert len(result) == 0

    def test_filters_high_flag_count(self):
        c = _make_candidate(roi_percent=25.0, flag_count=2)
        result = self._run([c])
        assert len(result) == 0

    def test_allows_flag_count_1(self):
        c = _make_candidate(roi_percent=25.0, flag_count=1)
        result = self._run([c])
        assert len(result) == 1

    def test_mixed_candidates(self):
        candidates = [
            _make_candidate(roi_percent=30.0, flag_count=0, title="Good"),
            _make_candidate(roi_percent=0.0, flag_count=0, title="Zero roi"),
            _make_candidate(roi_percent=20.0, flag_count=2, title="Too many flags"),
            _make_candidate(roi_percent=-5.0, flag_count=0, title="Negative roi"),
        ]
        result = self._run(candidates)
        assert len(result) == 1
        assert result[0].title == "Good"


class TestPipelineRun:
    """Integration-style tests with mocks for BrowserParser and TelegramNotifier."""

    def _make_flips_result(self, candidates: list[FlipCandidate]) -> FlipsResult:
        from datetime import datetime, timezone

        return FlipsResult(
            query="test",
            city="moskva",
            total_items=10,
            median_price=130000,
            mean_price=140000,
            candidates=candidates,
            scanned_at=datetime.now(timezone.utc),
        )

    @patch("avito_parser.pipeline.TelegramNotifier")
    @patch("avito_parser.pipeline.AvitoAnalytics")
    @patch("avito_parser.pipeline.AppConfig")
    def test_dry_run_prints_no_alerts(
        self, mock_config, mock_analytics, mock_notifier_cls,
    ):
        """Dry-run mode (notify=False) — should not send any alerts."""
        mock_config.from_env.return_value = AppConfig()
        mock_analytics_instance = mock_analytics.return_value
        mock_analytics_instance.analyze_flips.return_value = self._make_flips_result([
            _make_candidate(roi_percent=25.0, flag_count=0),
        ])

        with patch("avito_parser.pipeline.BrowserParser") as mock_bp:
            instance = mock_bp.return_value
            instance.search.return_value = [MagicMock()]

            from avito_parser.pipeline import run_flip_pipeline
            result = run_flip_pipeline(query="test", city="moskva", notify=False)

        assert len(result) == 1
        mock_notifier_cls.assert_not_called()

    @patch("avito_parser.pipeline.TelegramNotifier")
    @patch("avito_parser.pipeline.AvitoAnalytics")
    @patch("avito_parser.pipeline.AppConfig")
    def test_notify_mode_sends_alerts(
        self, mock_config, mock_analytics, mock_notifier_cls,
    ):
        """Notify mode with configured token — sends alerts for each candidate."""
        config = AppConfig(telegram_bot_token="000:test", telegram_chat_id="123")
        mock_config.from_env.return_value = config
        mock_analytics_instance = mock_analytics.return_value
        mock_analytics_instance.analyze_flips.return_value = self._make_flips_result([
            _make_candidate(roi_percent=25.0, flag_count=0),
            _make_candidate(roi_percent=50.0, flag_count=0),
        ])

        mock_notifier = MagicMock()
        mock_notifier.send_flip_alert.return_value = True
        mock_notifier_cls.return_value = mock_notifier

        with patch("avito_parser.pipeline.BrowserParser") as mock_bp:
            instance = mock_bp.return_value
            instance.search.return_value = [MagicMock(), MagicMock()]

            from avito_parser.pipeline import run_flip_pipeline
            result = run_flip_pipeline(query="test", city="moskva", notify=True)

        assert len(result) == 2
        assert mock_notifier.send_flip_alert.call_count == 2

    @patch("avito_parser.pipeline.TelegramNotifier")
    @patch("avito_parser.pipeline.AvitoAnalytics")
    @patch("avito_parser.pipeline.AppConfig")
    def test_notify_no_token_exits(
        self, mock_config, mock_analytics, mock_notifier_cls,
    ):
        """Notify mode without token — should sys.exit(1)."""
        config = AppConfig(telegram_bot_token=None)
        mock_config.from_env.return_value = config

        from avito_parser.pipeline import run_flip_pipeline

        with pytest.raises(SystemExit) as exc:
            run_flip_pipeline(query="test", city="moskva", notify=True)

        assert exc.value.code == 1

    @patch("avito_parser.pipeline.TelegramNotifier")
    @patch("avito_parser.pipeline.AvitoAnalytics")
    @patch("avito_parser.pipeline.AppConfig")
    def test_filter_applied_before_alerts(
        self, mock_config, mock_analytics, mock_notifier_cls,
    ):
        """Candidates with roi_percent<=0 or flag_count>1 should be filtered out."""
        mock_config.from_env.return_value = AppConfig(telegram_bot_token="000:test", telegram_chat_id="123")
        mock_analytics_instance = mock_analytics.return_value
        mock_analytics_instance.analyze_flips.return_value = self._make_flips_result([
            _make_candidate(roi_percent=30.0, flag_count=0, title="Good"),
            _make_candidate(roi_percent=0.0, flag_count=0, title="Zero roi"),
            _make_candidate(roi_percent=20.0, flag_count=2, title="Too many flags"),
        ])

        mock_notifier = MagicMock()
        mock_notifier.send_flip_alert.return_value = True
        mock_notifier_cls.return_value = mock_notifier

        with patch("avito_parser.pipeline.BrowserParser") as mock_bp:
            instance = mock_bp.return_value
            instance.search.return_value = [MagicMock(), MagicMock(), MagicMock()]

            from avito_parser.pipeline import run_flip_pipeline
            result = run_flip_pipeline(query="test", city="moskva", notify=True)

        assert len(result) == 1
        assert result[0].title == "Good"
        assert mock_notifier.send_flip_alert.call_count == 1

    @patch("avito_parser.pipeline.TelegramNotifier")
    @patch("avito_parser.pipeline.AvitoAnalytics")
    @patch("avito_parser.pipeline.AppConfig")
    def test_no_items_returns_empty(
        self, mock_config, mock_analytics, mock_notifier_cls,
    ):
        """If browser returns no items, pipeline returns empty list."""
        mock_config.from_env.return_value = AppConfig()

        with patch("avito_parser.pipeline.BrowserParser") as mock_bp:
            instance = mock_bp.return_value
            instance.search.return_value = []  # No items

            from avito_parser.pipeline import run_flip_pipeline
            result = run_flip_pipeline(query="test", city="moskva")

        assert result == []
        mock_analytics.assert_not_called()
