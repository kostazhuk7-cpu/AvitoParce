"""Tests for AvitoAnalytics engine."""

from datetime import datetime, timezone
from typing import List

import pytest

from avito_parser.analytics import AvitoAnalytics
from avito_parser.models import AvitoItem, RedFlag, SellerType


def _make_item(
    item_id: int,
    title: str,
    price: int,
    condition: str = "Б/у",
    description: str = "",
    images: List[str] = None,
    seller_type: SellerType = SellerType.PRIVATE,
    seller_rating: float = 4.0,
    seller_active_listings: int = None,
    city: str = "moskva",
) -> AvitoItem:
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return AvitoItem(
        item_id=item_id,
        title=title,
        price_rub=price,
        description=description,
        url=f"https://www.avito.ru/item/{item_id}",
        publish_date=now,
        seller_name="Test",
        seller_type=seller_type,
        seller_rating=seller_rating,
        seller_active_listings=seller_active_listings,
        images=images if images is not None else ["https://img.jpg"],
        condition=condition,
        city=city,
    )


class TestAnalyze:
    def test_analyze_empty_items(self):
        analyzer = AvitoAnalytics()
        result = analyzer.analyze([], query="iphone", city="moskva")
        assert result.total_items == 0
        assert result.median_price == 0
        assert result.profitable_items == []

    def test_analyze_normal_items(self):
        analyzer = AvitoAnalytics(profit_margin=0.30)
        items = [
            _make_item(1, "iPhone 16 Pro Max", 120000, condition="Новое"),
            _make_item(2, "iPhone 16 Pro", 100000, condition="Отличное"),
            _make_item(3, "iPhone 16", 50000, condition="Б/у"),
            _make_item(4, "iPhone 16 Plus", 90000, condition="Б/у"),
        ]
        result = analyzer.analyze(items, query="iPhone 16", city="moskva")
        assert result.total_items == 4
        assert result.median_price == 95000
        assert result.profitable_threshold == int(95000 * 0.70)
        assert len(result.profitable_items) == 1
        assert result.profitable_items[0].item_id == 3
        assert result.profitable_items[0].profit_margin == pytest.approx((95000 - 50000) / 95000, 0.001)

    def test_analyze_outliers_low_price(self):
        analyzer = AvitoAnalytics(profit_margin=0.30)
        items = [
            _make_item(1, "iPhone 16 Pro Max", 120000, condition="Новое"),
            _make_item(2, "iPhone 16 Pro", 100000, condition="Отличное"),
            _make_item(3, "iPhone 16", 40000, condition="Б/у", description="срочно"),
        ]
        result = analyzer.analyze(items, query="iPhone 16", city="moskva")
        assert result.total_items == 3
        assert result.median_price == 100000
        assert len(result.profitable_items) == 1
        assert result.profitable_items[0].item_id == 3
        assert any(f == RedFlag.SUSPICIOUS_PRICE for f in result.profitable_items[0].red_flags)

    def test_analyze_no_relevant_items(self):
        analyzer = AvitoAnalytics()
        items = [
            _make_item(1, "Samsung Galaxy S24", 80000),
            _make_item(2, "Xiaomi 14", 50000),
        ]
        result = analyzer.analyze(items, query="iPhone 16", city="moskva")
        assert result.total_items == 2
        assert result.median_price == 0
        assert result.profitable_items == []

    def test_analyze_red_flags_summary(self):
        analyzer = AvitoAnalytics(profit_margin=0.30)
        items = [
            _make_item(1, "iPhone 16", 120000, condition="Новое"),
            _make_item(2, "iPhone 16", 80000, condition="Б/у", description="срочно продам"),
            _make_item(3, "iPhone 16", 70000, condition="Б/у", description="без документов", images=[]),
        ]
        result = analyzer.analyze(items, query="iPhone 16", city="moskva")
        assert result.red_flags_summary.get("keyword_urgent", 0) >= 1
        assert result.red_flags_summary.get("keyword_no_docs", 0) >= 1


