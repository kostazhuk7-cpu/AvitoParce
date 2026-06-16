"""Tests for AvitoStorage CRUD operations."""

import json
from datetime import datetime, timezone
from typing import List
from unittest.mock import patch

import pytest

from avito_parser.models import AvitoItem, RedFlag, SellerType
from avito_parser.storage import AvitoStorage


def _make_item(
    item_id: int,
    title: str,
    price: int,
    city: str = "moskva",
    is_profitable: bool = False,
    profit_margin: float = None,
    red_flags: List[RedFlag] = None,
    condition: str = "Б/у",
) -> AvitoItem:
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return AvitoItem(
        item_id=item_id,
        title=title,
        price_rub=price,
        description="test",
        url=f"https://www.avito.ru/item/{item_id}",
        publish_date=now,
        seller_name="Test",
        seller_type=SellerType.PRIVATE,
        images=["https://img.jpg"],
        condition=condition,
        city=city,
        is_profitable=is_profitable,
        profit_margin=profit_margin,
        red_flags=red_flags or [],
        scraped_at=now,
    )


@pytest.mark.asyncio
class TestUpsert:
    async def test_save_new_item(self, storage: AvitoStorage):
        item = _make_item(1, "iPhone 16", 100000)
        count = await storage.save_items([item])
        assert count == 1
        retrieved = await storage.get_item_by_id(1)
        assert retrieved is not None
        assert retrieved.title == "iPhone 16"
        assert retrieved.price_rub == 100000

    async def test_save_updates_existing(self, storage: AvitoStorage):
        item = _make_item(1, "iPhone 16", 100000)
        await storage.save_items([item])
        updated = _make_item(1, "iPhone 16 Pro", 120000)
        count = await storage.save_items([updated])
        assert count == 1
        retrieved = await storage.get_item_by_id(1)
        assert retrieved.title == "iPhone 16 Pro"
        assert retrieved.price_rub == 120000

    async def test_save_multiple_items(self, storage: AvitoStorage):
        items = [
            _make_item(1, "A", 10000),
            _make_item(2, "B", 20000),
            _make_item(3, "C", 30000),
        ]
        count = await storage.save_items(items)
        assert count == 3


@pytest.mark.asyncio
class TestGetById:
    async def test_get_existing(self, storage: AvitoStorage):
        item = _make_item(42, "MacBook Pro", 200000)
        await storage.save_items([item])
        retrieved = await storage.get_item_by_id(42)
        assert retrieved is not None
        assert retrieved.item_id == 42

    async def test_get_missing_returns_none(self, storage: AvitoStorage):
        result = await storage.get_item_by_id(999)
        assert result is None


@pytest.mark.asyncio
class TestGetProfitable:
    async def test_get_profitable_items(self, storage: AvitoStorage):
        items = [
            _make_item(1, "Cheap", 50000, is_profitable=True, profit_margin=0.30),
            _make_item(2, "Expensive", 150000, is_profitable=False),
        ]
        await storage.save_items(items)
        profitable = await storage.get_profitable_items("moskva")
        assert len(profitable) == 1
        assert profitable[0].item_id == 1

    async def test_get_profitable_empty(self, storage: AvitoStorage):
        items = [
            _make_item(1, "Expensive", 150000, is_profitable=False),
        ]
        await storage.save_items(items)
        profitable = await storage.get_profitable_items("moskva")
        assert profitable == []

    async def test_get_profitable_wrong_city(self, storage: AvitoStorage):
        item = _make_item(1, "Cheap", 50000, city="sankt-peterburg", is_profitable=True)
        await storage.save_items([item])
        profitable = await storage.get_profitable_items("moskva")
        assert profitable == []


@pytest.mark.asyncio
class TestPriceHistory:
    async def test_save_and_get_price_history(self, storage: AvitoStorage):
        item = _make_item(1, "iPhone 16", 100000)
        await storage.save_price_snapshots([item], search_query="iphone", city="moskva")
        history = await storage.get_price_history(1)
        assert len(history) == 1
        assert history[0]["price"] == 100000

    async def test_multiple_snapshots(self, storage: AvitoStorage):
        item1 = _make_item(1, "iPhone 16", 100000)
        item2 = _make_item(1, "iPhone 16", 95000)
        await storage.save_price_snapshots([item1], search_query="iphone", city="moskva")
        await storage.save_price_snapshots([item2], search_query="iphone", city="moskva")
        history = await storage.get_price_history(1)
        assert len(history) == 2
        prices = [h["price"] for h in history]
        assert prices == [100000, 95000]


@pytest.mark.asyncio
class TestGetStats:
    async def test_get_search_sessions(self, storage: AvitoStorage):
        items = [
            _make_item(1, "A", 10000),
            _make_item(2, "B", 20000),
        ]
        await storage.save_price_snapshots(items, search_query="iphone", city="moskva")
        sessions = await storage.get_search_sessions()
        assert len(sessions) == 1
        assert sessions[0]["search_query"] == "iphone"
        assert sessions[0]["city"] == "moskva"
        assert sessions[0]["items"] == 2

    async def test_get_session_items(self, storage: AvitoStorage):
        items = [
            _make_item(1, "A", 10000),
            _make_item(2, "B", 20000),
        ]
        await storage.save_price_snapshots(items, search_query="iphone", city="moskva")
        session_items = await storage.get_session_items("iphone", "moskva")
        assert len(session_items) == 2
        ids = {i["item_id"] for i in session_items}
        assert ids == {1, 2}

    async def test_get_session_items_wrong_query(self, storage: AvitoStorage):
        items = [_make_item(1, "A", 10000)]
        await storage.save_price_snapshots(items, search_query="iphone", city="moskva")
        session_items = await storage.get_session_items("samsung", "moskva")
        assert session_items == []


@pytest.mark.asyncio
class TestAtomicTransaction:
    """Verify save_items rolls back all changes on partial failure."""

    async def test_save_items_rollback_on_error(self, storage: AvitoStorage):
        # Save initial batch successfully
        items = [
            _make_item(1, "Item 1", 10000),
            _make_item(2, "Item 2", 20000),
        ]
        count = await storage.save_items(items)
        assert count == 2

        cursor = await storage._db.execute("SELECT COUNT(*) FROM avito_items")
        row = await cursor.fetchone()
        assert row[0] == 2

        # Try batch where json.dumps fails on 3rd call
        # (2 calls per item: images + red_flags → 3rd = second item's images)
        items2 = [
            _make_item(3, "Item 3", 30000),
            _make_item(4, "Item 4", 40000),
        ]

        call_count = [0]

        def fail_on_third(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise ValueError("Simulated serialization error")
            return json.dumps(*args, **kwargs)

        with patch("avito_parser.storage.json.dumps", side_effect=fail_on_third):
            with pytest.raises(ValueError, match="Simulated serialization error"):
                await storage.save_items(items2)

        # Verify rollback: still only 2 original items
        cursor = await storage._db.execute("SELECT COUNT(*) FROM avito_items")
        row = await cursor.fetchone()
        assert row[0] == 2, (
            f"Expected 2 items after rollback, got {row[0]}. "
            "save_items must rollback entire batch on any error."
        )
