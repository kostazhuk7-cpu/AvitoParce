"""Tests for CategoryDiscovery engine — mock-based, no real Avito calls."""

from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from avito_parser.analytics import AvitoAnalytics
from avito_parser.discovery import (
    CATEGORY_SLUGS,
    MIN_MEDIAN_PRICE,
    MIN_VOLATILITY,
    MAX_RED_FLAG_RATIO,
    CategoryDiscovery,
)
from avito_parser.models import AvitoItem, RedFlag, SellerType


def _make_item(
    item_id: int,
    title: str,
    price: int,
    red_flags: Optional[List[RedFlag]] = None,
    condition: str = "Б/у",
    images: Optional[List[str]] = None,
    city: str = "moskva",
) -> AvitoItem:
    """Helper to create mock AvitoItem."""
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return AvitoItem(
        item_id=item_id,
        title=title,
        price_rub=price,
        description="",
        url=f"https://www.avito.ru/item/{item_id}",
        publish_date=now,
        seller_name="Test",
        seller_type=SellerType.PRIVATE,
        seller_rating=4.0,
        images=images if images is not None else ["https://img.jpg"],
        condition=condition,
        city=city,
        red_flags=red_flags if red_flags is not None else [],
    )


def _make_items_prices(
    start_id: int,
    title_prefix: str,
    prices: List[int],
    red_flags: Optional[List[RedFlag]] = None,
) -> List[AvitoItem]:
    """Create items with given prices."""
    items = []
    for i, price in enumerate(prices):
        items.append(_make_item(start_id + i, f"{title_prefix} {i}", price, red_flags=red_flags))
    return items


@pytest.fixture
def mock_parser() -> AsyncMock:
    """Mock AvitoParser that returns items based on category slug."""
    parser = AsyncMock()
    parser.search = AsyncMock()
    return parser


@pytest.fixture
def analytics() -> AvitoAnalytics:
    """Real AvitoAnalytics instance."""
    return AvitoAnalytics()


@pytest.fixture
def discovery(mock_parser, analytics) -> CategoryDiscovery:
    """CategoryDiscovery with mock parser."""
    return CategoryDiscovery(mock_parser, analytics)