class TestAnalyzeFlips:
    def test_analyze_flips_empty(self):
        analyzer = AvitoAnalytics()
        result = analyzer.analyze_flips([], query="iphone", city="moskva")
        assert result.total_items == 0
        assert result.candidates == []

    def test_analyze_flips_candidates(self):
        analyzer = AvitoAnalytics(profit_margin=0.30)
        items = [
            _make_item(1, "iPhone 16 Pro Max", 120000, condition="Новое"),
            _make_item(2, "iPhone 16 Pro", 100000, condition="Отличное"),
            _make_item(3, "iPhone 16", 60000, condition="Отличное"),
            _make_item(4, "iPhone 16", 55000, condition="Б/у"),
        ]
        result = analyzer.analyze_flips(items, query="iPhone 16", city="moskva")
        assert result.total_items == 4
        assert result.median_price == 80000
        assert len(result.candidates) >= 1
        for c in result.candidates:
            assert c.buy_price < result.median_price
            assert c.condition in ("Новое", "Отличное", "Б/у")
            assert c.net_profit > 0

    def test_analyze_flips_no_good_condition(self):
        analyzer = AvitoAnalytics()
        items = [
            _make_item(1, "iPhone 16", 50000, condition="Убитое"),
        ]
        result = analyzer.analyze_flips(items, query="iPhone 16", city="moskva")
        assert result.candidates == []

    def test_analyze_flips_red_flags_filter(self):
        analyzer = AvitoAnalytics()
        items = [
            _make_item(1, "iPhone 16 Pro Max", 120000, condition="Новое"),
            _make_item(2, "iPhone 16", 60000, condition="Отличное", description="срочно", seller_active_listings=60),
        ]
        result = analyzer.analyze_flips(items, query="iPhone 16", city="moskva")
        assert result.total_items == 2
        assert all(c.flag_count <= 1 for c in result.candidates)


class TestDetectRedFlags:
    def test_suspicious_price(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 30000)
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.SUSPICIOUS_PRICE in flags

    def test_no_suspicious_price_when_normal(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 80000)
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.SUSPICIOUS_PRICE not in flags

    def test_keyword_urgent(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, description="срочно продам")
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.KEYWORD_URGENT in flags

    def test_keyword_damaged(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, description="после аварии")
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.KEYWORD_DAMAGED in flags

    def test_keyword_no_docs(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, description="без документов")
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.KEYWORD_NO_DOCS in flags

    def test_keyword_parts(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, description="на запчасти")
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.KEYWORD_PARTS in flags

    def test_no_photos(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, images=[])
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.NO_PHOTOS in flags

    def test_new_account_never_triggered(self):
        analyzer = AvitoAnalytics()
        for st in (SellerType.PRIVATE, SellerType.BUSINESS, SellerType.UNKNOWN):
            item = _make_item(1, "iPhone 16", 50000, seller_type=st)
            flags = analyzer._detect_red_flags(item, median_price=100000)
            assert RedFlag.NEW_ACCOUNT not in flags

    def test_low_rating(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, seller_rating=2.5)
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.LOW_RATING in flags

    def test_bulk_seller(self):
        analyzer = AvitoAnalytics()
        item = _make_item(1, "iPhone 16", 50000, seller_active_listings=60)
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert RedFlag.BULK_SELLER in flags

    def test_multiple_flags(self):
        analyzer = AvitoAnalytics()
        item = _make_item(
            1,
            "iPhone 16",
            30000,
            description="срочно, без документов",
            images=[],
            seller_rating=2.0,
            seller_active_listings=60,
        )
        flags = analyzer._detect_red_flags(item, median_price=100000)
        assert len(flags) >= 4
        assert RedFlag.SUSPICIOUS_PRICE in flags
        assert RedFlag.KEYWORD_URGENT in flags
        assert RedFlag.KEYWORD_NO_DOCS in flags
        assert RedFlag.NO_PHOTOS in flags
        assert RedFlag.LOW_RATING in flags
        assert RedFlag.BULK_SELLER in flags
