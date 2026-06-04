"""
Avito Parser - Core parsing logic for extracting listings.
Handles both SSR HTML parsing and API data extraction.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

from loguru import logger
from selectolax.parser import HTMLParser

from avito_parser.client import AvitoHttpClient, CaptchaRedirectError
from avito_parser.config import AppConfig, SearchConfig
from avito_parser.models import (
    AvitoItem,
    RedFlag,
    SearchParams,
    SellerType,
    SortOption,
)

# Avito mobile API key (static, same for all users)
AVITO_API_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

# Avito API endpoints
API_BASE = "https://m.avito.ru/api"
API_SEARCH = f"{API_BASE}/11/items"
API_ITEM_DETAIL = f"{API_BASE}/15/items"
API_WEB_ITEMS = "https://www.avito.ru/web/1/js/items"

# Red flag keywords (Russian)
RED_FLAG_KEYWORDS_URGENT = [
    "срочно", "быстро", "немедленно", "в течение дня", "сегодня",
    "распродажа", "liquidation", "скидка", "дешево",
]
RED_FLAG_KEYWORDS_DAMAGED = [
    "битый", "после аварии", "не работает", "на запчасти",
    "ремонт", "восстановление", "дефект", "трещина",
]
RED_FLAG_KEYWORDS_NO_DOCS = [
    "без документов", "без ПТС", "без СТС", "без договора",
    "без чека", "без гарантии",
]


class AvitoParser:
    """
    Main parser for Avito listings.
    Supports both SSR HTML and mobile API parsing.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = AvitoHttpClient(config)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure client is initialized (fetches free proxies)."""
        if not self._initialized:
            await self._client.initialize()
            self._initialized = True

    async def search(
        self,
        search_config: SearchConfig,
        max_pages: Optional[int] = None,
    ) -> List[AvitoItem]:
        """
        Search Avito listings with automatic retry on captcha.

        Args:
            search_config: Search parameters
            max_pages: Override max pages to scan

        Returns:
            List of parsed AvitoItem objects
        """
        # Ensure client is initialized (fetches free proxies)
        await self._ensure_initialized()

        pages_to_scan = max_pages or search_config.pages
        all_items: List[AvitoItem] = []
        context_token: Optional[str] = None

        logger.info(
            f"Starting search: city={search_config.city}, "
            f"category={search_config.category}, "
            f"query={search_config.query}, pages={pages_to_scan}"
        )

        # Retry loop for captcha/block on first page
        max_captcha_retries = 3
        for captcha_attempt in range(max_captcha_retries):
            try:
                for page in range(1, pages_to_scan + 1):
                    try:
                        if page == 1:
                            # First page: fetch SSR HTML
                            items, context_token, total_count = await self._parse_first_page(
                                search_config
                            )
                            logger.info(f"Page 1: found {len(items)} items (total: {total_count})")
                        else:
                            # Subsequent pages: use API with context token
                            if not context_token:
                                logger.warning(f"No context token for page {page}, stopping")
                                break

                            items, context_token = await self._parse_api_page(
                                search_config, context_token, page
                            )
                            logger.info(f"Page {page}: found {len(items)} items")

                    except (CaptchaRedirectError, Exception) as e:
                        if page == 1:
                            # Block/captcha on first page — burn session, retry
                            logger.warning(
                                f"Block on page 1 ({type(e).__name__}: {e}) "
                                f"(attempt {captcha_attempt + 1}/{max_captcha_retries}), "
                                "rotating session..."
                            )
                            self._client._session = None
                            await asyncio.sleep(random.uniform(3, 8))
                            break  # Break inner loop to retry first page
                        else:
                            # Block on later page — stop pagination, keep what we have
                            logger.warning(f"Block on page {page}, keeping collected items")
                            break

                else:
                    # Inner loop completed without break (no block) — exit retry loop
                    break

                # If we broke out of inner loop due to block on page 1,
                # check if we got items from previous attempts
                if all_items:
                    break

            except (CaptchaRedirectError, Exception) as e:
                logger.warning(f"Block retry {captcha_attempt + 1}/{max_captcha_retries}: {e}")
                self._client._session = None
                await asyncio.sleep(random.uniform(3, 8))
                continue

        # Filter out items with 0 price (likely parsing artifacts)
        valid_items = [item for item in all_items if item.price_rub > 0]

        logger.info(
            f"Search complete: {len(valid_items)} valid items "
            f"(of {len(all_items)} total parsed)"
        )
        return valid_items

    async def _parse_first_page(
        self, search_config: SearchConfig
    ) -> Tuple[List[AvitoItem], Optional[str], int]:
        """
        Parse first page of search results (SSR HTML).

        Returns:
            Tuple of (items, context_token, total_count)
        """
        # Build URL
        params = SearchParams(
            city=search_config.city,
            category=search_config.category,
            query=search_config.query,
            sort=SortOption.DATE if search_config.sort_by_date else SortOption.RELEVANCE,
            min_price=search_config.min_price,
            max_price=search_config.max_price,
            radius=search_config.radius,
            private_only=search_config.private_only,
        )
        url = params.build_url()

        # Fetch page
        response = await self._client.get(url)
        html_content = response.text

        # Parse HTML
        tree = HTMLParser(html_content)

        # Extract JSON data from embedded script
        json_data = self._extract_json_from_html(html_content)
        context_token = json_data.get("context")
        total_count = json_data.get("catalog", {}).get("totalCount", 0)

        # Parse items from JSON data
        items = self._parse_items_from_json(json_data, search_config.city)

        # Fallback: parse from DOM if JSON extraction failed
        if not items:
            items = self._parse_items_from_html(tree, search_config.city)

        # Detect captcha/challenge page (200 with 0 items and captcha in body)
        if not items:
            text_lower = html_content[:5000].lower()
            if "captcha" in text_lower or "recaptcha" in text_lower:
                logger.warning("Captcha page detected (empty items + captcha in body)")
                # Raise to trigger retry with new session in search()
                from avito_parser.client import CaptchaRedirectError
                raise CaptchaRedirectError("Captcha page detected (empty items)")

        return items, context_token, total_count

    async def _parse_api_page(
        self,
        search_config: SearchConfig,
        context_token: str,
        page: int,
    ) -> Tuple[List[AvitoItem], Optional[str]]:
        """
        Parse subsequent pages using Avito API.

        Returns:
            Tuple of (items, next_context_token)
        """
        # Build API request
        params = {
            "p": str(page),
            "context": context_token,
            "updateListOnly": "true",
        }

        response = await self._client.get(API_WEB_ITEMS, params=params)
        data = response.json()

        # Extract items and next context
        items_data = data.get("catalog", {}).get("items", [])
        next_context = data.get("context")

        items = self._parse_items_from_api_data(items_data, search_config.city)

        return items, next_context

    def _extract_json_from_html(self, html_content: str) -> Dict[str, Any]:
        """
        Extract embedded JSON data from Avito HTML page.
        Avito embeds data in <script type="mime/invalid" data-mfe-state="true">
        """
        tree = HTMLParser(html_content)

        # Find the script tag with mfe-state data
        for script in tree.css('script[type="mime/invalid"]'):
            if script.attributes.get("data-mfe-state") == "true":
                text = script.text()
                if not text or "sandbox" in text:
                    continue

                try:
                    # Unescape HTML entities
                    unescaped = html_lib.unescape(text)
                    data = json.loads(unescaped)

                    # Navigate to the data we need
                    state = data.get("state", {})
                    return state.get("data", {})
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug(f"Failed to parse embedded JSON: {e}")
                    continue

        return {}

    def _parse_items_from_json(
        self, json_data: Dict[str, Any], default_city: str
    ) -> List[AvitoItem]:
        """Parse AvitoItem list from embedded JSON data."""
        items = []
        catalog_items = json_data.get("catalog", {}).get("items", [])

        for item_data in catalog_items:
            try:
                item = self._parse_single_item(item_data, default_city)
                if item:
                    items.append(item)
            except Exception as e:
                logger.debug(f"Failed to parse item: {e}")
                continue

        return items

    def _parse_items_from_api_data(
        self, items_data: List[Dict[str, Any]], default_city: str
    ) -> List[AvitoItem]:
        """Parse AvitoItem list from API response data."""
        items = []
        for item_data in items_data:
            try:
                item = self._parse_single_item(item_data, default_city)
                if item:
                    items.append(item)
            except Exception as e:
                logger.debug(f"Failed to parse API item: {e}")
                continue
        return items

    def _parse_single_item(
        self, data: Dict[str, Any], default_city: str
    ) -> Optional[AvitoItem]:
        """Parse a single AvitoItem from data dictionary.

        Handles None values gracefully — Avito JSON often has keys with None
        values (not missing keys), so dict.get(key, default) returns None
        instead of the default.
        """
        try:
            # Extract item ID
            item_id = data.get("id")
            if not item_id:
                return None

            # Extract title
            title = (data.get("title") or "").strip()
            if not title:
                return None

            # Extract price — priceDetailed may be None, {} or missing
            price_data = data.get("priceDetailed") or {}
            price_value = price_data.get("value") or 0
            if isinstance(price_value, str):
                price_value = int("".join(c for c in price_value if c.isdigit()) or "0")

            # Extract URL
            url_path = data.get("urlPath") or data.get("url") or ""
            url = f"https://www.avito.ru{url_path}" if url_path.startswith("/") else url_path

            # Extract publish date
            timestamp_ms = data.get("sortTimeStamp") or 0
            if timestamp_ms:
                publish_date = datetime.fromtimestamp(
                    timestamp_ms / 1000, tz=timezone.utc
                )
            else:
                publish_date = datetime.now(timezone.utc)

            # Extract images — may be None or empty
            images = []
            images_data = data.get("images") or []
            for img in images_data:
                if isinstance(img, dict):
                    img_url = img.get("imageUrl") or ""
                    if img_url:
                        images.append(img_url)

            # Extract location — may be None
            location = data.get("location") or {}
            city = location.get("name") or default_city

            # Extract seller info
            seller_id = data.get("sellerId")
            seller_name = data.get("sellerName") or "Unknown"

            # Extract description
            description = data.get("description") or ""

            # Build item
            item = AvitoItem(
                item_id=int(item_id),
                title=title,
                price_rub=int(price_value),
                description=description,
                url=url,
                publish_date=publish_date,
                seller_name=seller_name,
                seller_id=str(seller_id) if seller_id else None,
                images=images,
                city=city,
                thumbnail_url=data.get("thumbnailUrl"),
            )

            return item

        except Exception as e:
            logger.debug(f"Item parse error: {e}")
            return None

    def _parse_items_from_html(
        self, tree: HTMLParser, default_city: str
    ) -> List[AvitoItem]:
        """
        Fallback: parse items directly from HTML DOM.
        Used when JSON extraction fails.
        """
        items = []

        # Find item containers
        for item_node in tree.css('[data-marker="item"]'):
            try:
                item = self._parse_item_from_dom(item_node, default_city)
                if item:
                    items.append(item)
            except Exception as e:
                logger.debug(f"DOM parse error: {e}")
                continue

        return items

    def _parse_item_from_dom(
        self, node: HTMLParser, default_city: str
    ) -> Optional[AvitoItem]:
        """Parse a single item from DOM node."""
        # Extract item ID
        item_id_str = node.attributes.get("data-item-id", "")
        if not item_id_str:
            return None
        item_id = int(item_id_str)

        # Extract title
        title_node = node.css_first('[itemprop="name"]')
        if not title_node:
            title_node = node.css_first(".snippet-title")
        title = title_node.text(strip=True) if title_node else ""
        if not title:
            return None

        # Extract price
        price_node = node.css_first('[itemprop="price"]')
        if not price_node:
            price_node = node.css_first(".snippet-price-row")
        price_text = price_node.text(strip=True) if price_node else "0"
        price = int("".join(c for c in price_text if c.isdigit()) or "0")

        # Extract URL
        link_node = node.css_first('a[itemprop="url"]')
        if not link_node:
            link_node = node.css_first(".snippet-link")
        url_path = link_node.attributes.get("href", "") if link_node else ""
        url = f"https://www.avito.ru{url_path}" if url_path.startswith("/") else url_path

        # Extract date
        date_node = node.css_first('[data-marker="item-date"]')
        date_text = date_node.text(strip=True) if date_node else ""
        publish_date = self._parse_relative_date(date_text)

        # Extract images
        images = []
        for img in node.css("img"):
            src = img.attributes.get("src", "")
            if src and "avito" in src:
                images.append(src)

        # Extract seller
        seller_node = node.css_first('[data-marker="seller-info"]')
        seller_name = seller_node.text(strip=True) if seller_node else "Unknown"

        return AvitoItem(
            item_id=item_id,
            title=title,
            price_rub=price,
            url=url,
            publish_date=publish_date,
            seller_name=seller_name,
            images=images,
            city=default_city,
        )

    def _parse_relative_date(self, date_text: str) -> datetime:
        """Parse relative date text (e.g., 'Сегодня, 14:30', 'Вчера, 10:00')."""
        now = datetime.now(timezone.utc)
        text = date_text.lower().strip()

        if "сегодня" in text:
            # Extract time if present
            time_match = re.search(r"(\d{1,2}):(\d{2})", text)
            if time_match:
                hour, minute = int(time_match.group(1)), int(time_match.group(2))
                return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return now

        if "вчера" in text:
            from datetime import timedelta
            yesterday = now - timedelta(days=1)
            time_match = re.search(r"(\d{1,2}):(\d{2})", text)
            if time_match:
                hour, minute = int(time_match.group(1)), int(time_match.group(2))
                return yesterday.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return yesterday

        # Try to parse absolute date
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text.split(",")[0].strip(), fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

        return now

    def parse_html_content(
        self, html_content: str, query: str = "", city: str = "moskva"
    ) -> List[AvitoItem]:
        """
        Parse raw Avito HTML content (from user's browser).
        First tries embedded JSON, then falls back to DOM parsing.

        Args:
            html_content: Raw HTML of Avito search results page
            query: Original search query (for context)
            city: City name

        Returns:
            List of parsed AvitoItem objects
        """
        items = []

        # Try embedded JSON first
        json_data = self._extract_json_from_html(html_content)
        if json_data:
            items = self._parse_items_from_json(json_data, city)

        # Fallback to DOM parsing
        if not items:
            tree = HTMLParser(html_content)
            items = self._parse_items_from_html(tree, city)

        logger.info(f"Parsed {len(items)} items from user-provided HTML")
        return items

    async def get_item_details(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch detailed item information from API."""
        url = f"{API_ITEM_DETAIL}/{item_id}"
        params = {"key": AVITO_API_KEY}

        try:
            response = await self._client.get(url, params=params)
            data = response.json()

            if data.get("status") == "ok":
                return data.get("result", {})
            else:
                logger.warning(f"Item detail API error: {data}")
                return None

        except Exception as e:
            logger.error(f"Failed to fetch item details: {e}")
            return None

    async def get_seller_profile(self, seller_id: str) -> Optional[Dict[str, Any]]:
        """Fetch seller profile information."""
        # This would require additional API endpoints
        # For now, return basic info from item data
        logger.debug(f"Seller profile fetch not implemented for ID: {seller_id}")
        return None

    @property
    def stats(self) -> Dict[str, int]:
        """Get parser statistics."""
        return self._client.stats

    async def close(self) -> None:
        """Cleanup resources."""
        await self._client.close()
