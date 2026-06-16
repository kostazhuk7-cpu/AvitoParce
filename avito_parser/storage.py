"""
Storage module for persisting Avito data to SQLite.
Supports upserts and status updates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiosqlite
from loguru import logger

from avito_parser.models import AvitoItem, RedFlag


# SQL statements
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS avito_items (
    item_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    price_rub INTEGER NOT NULL,
    description TEXT,
    url TEXT NOT NULL,
    publish_date TEXT,
    seller_name TEXT,
    seller_id TEXT,
    seller_type TEXT,
    seller_rating REAL,
    seller_registration_date TEXT,
    seller_active_listings INTEGER,
    images TEXT,
    thumbnail_url TEXT,
    condition TEXT,
    views_count INTEGER,
    today_views INTEGER,
    favorites_count INTEGER,
    city TEXT,
    district TEXT,
    address TEXT,
    category_id INTEGER,
    category_name TEXT,
    is_profitable INTEGER DEFAULT 0,
    profit_margin REAL,
    red_flags TEXT,
    market_median_price INTEGER,
    scraped_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_avito_items_city ON avito_items(city);
CREATE INDEX IF NOT EXISTS idx_avito_items_price ON avito_items(price_rub);
CREATE INDEX IF NOT EXISTS idx_avito_items_profitable ON avito_items(is_profitable);
CREATE INDEX IF NOT EXISTS idx_avito_items_scraped ON avito_items(scraped_at);
"""

UPSERT_SQL = """
INSERT INTO avito_items (
    item_id, title, price_rub, description, url, publish_date,
    seller_name, seller_id, seller_type, seller_rating,
    seller_registration_date, seller_active_listings,
    images, thumbnail_url, condition, views_count, today_views,
    favorites_count, city, district, address,
    category_id, category_name, is_profitable, profit_margin,
    red_flags, market_median_price, scraped_at, updated_at
) VALUES (
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?
)
ON CONFLICT(item_id) DO UPDATE SET
    title = excluded.title,
    price_rub = excluded.price_rub,
    description = excluded.description,
    views_count = excluded.views_count,
    today_views = excluded.today_views,
    favorites_count = excluded.favorites_count,
    is_profitable = excluded.is_profitable,
    profit_margin = excluded.profit_margin,
    condition = excluded.condition,
    red_flags = excluded.red_flags,
    market_median_price = excluded.market_median_price,
    updated_at = excluded.updated_at
"""