class TestCategoryDiscovery:

    @pytest.mark.asyncio
    async def test_discover_empty_categories(self, discovery, mock_parser):
        """When parser returns empty for all categories, result should be empty."""
        mock_parser.search.return_value = []
        result = await discovery.discover(city="moskva", max_categories=3)
        assert len(result.categories) == 0

    @pytest.mark.asyncio
    async def test_discover_profitable_category(self, discovery, mock_parser):
        """
        Items with wide price spread, high median, few red flags → profitable.
        Prices: 1000, 5000, 10000, 15000, 20000
        mean=10200, stdev≈7600, volatility≈0.746, median=10000
        red_flag_ratio=0/5=0 → margin≈7.46
        """
        items = _make_items_prices(1, "iPhone", [1000, 5000, 10000, 15000, 20000])
        mock_parser.search.return_value = items

        result = await discovery.discover(city="moskva", max_categories=1)
        assert len(result.categories) == 1
        cat = result.categories[0]
        assert cat.price_volatility > MIN_VOLATILITY
        assert cat.margin_potential > 0
        assert cat.item_count == 5

    @pytest.mark.asyncio
    async def test_discover_filters_low_median_price(self, discovery, mock_parser):
        """
        Items with median price below MIN_MEDIAN_PRICE (5000) → filtered out.
        Prices: 100, 200, 300, 400, 500 → median=300 < 5000
        """
        items = _make_items_prices(1, "Pen", [100, 200, 300, 400, 500])
        mock_parser.search.return_value = items

        result = await discovery.discover(city="moskva", max_categories=1)
        assert len(result.categories) == 0

    @pytest.mark.asyncio
    async def test_discover_filters_high_red_flag_ratio(self, discovery, mock_parser):
        """
        Items with red_flag_ratio >= MAX_RED_FLAG_RATIO (0.20) → filtered out.
        3 items total, 2 with red flags → ratio=0.67 > 0.20
        """
        items = [
            _make_item(1, "iPhone 16", 50000, red_flags=[RedFlag.KEYWORD_URGENT]),
            _make_item(2, "iPhone 16", 60000, red_flags=[RedFlag.SUSPICIOUS_PRICE]),
            _make_item(3, "iPhone 16", 70000),
        ]
        mock_parser.search.return_value = items

        result = await discovery.discover(city="moskva", max_categories=1)
        assert len(result.categories) == 0

    @pytest.mark.asyncio
    async def test_discover_filters_low_volatility(self, discovery, mock_parser):
        """
        Items with stable prices (low volatility) → filtered out.
        Prices: 100000, 100000, 100000 → stdev=0, volatility=0 < 0.10
        """
        items = _make_items_prices(1, "MacBook", [100000, 100000, 100000])
        mock_parser.search.return_value = items

        result = await discovery.discover(city="moskva", max_categories=1)
        assert len(result.categories) == 0

    @pytest.mark.asyncio
    async def test_discover_multiple_profitable_categories_sorted(self, discovery, mock_parser):
        """
        Two profitable categories → sorted by margin_potential descending.
        Category A: prices [5000, 10000, 15000] → vol=0.408, median=10000, margin≈4.08
        Category B: prices [100, 20000, 40000] → vol≈0.988, median=20000, margin≈19.77
        """
        items_a = _make_items_prices(1, "Guitar", [5000, 10000, 15000])
        items_b = _make_items_prices(10, "Bike", [100, 20000, 40000])

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return items_a
            return items_b

        mock_parser.search.side_effect = side_effect

        result = await discovery.discover(city="moskva", max_categories=2)
        assert len(result.categories) == 2
        assert result.categories[0].margin_potential >= result.categories[1].margin_potential
        assert result.categories[0].slug != result.categories[1].slug

    @pytest.mark.asyncio
    async def test_discover_respects_max_categories_cap(self, discovery, mock_parser):
        """max_categories larger than CATEGORY_SLUGS is capped automatically."""
        mock_parser.search.return_value = _make_items_prices(
            1, "Item", [10000, 20000, 30000]
        )
        max_slugs = len(CATEGORY_SLUGS)
        # Request more than available
        result = await discovery.discover(city="moskva", max_categories=999)
        # Should not exceed available slugs
        assert len(result.categories) <= max_slugs

    @pytest.mark.asyncio
    async def test_discover_parser_exception_skips_category(self, discovery, mock_parser):
        """If parser raises for a category, it should be skipped (no candidate)."""
        mock_parser.search.side_effect = RuntimeError("Blocked")
        result = await discovery.discover(city="moskva", max_categories=1)
        assert len(result.categories) == 0


class TestCategoryDiscoveryStatic:
    """Tests for static methods and heuristics."""

    def test_is_profitable_true(self):
        assert CategoryDiscovery._is_profitable(0.15, 10000, 0.05)

    def test_is_profitable_false_low_volatility(self):
        assert not CategoryDiscovery._is_profitable(0.05, 10000, 0.05)

    def test_is_profitable_false_low_median(self):
        assert not CategoryDiscovery._is_profitable(0.15, 3000, 0.05)

    def test_is_profitable_false_high_red_flag(self):
        assert not CategoryDiscovery._is_profitable(0.15, 10000, 0.30)

    def test_margin_potential_formula(self):
        """margin = volatility * median_price / 1000 * (1 - red_flag_ratio)"""
        result = CategoryDiscovery._compute_margin_potential(0.5, 20000, 0.1)
        expected = 0.5 * 20000 / 1000.0 * (1 - 0.1)
        assert result == pytest.approx(expected, 0.01)

    def test_margin_potential_zero_quality(self):
        """When red_flag_ratio >= 1.0, quality_factor is 0 → margin=0"""
        result = CategoryDiscovery._compute_margin_potential(0.5, 20000, 1.0)
        assert result == 0.0

    def test_catalog_slugs_count(self):
        """Ensure we have at least 10 category slugs."""
        assert len(CATEGORY_SLUGS) >= 10
