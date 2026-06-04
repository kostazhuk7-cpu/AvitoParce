"""
Browser-based Avito parser using Playwright.
Opens Avito in a real Chromium browser, applies filters, extracts data.
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from selectolax.parser import HTMLParser

from avito_parser.analytics import AvitoAnalytics
from avito_parser.models import AvitoItem, AnalyticsResult, SearchParams, SortOption

# Path for persistent browser profile (keeps cookies between runs)
BROWSER_PROFILE_DIR = str(Path(__file__).parent.parent / ".browser_profile")


class BrowserParser:
    """Parses Avito using real browser automation via Playwright."""

    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 80,
        timeout: int = 60000,
    ) -> None:
        self._headless = headless
        self._slow_mo = slow_mo
        self._timeout = timeout
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    def _get_window_size(self) -> tuple[int, int]:
        """Realistic window size."""
        sizes = [
            (1366, 768),
            (1440, 900),
            (1536, 864),
            (1920, 1080),
            (1280, 720),
        ]
        return random.choice(sizes)

    def _human_delay(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        """Human-like random delay."""
        time.sleep(random.uniform(min_s, max_s))

    def start(self) -> None:
        """Launch browser with persistent profile + stealth."""
        width, height = self._get_window_size()

        self._playwright = sync_playwright().start()

        # Stealth script to apply on every new page
        def _stealth_page(page: Page) -> None:
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['ru-RU', 'ru', 'en-US', 'en']
                });
                window.chrome = { runtime: {} };
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
            """)

        # Use persistent context with saved profile to keep cookies
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-web-security",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--disable-translate",
            "--no-default-browser-check",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
            "--use-mock-keychain",
            f"--window-size={width},{height}",
        ]

        if self._headless:
            launch_args.append("--headless=new")

        # Always use visible browser - headless triggers Cloudflare
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=False,
            slow_mo=self._slow_mo,
            args=launch_args,
            viewport={"width": width, "height": height},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            device_scale_factor=1.0,
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

        self._context.on("page", _stealth_page)
        self._browser = self._context  # persistent context IS the browser

        logger.info(f"Browser started (profile: {BROWSER_PROFILE_DIR})")

    def search(
        self,
        query: str,
        city: str = "moskva",
        category: str = "",
        max_pages: int = 3,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        private_only: bool = False,
    ) -> List[AvitoItem]:
        """
        Open Avito, search, apply filters, extract all listings.

        Returns:
            List of AvitoItem objects
        """
        if not self._browser or not self._context:
            raise RuntimeError("Browser not started. Call start() first.")

        page = self._context.new_page()
        page.set_default_timeout(self._timeout)
        all_items: List[AvitoItem] = []

        try:
            # Build search URL
            params = SearchParams(
                city=city,
                category=category,
                query=query,
                sort=SortOption.DATE,
                min_price=min_price,
                max_price=max_price,
                private_only=private_only,
            )
            url = params.build_url()
            logger.info(f"Opening: {url}")

            # First visit Avito homepage to set cookies + pass JS challenge
            logger.info("Visiting Avito homepage first...")
            page.goto("https://www.avito.ru", wait_until="domcontentloaded", timeout=self._timeout)
            self._human_delay(3.0, 5.0)
            # Handle Cloudflare challenge if present - wait for real content
            try:
                page.wait_for_selector('main, [data-marker]', timeout=30000)
                logger.info("Homepage loaded successfully")
            except Exception:
                logger.warning("Homepage challenge may not have passed")
            self._human_delay(2.0, 3.0)

            # Navigate to search
            logger.info(f"Navigating to search: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
            self._human_delay(3.0, 5.0)

            # Wait for content to load
            try:
                page.wait_for_selector(
                    '[data-marker*="item"]',
                    timeout=20000,
                )
                logger.info("Items found on page")
            except Exception:
                logger.warning("Items not found by marker, checking body")
                html_content = page.content()
                if "captcha" in html_content.lower() or "доступ ограничен" in html_content.lower():
                    logger.error("Captcha or access block detected")
                    # Take screenshot for debugging
                    try:
                        page.screenshot(path="captcha_debug.png")
                        logger.info("Screenshot saved: captcha_debug.png")
                    except Exception:
                        pass
                    return []
                # Try alternative selector
                try:
                    page.wait_for_selector("a[itemprop='url']", timeout=5000)
                    logger.info("Items found by itemprop")
                except Exception:
                    logger.error("No items found on page")
                    return []

            self._human_delay(1.0, 2.0)

            # Human-like scrolling
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 300)")
                self._human_delay(0.3, 0.8)
            self._human_delay(0.5, 1.0)

            # Extract items from current page
            items = self._extract_items_from_page(page, city)
            all_items.extend(items)
            logger.info(f"Page 1: {len(items)} items extracted")

            # Pagination
            for page_num in range(2, max_pages + 1):
                logger.info(f"Navigating to page {page_num}")

                # Try to click next page
                try:
                    next_link = page.query_selector(f'a[href*="p={page_num}"]')
                    if not next_link:
                        # Try "next" button
                        next_link = page.query_selector(
                            '[data-marker*="pagination-button/next"]'
                        )
                    if not next_link:
                        logger.info(f"Page {page_num} not available")
                        break

                    next_link.click()
                    self._human_delay(2.0, 4.0)

                    # Wait for new content
                    try:
                        page.wait_for_selector(
                            '[data-marker*="item"]',
                            timeout=15000,
                        )
                    except Exception:
                        break

                    self._human_delay()

                    items = self._extract_items_from_page(page, city)
                    all_items.extend(items)

                except Exception as e:
                    logger.warning(f"Pagination error: {e}")
                    break

            logger.info(f"Total items extracted: {len(all_items)}")

        except Exception as e:
            logger.error(f"Search error: {e}")
        finally:
            page.close()

        return all_items

    def _extract_items_from_page(
        self, page: Page, default_city: str
    ) -> List[AvitoItem]:
        """Extract items from current page content."""
        # First try to extract from embedded JSON
        html_content = page.content()
        json_data = self._extract_json_from_html(html_content)

        if json_data:
            items = self._parse_items_from_json(json_data, default_city)
            if items:
                logger.debug(f"Extracted {len(items)} items from JSON")
                return items

        # Fallback to DOM parsing
        items = self._parse_items_from_dom(page, default_city)
        logger.debug(f"Extracted {len(items)} items from DOM")
        return items

    def _extract_json_from_html(self, html_content: str) -> Dict[str, Any]:
        """Extract embedded JSON from Avito HTML."""
        import html as html_lib

        tree = HTMLParser(html_content)

        for script in tree.css('script[type="mime/invalid"]'):
            if script.attributes.get("data-mfe-state") == "true":
                text = script.text()
                if not text or "sandbox" in text:
                    continue

                try:
                    unescaped = html_lib.unescape(text)
                    data = json.loads(unescaped)
                    state = data.get("state", {})
                    return state.get("data", {})
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug(f"JSON parse error: {e}")
                    continue

        return {}

    def _parse_items_from_json(
        self, json_data: Dict[str, Any], default_city: str
    ) -> List[AvitoItem]:
        """Parse items from embedded JSON."""
        items = []
        catalog_items = json_data.get("catalog", {}).get("items", [])

        for item_data in catalog_items:
            try:
                item = self._parse_single_item(item_data, default_city)
                if item:
                    items.append(item)
            except Exception as e:
                logger.debug(f"Item parse error: {e}")
                continue

        return items

    def _parse_single_item(
        self, data: Dict[str, Any], default_city: str
    ) -> Optional[AvitoItem]:
        """Parse a single item from data dict."""
        try:
            item_id = data.get("id")
            if not item_id:
                return None

            title = (data.get("title") or "").strip()
            if not title:
                return None

            price_data = data.get("priceDetailed") or {}
            price_value = price_data.get("value", 0)
            if isinstance(price_value, str):
                price_value = int(
                    "".join(c for c in price_value if c.isdigit()) or "0"
                )

            url_path = data.get("urlPath") or data.get("url", "")
            url = (
                f"https://www.avito.ru{url_path}"
                if url_path.startswith("/")
                else url_path
            )

            timestamp_ms = data.get("sortTimeStamp", 0) or 0
            if timestamp_ms:
                publish_date = datetime.fromtimestamp(
                    timestamp_ms / 1000, tz=timezone.utc
                )
            else:
                publish_date = datetime.now(timezone.utc)

            images_data = data.get("images") or []
            images = [
                img["imageUrl"]
                for img in images_data
                if img.get("imageUrl")
            ]

            location = data.get("location") or {}
            city = location.get("name", default_city) or default_city

            seller_id = data.get("sellerId")
            description = data.get("description") or ""

            import re
            # Try to extract condition
            condition = None
            params = data.get("params")
            if isinstance(params, list):
                for p in params:
                    if isinstance(p, dict) and p.get("name") == "Состояние":
                        condition = p.get("value")
                        break
            if not condition and description:
                m = re.search(r"[Сс]остояние[:\s]+([А-Яа-я\s,]+?)\.", description)
                if m:
                    condition = m.group(1).strip()
            if not condition and description:
                m = re.search(r"(?:Состояние|состояние)[:\s]+([А-Яа-я\s\-]+?)(?:\.|,|$|\\n)", description)
                if m:
                    condition = m.group(1).strip()

            item = AvitoItem(
                item_id=int(item_id),
                title=title,
                price_rub=int(price_value),
                description=description,
                url=url,
                publish_date=publish_date,
                seller_name=data.get("sellerName") or "Unknown",
                seller_id=str(seller_id) if seller_id else None,
                images=images,
                city=city,
                thumbnail_url=data.get("thumbnailUrl"),
                condition=condition,
            )
            return item

        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def _parse_items_from_dom(
        self, page: Page, default_city: str
    ) -> List[AvitoItem]:
        """Fallback: parse items from DOM using Playwright JS evaluation."""
        items_data = page.evaluate("""() => {
            function extractCondition(text) {
                const m = text.match(/(?:Состояние|состояние)[:\\s]+([А-Яа-я\\s\\-]+?)(?:\\.|,|$|\\n)/);
                return m ? m[1].trim() : '';
            }
            const items = [];
            const cards = document.querySelectorAll('[data-marker*="item"]');
            cards.forEach(card => {
                try {
                    const id = card.getAttribute('data-item-id');
                    const link = card.querySelector('a[href*="/item/"], a[itemprop="url"]');
                    const titleEl = card.querySelector('[itemprop="name"], [data-marker*="title"], h3');
                    const dateEl = card.querySelector('[data-marker*="date"]');
                    const img = card.querySelector('img');

                    const allText = card.textContent.replace(/\\s+/g, ' ').trim();

                    // Extract price: look for pattern like "1 234 567 ₽" or "1234567 руб"
                    const priceMatch = allText.match(/(\\d[\\d\\s]*)\\s*[₽р]/);
                    let price = 0;
                    if (priceMatch) {
                        price = parseInt(priceMatch[1].replace(/\\s/g, '')) || 0;
                    }

                    items.push({
                        id: parseInt(id) || 0,
                        title: titleEl ? titleEl.textContent.trim() : '',
                        price: price,
                        url: link ? (link.getAttribute('href') || '') : '',
                        dateText: dateEl ? dateEl.textContent.trim() : '',
                        text: allText.substring(0, 100),
                        img: img ? img.getAttribute('src') || '' : '',
                        condition: extractCondition(card.textContent),
                    });
                } catch(e) {}
            });
            return items;
        }""")

        items = []
        now = datetime.now(timezone.utc)
        for d in items_data:
            try:
                if not d["id"] or not d["title"]:
                    continue

                url_path = d["url"]
                if url_path and not url_path.startswith("http"):
                    url_path = f"https://www.avito.ru{url_path}"

                publish_date = now
                if d.get("dateText"):
                    publish_date = self._parse_relative_date(d["dateText"])

                item = AvitoItem(
                    item_id=d["id"],
                    title=d["title"],
                    price_rub=d["price"],
                    url=url_path or f"https://www.avito.ru/item/{d['id']}",
                    publish_date=publish_date,
                    seller_name=d.get("text", "Unknown")[:30],
                    city=default_city,
                    images=[d["img"]] if d.get("img") else [],
                    condition=d.get("condition", ""),
                )
                items.append(item)
            except Exception as e:
                logger.debug(f"DOM item parse error: {e}")
                continue

        return items

    def _parse_relative_date(self, date_text: str) -> datetime:
        """Parse relative date text."""
        now = datetime.now(timezone.utc)
        text = date_text.lower().strip()

        if "сегодня" in text:
            import re
            m = re.search(r"(\d{1,2}):(\d{2})", text)
            if m:
                return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
            return now

        if "вчера" in text:
            from datetime import timedelta
            yesterday = now - timedelta(days=1)
            import re
            m = re.search(r"(\d{1,2}):(\d{2})", text)
            if m:
                return yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
            return yesterday

        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text.split(",")[0].strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        return now

    def analyze(
        self,
        items: List[AvitoItem],
        query: str,
        city: str,
        profit_margin: float = 0.30,
    ) -> AnalyticsResult:
        """Run analytics on scraped items."""
        analytics = AvitoAnalytics(profit_margin=profit_margin)
        return analytics.analyze(items, query=query, city=city)

    def stop(self) -> None:
        """Close browser context."""
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.error(f"Browser close error: {e}")
        logger.info("Browser stopped")
