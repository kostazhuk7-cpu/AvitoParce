"""End-to-end integration tests for Avito Parser API.

Tests full cycle: search → analyze → save to DB → read via API → verify consistency.
All external HTTP calls are mocked — no real Avito connection.
"""

from datetime import datetime, timezone
from typing import List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from avito_parser.models import AvitoItem, RedFlag, SellerType
from avito_parser.storage import AvitoStorage
from web_app import app


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


@pytest.mark.asyncio
class TestE2EFullCycle:
    async def test_full_cycle(
        self,
        client: TestClient,
        storage: AvitoStorage,
        sample_items: List[AvitoItem],
    ):
        """Full cycle: mock search → analyze → save to DB → read via API → verify consistency."""
        original_storage = getattr(app.state, "avito_storage", None)
        app.state.avito_storage = storage

        try:
            # Step 1: Save sample items to DB (simulating analyze + save)
            await storage.save_items(sample_items)

            # Step 2: Call dashboard API (reads from DB, analyzes flips)
            response = client.get("/api/dashboard/flips?city=moskva&mode=all")
            assert response.status_code == 200
            dashboard_data = response.json()
            assert "candidates" in dashboard_data
            assert isinstance(dashboard_data["candidates"], list)

            # Step 3: Call /api/flips with mocked browser search
            with patch("web_app._browser_search_thread", return_value=sample_items):
                response = client.get(
                    "/api/flips?query=iPhone&city=moskva&mode=all&pages=1"
                )
                assert response.status_code == 200
                flips_data = response.json()
                assert "candidates" in flips_data
                assert "total_items" in flips_data
                assert flips_data["total_items"] == len(sample_items)

                # Verify candidates have expected fields
                for candidate in flips_data["candidates"]:
                    assert "title" in candidate
                    assert "buy_price" in candidate
                    assert "net_profit" in candidate
                    assert "discount_percent" in candidate
                    assert "flag_count" in candidate
                    assert candidate["flag_count"] <= 1

            # Step 4: Verify stats API reflects saved items
            response = client.get("/api/stats?city=moskva")
            assert response.status_code == 200
            stats_data = response.json()
            assert "storage" in stats_data
            assert stats_data["storage"]["total_items"] == len(
                [i for i in sample_items if i.city == "moskva"]
            )

            # Step 5: Verify trends API returns empty (no price history yet)
            response = client.get("/api/trends?query=iPhone&city=moskva&days=7")
            assert response.status_code == 200
            trends_data = response.json()
            assert "labels" in trends_data
            assert "prices" in trends_data
        finally:
            app.state.avito_storage = original_storage

    async def test_empty_result(self, client: TestClient):
        """0 candidates → API returns empty list, no crash."""
        with patch("web_app._browser_search_thread", return_value=[]):
            response = client.get(
                "/api/flips?query=nonexistent12345&city=moskva&mode=all&pages=1"
            )
            assert response.status_code == 200
            data = response.json()
            assert data["candidates"] == []
            assert data["total_items"] == 0
            assert "error" not in data

    async def test_filtering(self, client: TestClient, storage: AvitoStorage):
        """Items with all red flags get filtered correctly from profitable candidates."""
        original_storage = getattr(app.state, "avito_storage", None)
        app.state.avito_storage = storage

        try:
            # Baseline items to set market median
            baseline_high = _make_item(10, "iPhone 16 Pro Max", 100000, condition="Новое")
            baseline_mid = _make_item(11, "iPhone 16 Pro", 90000, condition="Отличное")

            # Clean item: good condition, low price, no red flags → should be candidate
            clean_item = _make_item(
                1, "iPhone 16", 40000, condition="Отличное", seller_rating=4.5
            )

            # Flagged item: many red flags → should be excluded from candidates
            flagged_item = _make_item(
                2,
                "iPhone 16",
                30000,
                condition="Б/у",
                description="срочно, без документов, на запчасти",
                images=[],
                seller_rating=2.0,
                seller_active_listings=60,
            )

            items = [baseline_high, baseline_mid, clean_item, flagged_item]
            await storage.save_items(items)

            # Call dashboard API
            response = client.get("/api/dashboard/flips?city=moskva&mode=all")
            assert response.status_code == 200
            data = response.json()
            candidates = data["candidates"]

            candidate_ids = {c["item_id"] for c in candidates}

            # Clean item should be a candidate (low price, 0 flags)
            assert 1 in candidate_ids

            # Flagged item should NOT be a candidate (too many red flags)
            assert 2 not in candidate_ids

            # Verify directly via analytics engine
            from avito_parser.analytics import AvitoAnalytics

            analyzer = AvitoAnalytics()
            result = analyzer.analyze_flips(items, query="", city="moskva")
            direct_ids = {c.item_id for c in result.candidates}
            assert 1 in direct_ids
            assert 2 not in direct_ids

            # Verify flagged item has >1 red flags
            flags = analyzer._detect_red_flags(flagged_item, median_price=95000)
            assert len(flags) > 1
        finally:
            app.state.avito_storage = original_storage

    async def test_dashboard_modes(self, client: TestClient, storage: AvitoStorage):
        """Quick and deep modes filter candidates by discount threshold."""
        original_storage = getattr(app.state, "avito_storage", None)
        app.state.avito_storage = storage

        try:
            # Create items with varying discounts
            baseline_high = _make_item(10, "iPhone 16 Pro Max", 100000, condition="Новое")
            baseline_mid = _make_item(11, "iPhone 16 Pro", 90000, condition="Отличное")
            quick_flip = _make_item(1, "iPhone 16", 60000, condition="Отличное")
            deep_flip = _make_item(2, "iPhone 16", 40000, condition="Отличное")

            items = [baseline_high, baseline_mid, quick_flip, deep_flip]
            await storage.save_items(items)

            # All mode
            response = client.get("/api/dashboard/flips?city=moskva&mode=all")
            assert response.status_code == 200
            all_candidates = response.json()["candidates"]
            assert len(all_candidates) == 2

            # Quick mode (≥20% discount)
            response = client.get("/api/dashboard/flips?city=moskva&mode=quick")
            assert response.status_code == 200
            quick_candidates = response.json()["candidates"]
            assert len(quick_candidates) >= 1
            for c in quick_candidates:
                assert c["discount_percent"] >= 20.0

            # Deep mode (≥40% discount)
            response = client.get("/api/dashboard/flips?city=moskva&mode=deep")
            assert response.status_code == 200
            deep_candidates = response.json()["candidates"]
            assert len(deep_candidates) >= 1
            for c in deep_candidates:
                assert c["discount_percent"] >= 40.0
        finally:
            app.state.avito_storage = original_storage