PRICE_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    title TEXT,
    price_rub INTEGER NOT NULL,
    url TEXT,
    search_query TEXT,
    city TEXT,
    seller_name TEXT,
    condition TEXT,
    red_flags TEXT,
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_item_id ON price_history(item_id);
CREATE INDEX IF NOT EXISTS idx_price_history_query ON price_history(search_query);
"""

SAVE_SNAPSHOT_SQL = """
INSERT INTO price_history (item_id, title, price_rub, url, search_query, city, seller_name, condition, red_flags, scraped_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SELECT_ALL_SQL = "SELECT * FROM avito_items WHERE city = ? ORDER BY scraped_at DESC"

SELECT_PROFITABLE_SQL = """
SELECT * FROM avito_items 
WHERE city = ? AND is_profitable = 1 
ORDER BY profit_margin DESC
"""

SELECT_BY_ID_SQL = "SELECT * FROM avito_items WHERE item_id = ?"

UPDATE_STATUS_SQL = """
UPDATE avito_items 
SET is_profitable = ?, profit_margin = ?, red_flags = ?, updated_at = ?
WHERE item_id = ?
"""


class AvitoStorage:
    """
    SQLite storage for Avito items.
    Supports upserts and status updates.
    """

    def __init__(self, db_path: str = "data/avito.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Initialize database connection and create tables."""
        # Ensure directory exists
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Create tables
        await self._db.executescript(CREATE_TABLE_SQL)
        await self._db.executescript(CREATE_INDEX_SQL)
        await self._db.executescript(PRICE_HISTORY_TABLE_SQL)
        await self._db.commit()

        logger.info(f"Storage initialized: {self._db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def save_items(self, items: List[AvitoItem]) -> int:
        """
        Save or update items in database atomically.

        All items are saved in a single transaction. If ANY item fails,
        the entire batch is rolled back and the exception is re-raised.

        Args:
            items: List of AvitoItem objects

        Returns:
            Number of items saved

        Raises:
            RuntimeError: If storage not initialized
            Exception: On database error (transaction is rolled back)
        """
        if not self._db:
            raise RuntimeError("Storage not initialized. Call initialize() first.")

        now = datetime.now(timezone.utc).isoformat()
        saved_count = 0

        await self._db.execute("BEGIN TRANSACTION")

        try:
            for item in items:
                images_json = json.dumps(item.images)
                red_flags_json = json.dumps([f.value for f in item.red_flags])

                await self._db.execute(
                    UPSERT_SQL,
                    (
                        item.item_id,
                        item.title,
                        item.price_rub,
                        item.description,
                        item.url,
                        item.publish_date.isoformat() if item.publish_date else None,
                        item.seller_name,
                        item.seller_id,
                        item.seller_type.value if item.seller_type else None,
                        item.seller_rating,
                        item.seller_registration_date.isoformat() if item.seller_registration_date else None,
                        item.seller_active_listings,
                        images_json,
                        item.thumbnail_url,
                        item.condition,
                        item.views_count,
                        item.today_views,
                        item.favorites_count,
                        item.city,
                        item.district,
                        item.address,
                        item.category_id,
                        item.category_name,
                        1 if item.is_profitable else 0,
                        item.profit_margin,
                        red_flags_json,
                        item.market_median_price,
                        item.scraped_at.isoformat() if item.scraped_at else now,
                        now,
                    ),
                )
                saved_count += 1

            await self._db.commit()
            logger.info(f"Saved {saved_count} items to database")
            return saved_count

        except Exception as e:
            await self._db.execute("ROLLBACK")
            logger.error(f"Failed to save items batch, rolling back: {e}")
            raise

    async def get_items_by_city(self, city: str) -> List[AvitoItem]:
        """Get all items for a city."""
        if not self._db:
            raise RuntimeError("Storage not initialized.")

        cursor = await self._db.execute(SELECT_ALL_SQL, (city,))
        rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def get_profitable_items(self, city: str) -> List[AvitoItem]:
        """Get profitable items for a city."""
        if not self._db:
            raise RuntimeError("Storage not initialized.")

        cursor = await self._db.execute(SELECT_PROFITABLE_SQL, (city,))
        rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def get_item_by_id(self, item_id: int) -> Optional[AvitoItem]:
        """Get single item by ID."""
        if not self._db:
            raise RuntimeError("Storage not initialized.")

        cursor = await self._db.execute(SELECT_BY_ID_SQL, (item_id,))
        row = await cursor.fetchone()
        if row:
            return self._row_to_item(row)
        return None

    async def update_item_status(
        self,
        item_id: int,
        is_profitable: bool,
        profit_margin: Optional[float],
        red_flags: List[RedFlag],
    ) -> None:
        """Update item analysis status."""
        if not self._db:
            raise RuntimeError("Storage not initialized.")

        now = datetime.now(timezone.utc).isoformat()
        red_flags_json = json.dumps([f.value for f in red_flags])

        await self._db.execute(
            UPDATE_STATUS_SQL,
            (
                1 if is_profitable else 0,
                profit_margin,
                red_flags_json,
                now,
                item_id,
            ),
        )
        await self._db.commit()

    async def save_price_snapshots(self, items: List[AvitoItem], search_query: str, city: str) -> int:
        """Save price snapshots atomically."""
        if not self._db:
            raise RuntimeError("Storage not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        count = 0

        await self._db.execute("BEGIN TRANSACTION")

        try:
            for item in items:
                red_flags_json = json.dumps([f.value for f in item.red_flags])
                await self._db.execute(SAVE_SNAPSHOT_SQL, (
                    item.item_id, item.title, item.price_rub, item.url,
                    search_query, city, item.seller_name, item.condition or "",
                    red_flags_json, now,
                ))
                count += 1

            await self._db.commit()
            return count

        except Exception as e:
            await self._db.execute("ROLLBACK")
            logger.error(f"Failed to save price snapshots batch, rolling back: {e}")
            raise

    async def get_price_history(self, item_id: int) -> list[dict]:
        if not self._db:
            raise RuntimeError("Storage not initialized.")
        cursor = await self._db.execute(
            "SELECT price_rub, scraped_at FROM price_history WHERE item_id = ? ORDER BY scraped_at ASC", (item_id,))
        rows = await cursor.fetchall()
        return [{"price": row["price_rub"], "date": row["scraped_at"]} for row in rows]

    async def get_search_sessions(self) -> list[dict]:
        if not self._db:
            raise RuntimeError("Storage not initialized.")
        cursor = await self._db.execute("""
            SELECT search_query, city, COUNT(*) as items, 
                   MIN(price_rub) as min_price, MAX(price_rub) as max_price,
                   MIN(scraped_at) as first_seen
            FROM price_history 
            GROUP BY search_query, city 
            ORDER BY first_seen DESC
            LIMIT 50
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_session_items(self, search_query: str, city: str) -> list[dict]:
        if not self._db:
            raise RuntimeError("Storage not initialized.")
        cursor = await self._db.execute("""
            SELECT item_id, title, price_rub, url, seller_name, condition, scraped_at
            FROM price_history 
            WHERE search_query = ? AND city = ?
            ORDER BY scraped_at DESC
        """, (search_query, city))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    def _row_to_item(self, row: aiosqlite.Row) -> AvitoItem:
        """Convert database row to AvitoItem."""
        # Parse JSON fields
        images = json.loads(row["images"]) if row["images"] else []
        red_flags = [
            RedFlag(f) for f in json.loads(row["red_flags"]) if row["red_flags"]
        ]

        # Parse datetime fields
        publish_date = None
        if row["publish_date"]:
            try:
                publish_date = datetime.fromisoformat(row["publish_date"])
            except ValueError:
                pass

        scraped_at = datetime.now(timezone.utc)
        if row["scraped_at"]:
            try:
                scraped_at = datetime.fromisoformat(row["scraped_at"])
            except ValueError:
                pass

        return AvitoItem(
            item_id=row["item_id"],
            title=row["title"],
            price_rub=row["price_rub"],
            description=row["description"] or "",
            url=row["url"],
            publish_date=publish_date or scraped_at,
            seller_name=row["seller_name"] or "Unknown",
            seller_id=row["seller_id"],
            seller_type=row["seller_type"] or "unknown",
            seller_rating=row["seller_rating"],
            seller_active_listings=row["seller_active_listings"],
            images=images,
            thumbnail_url=row["thumbnail_url"],
            condition=row["condition"],
            views_count=row["views_count"],
            today_views=row["today_views"],
            favorites_count=row["favorites_count"],
            city=row["city"] or "",
            district=row["district"],
            address=row["address"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            is_profitable=bool(row["is_profitable"]),
            profit_margin=row["profit_margin"],
            red_flags=red_flags,
            market_median_price=row["market_median_price"],
            scraped_at=scraped_at,
        )

    async def get_stats(self, city: str) -> dict:
        """Get storage statistics for a city."""
        if not self._db:
            raise RuntimeError("Storage not initialized.")

        cursor = await self._db.execute(
            """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN is_profitable = 1 THEN 1 ELSE 0 END) as profitable,
                AVG(price_rub) as avg_price,
                MIN(price_rub) as min_price,
                MAX(price_rub) as max_price
            FROM avito_items
            WHERE city = ?
            """,
            (city,),
        )
        row = await cursor.fetchone()
        return {
            "total_items": row["total"],
            "profitable_items": row["profitable"],
            "avg_price": int(row["avg_price"] or 0),
            "min_price": row["min_price"],
            "max_price": row["max_price"],
        }
