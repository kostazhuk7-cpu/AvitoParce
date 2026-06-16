"""Pytest fixtures for Avito parser tests."""

from datetime import datetime, timezone
from typing import List

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from avito_parser.models import AvitoItem, RedFlag, SellerType
from avito_parser.storage import AvitoStorage
from web_app import app


@pytest_asyncio.fixture
async def storage() -> AvitoStorage:
    """In-memory SQLite storage, initialized and cleaned up."""
    db = AvitoStorage(":memory:")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def sample_items() -> List[AvitoItem]:
    """≥5 realistic AvitoItem instances for testing."""
    base_time = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return [
        AvitoItem(
            item_id=1,
            title="iPhone 16 Pro Max 256GB",
            price_rub=120000,
            description="Новый, в упаковке",
            url="https://www.avito.ru/item/1",
            publish_date=base_time,
            seller_name="Alex",
            seller_type=SellerType.PRIVATE,
            seller_rating=4.8,
            images=["https://img1.jpg"],
            condition="Новое",
            city="moskva",
        ),
        AvitoItem(
            item_id=2,
            title="iPhone 16 128GB",
            price_rub=90000,
            description="Б/у, состояние отличное",
            url="https://www.avito.ru/item/2",
            publish_date=base_time,
            seller_name="Maria",
            seller_type=SellerType.PRIVATE,
            seller_rating=4.5,
            images=["https://img2.jpg"],
            condition="Отличное",
            city="moskva",
        ),
        AvitoItem(
            item_id=3,
            title="iPhone 15 Pro 128GB",
            price_rub=70000,
            description="Трещина на экране, на запчасти",
            url="https://www.avito.ru/item/3",
            publish_date=base_time,
            seller_name="Ivan",
            seller_type=SellerType.PRIVATE,
            seller_rating=2.1,
            images=[],
            condition="Б/у",
            city="moskva",
        ),
        AvitoItem(
            item_id=4,
            title="iPhone 16 Pro Max 512GB",
            price_rub=110000,
            description="Срочно продам",
            url="https://www.avito.ru/item/4",
            publish_date=base_time,
            seller_name="Oleg",
            seller_type=SellerType.BUSINESS,
            seller_rating=4.9,
            seller_active_listings=75,
            images=["https://img4.jpg"],
            condition="Новое",
            city="moskva",
        ),
        AvitoItem(
            item_id=5,
            title="Samsung Galaxy S24 Ultra",
            price_rub=95000,
            description="Без документов",
            url="https://www.avito.ru/item/5",
            publish_date=base_time,
            seller_name="Elena",
            seller_type=SellerType.UNKNOWN,
            seller_rating=3.5,
            images=["https://img5.jpg"],
            condition="Б/у",
            city="sankt-peterburg",
        ),
        AvitoItem(
            item_id=6,
            title="iPhone 14 64GB",
            price_rub=45000,
            description="Состояние хорошее",
            url="https://www.avito.ru/item/6",
            publish_date=base_time,
            seller_name="Petr",
            seller_type=SellerType.PRIVATE,
            seller_rating=4.0,
            images=["https://img6.jpg"],
            condition="Б/у",
            city="moskva",
        ),
    ]


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient for web_app endpoints."""
    return TestClient(app)
