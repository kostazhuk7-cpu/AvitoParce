"""
HTTP Client with curl_cffi for TLS fingerprint impersonation.
Handles proxy rotation, rate limiting, and retry logic.
Maintains session cookies between requests for pagination.
Integrates SmartProxyManager for automatic proxy management.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, List, Optional

from curl_cffi import requests as curl_requests
from fake_useragent import UserAgent
from loguru import logger

from avito_parser.config import AppConfig, ProxyConfig
from avito_parser.proxy_fetcher import (
    DirectConnection,
    MobileProxy,
    Proxy,
    ServerProxy,
    SmartProxyManager,
    create_proxy_manager_from_config,
)


# Browser impersonation options for curl_cffi
IMPERSONATE_BROWSERS = [
    "chrome",
    "chrome110",
    "chrome120",
    "chrome131",
    "edge",
    "edge99",
    "firefox",
    "safari",
    "safari15_5",
]

# Default headers mimicking real browser
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class CaptchaRedirectError(Exception):
    """Raised when captcha redirect is detected."""
    pass


class RateLimitError(Exception):
    """Raised when rate limited (429)."""
    pass


class AvitoHttpClient:
    """
    Async HTTP client for Avito scraping with:
    - curl_cffi for TLS fingerprint impersonation
    - Persistent session (cookies preserved between requests for pagination)
    - SmartProxyManager for automatic proxy rotation
    - Adaptive rate limiting
    - Retry logic with exponential backoff
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._ua = UserAgent()
        self._semaphore = asyncio.Semaphore(config.rate_limit.max_concurrent)

        # Initialize proxy manager
        proxies = [ServerProxy(p) for p in config.proxies]
        self._proxy_manager = SmartProxyManager(
            proxies=proxies,
            cooldown_seconds=30,
            max_failures_before_rotate=3,
            auto_rotate_interval=300,
        )

        # Persistent session for cookie preservation
        self._session: Optional[curl_requests.Session] = None
        self._session_impersonate: str = ""
        self._current_proxy: Optional[Proxy] = None

        # Adaptive rate limiting
        self._base_delay = config.rate_limit.min_delay
        self._current_delay = config.rate_limit.min_delay
        self._max_delay = config.rate_limit.max_delay * 2  # Allow longer delays

        # Stats
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "captcha_redirects": 0,
            "rate_limits": 0,
            "proxy_rotations": 0,
        }

    async def initialize(self) -> None:
        """Initialize client (no-op, proxy manager handles initialization)."""
        logger.info(
            f"Client initialized. Proxy stats: {self._proxy_manager.stats}"
        )

    def _create_new_session(self) -> curl_requests.Session:
        """Create a fresh curl_cffi session with random fingerprint."""
        # Close old session if exists
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

        impersonate = random.choice(IMPERSONATE_BROWSERS)
        self._session_impersonate = impersonate
        session = curl_requests.Session(impersonate=impersonate)

        # Set random User-Agent
        headers = DEFAULT_HEADERS.copy()
        headers["User-Agent"] = self._ua.random
        session.headers.update(headers)

        # Get next proxy
        proxy = self._proxy_manager.get_next_proxy()
        if proxy:
            proxy_url = proxy.get_url()
            if proxy_url:
                session.proxies = {"http": proxy_url, "https": proxy_url}
                self._current_proxy = proxy
                logger.debug(f"Using proxy: {proxy}")
            else:
                self._current_proxy = None
                logger.debug("Using direct connection")
        else:
            self._current_proxy = None
            logger.debug("Using direct connection (no proxy)")

        self._session = session
        self._stats["proxy_rotations"] += 1
        return session

    @property
    def _ensure_session(self) -> curl_requests.Session:
        """Get current session or create new one."""
        if self._session is None:
            return self._create_new_session()
        return self._session

    def _is_captcha_redirect(self, response: curl_requests.Response) -> bool:
        """Detect captcha or challenge page in response."""
        # Check for captcha indicators in redirects
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if "captcha" in location.lower() or "challenge" in location.lower():
                return True

        # Check for captcha in response body
        if response.status_code == 200:
            text = response.text[:8000]  # Check more content
            text_lower = text.lower()
            if "captcha" in text_lower or "recaptcha" in text_lower:
                return True
            if "cf-browser-verification" in text_lower or "challenge-platform" in text_lower:
                return True
            if "доступ ограничен" in text_lower or "blocked" in text_lower:
                return True
            if "verify you are human" in text_lower:
                return True

        return False

    def _is_empty_response(self, response: curl_requests.Response) -> bool:
        """Check if response is empty or contains no useful data."""
        if response.status_code != 200:
            return False

        text = response.text
        # Very short response
        if len(text) < 500:
            return True

        # No item data markers
        if "data-marker" not in text and "catalog" not in text.lower():
            # Check if it's an error page
            if "error" in text.lower() or "ошибка" in text.lower():
                return True

        return False

    def _adaptive_delay(self, success: bool) -> None:
        """Adjust delay based on success/failure."""
        if success:
            # Success: slightly decrease delay (go faster)
            self._current_delay = max(
                self._base_delay,
                self._current_delay * 0.9
            )
        else:
            # Failure: increase delay (go slower)
            self._current_delay = min(
                self._max_delay,
                self._current_delay * 1.5
            )

    async def _human_delay(self) -> None:
        """Emulate human behavior with adaptive random delay."""
        # Use gaussian distribution for more natural timing
        delay = random.gauss(self._current_delay, self._current_delay * 0.3)
        delay = max(0.5, delay)  # Minimum 0.5s
        await asyncio.sleep(delay)

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> curl_requests.Response:
        """
        Make GET request with rate limiting and retry logic.
        Preserves cookies between calls (for pagination).
        On 403/429 creates a fresh session with new fingerprint.
        """
        async with self._semaphore:
            self._stats["total_requests"] += 1
            last_exception: Optional[Exception] = None

            for attempt in range(self._config.rate_limit.max_retries):
                # On first attempt, use existing session (preserves cookies).
                # On retry (blocked), create fresh session.
                if attempt == 0:
                    session = self._ensure_session
                else:
                    session = self._create_new_session()

                try:
                    # Adaptive delay before request
                    await self._human_delay()

                    # Merge headers
                    request_headers = DEFAULT_HEADERS.copy()
                    if headers:
                        request_headers.update(headers)

                    logger.debug(
                        f"Request attempt {attempt + 1}/{self._config.rate_limit.max_retries}: "
                        f"{url} (impersonate={self._session_impersonate})"
                    )

                    response = await asyncio.to_thread(
                        session.get,
                        url,
                        params=params,
                        headers=request_headers,
                        timeout=30,
                        allow_redirects=True,
                    )

                    # Check for captcha
                    if self._is_captcha_redirect(response):
                        self._stats["captcha_redirects"] += 1
                        logger.warning(f"Captcha detected at {url}")
                        last_exception = CaptchaRedirectError(f"Captcha at {url}")

                        # Mark proxy as blocked
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)

                        # Burn this session, next attempt gets fresh one
                        self._session = None
                        self._adaptive_delay(False)
                        continue

                    # Check for rate limiting (429)
                    if response.status_code == 429:
                        self._stats["rate_limits"] += 1
                        logger.warning(f"Rate limited (429) at {url}")
                        last_exception = RateLimitError(f"429 at {url}")

                        # Mark proxy as blocked
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)

                        self._session = None
                        self._adaptive_delay(False)

                        # Exponential backoff
                        wait_time = min(
                            self._config.rate_limit.retry_wait_max,
                            self._config.rate_limit.retry_wait_min * (2 ** attempt),
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # Check for 403 (blocked / Cloudflare)
                    if response.status_code == 403:
                        logger.warning(f"Blocked (403) at {url}, rotating session")
                        last_exception = Exception(f"HTTP 403 at {url}")

                        # Mark proxy as blocked
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)

                        # Burn session — next retry gets new fingerprint
                        self._session = None
                        self._adaptive_delay(False)
                        continue

                    # Check for empty response
                    if self._is_empty_response(response):
                        logger.warning(f"Empty/invalid response at {url}")
                        last_exception = Exception(f"Empty response at {url}")
                        self._session = None
                        self._adaptive_delay(False)
                        continue

                    # Other errors
                    if response.status_code >= 400:
                        logger.warning(f"HTTP {response.status_code} at {url}")
                        last_exception = Exception(f"HTTP {response.status_code}")
                        self._adaptive_delay(False)
                        continue

                    # Success — cookies are preserved in the session
                    self._stats["successful_requests"] += 1
                    self._adaptive_delay(True)
                    return response

                except Exception as e:
                    last_exception = e
                    logger.error(f"Request error: {e}")
                    self._session = None
                    self._adaptive_delay(False)
                    continue

            # All retries exhausted
            self._stats["failed_requests"] += 1
            if last_exception:
                raise last_exception
            raise Exception(f"Request failed after {self._config.rate_limit.max_retries} attempts")

    async def post(
        self,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> curl_requests.Response:
        """Make POST request with same retry logic as GET."""
        async with self._semaphore:
            self._stats["total_requests"] += 1
            last_exception: Optional[Exception] = None

            for attempt in range(self._config.rate_limit.max_retries):
                if attempt == 0:
                    session = self._ensure_session
                else:
                    session = self._create_new_session()

                try:
                    await self._human_delay()

                    request_headers = DEFAULT_HEADERS.copy()
                    if headers:
                        request_headers.update(headers)

                    logger.debug(f"POST attempt {attempt + 1}: {url}")

                    response = await asyncio.to_thread(
                        session.post,
                        url,
                        json=json,
                        data=data,
                        headers=request_headers,
                        timeout=30,
                        allow_redirects=True,
                    )

                    if self._is_captcha_redirect(response):
                        self._stats["captcha_redirects"] += 1
                        logger.warning(f"Captcha detected at {url}")
                        last_exception = CaptchaRedirectError(f"Captcha at {url}")
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)
                        self._session = None
                        self._adaptive_delay(False)
                        continue

                    if response.status_code == 429:
                        self._stats["rate_limits"] += 1
                        logger.warning(f"Rate limited at {url}")
                        last_exception = RateLimitError(f"429 at {url}")
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)
                        self._session = None
                        self._adaptive_delay(False)
                        wait_time = min(
                            self._config.rate_limit.retry_wait_max,
                            self._config.rate_limit.retry_wait_min * (2 ** attempt),
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    if response.status_code == 403:
                        logger.warning(f"Blocked (403) at {url}")
                        last_exception = Exception(f"HTTP 403 at {url}")
                        if self._current_proxy:
                            self._proxy_manager.on_failure(self._current_proxy)
                        self._session = None
                        self._adaptive_delay(False)
                        continue

                    if response.status_code >= 400:
                        logger.warning(f"HTTP {response.status_code} at {url}")
                        last_exception = Exception(f"HTTP {response.status_code}")
                        self._adaptive_delay(False)
                        continue

                    self._stats["successful_requests"] += 1
                    self._adaptive_delay(True)
                    return response

                except Exception as e:
                    last_exception = e
                    logger.error(f"POST error: {e}")
                    self._session = None
                    self._adaptive_delay(False)
                    continue

            self._stats["failed_requests"] += 1
            if last_exception:
                raise last_exception
            raise Exception(f"POST failed after {self._config.rate_limit.max_retries} attempts")

    @property
    def stats(self) -> Dict[str, int]:
        return self._stats.copy()

    def reset_stats(self) -> None:
        for key in self._stats:
            self._stats[key] = 0

    async def close(self) -> None:
        """Cleanup resources."""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info(f"Client closed. Stats: {self._stats}")
