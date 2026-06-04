"""
Proxy management for Avito scraping.
Supports mobile proxies with IP rotation API, server proxies, and direct connection.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Set

import httpx
from loguru import logger

from avito_parser.config import ProxyConfig


class Proxy(ABC):
    """Abstract proxy base class."""

    @abstractmethod
    def get_url(self) -> Optional[str]:
        """Get proxy URL for curl_cffi."""
        ...

    @abstractmethod
    def rotate(self) -> None:
        """Rotate IP (for mobile proxies)."""
        ...

    def __str__(self) -> str:
        url = self.get_url()
        return url if url else "direct"


class DirectConnection(Proxy):
    """Direct connection without proxy."""

    def get_url(self) -> Optional[str]:
        return None

    def rotate(self) -> None:
        pass


class ServerProxy(Proxy):
    """Regular HTTP/SOCKS proxy."""

    def __init__(self, config: ProxyConfig):
        self._config = config
        self._url = config.to_url()

    def get_url(self) -> Optional[str]:
        return self._url

    def rotate(self) -> None:
        pass


class MobileProxy(Proxy):
    """
    Mobile proxy with IP rotation via API.
    Supports mobileproxy.space, proxy.space, and similar services.
    """

    def __init__(
        self,
        proxy_url: str,
        change_ip_api: Optional[str] = None,
        proxy_type: str = "http",
    ):
        self._proxy_url = proxy_url
        self._change_ip_api = change_ip_api
        self._proxy_type = proxy_type
        self._current_ip: Optional[str] = None
        self._last_rotation: float = 0
        self._rotation_count: int = 0

    def get_url(self) -> Optional[str]:
        if self._proxy_type == "socks5":
            return f"socks5://{self._proxy_url}"
        return f"http://{self._proxy_url}"

    def rotate(self) -> None:
        if not self._change_ip_api:
            logger.warning("No change IP API configured for mobile proxy")
            return

        try:
            import requests
            response = requests.get(
                self._change_ip_api,
                params={"format": "json"},
                timeout=15,
            )
            if response.status_code == 200:
                data = response.json()
                self._current_ip = data.get("new_ip", data.get("ip", "unknown"))
                self._last_rotation = time.time()
                self._rotation_count += 1
                logger.success(
                    f"IP rotated → {self._current_ip} "
                    f"(rotation #{self._rotation_count})"
                )
            else:
                logger.error(f"IP rotation failed: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"IP rotation error: {e}")

    async def rotate_async(self) -> None:
        if not self._change_ip_api:
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    self._change_ip_api,
                    params={"format": "json"},
                )
                if response.status_code == 200:
                    data = response.json()
                    self._current_ip = data.get("new_ip", data.get("ip", "unknown"))
                    self._last_rotation = time.time()
                    self._rotation_count += 1
                    logger.success(f"IP rotated → {self._current_ip}")
        except Exception as e:
            logger.error(f"Async IP rotation error: {e}")

    @property
    def time_since_rotation(self) -> float:
        return time.time() - self._last_rotation


class SmartProxyManager:
    """
    Intelligent proxy management with:
    - Mobile proxy rotation on failure
    - Blacklist management
    - Cooldown periods after blocks
    - Fallback to direct connection
    """

    def __init__(
        self,
        proxies: Optional[List[Proxy]] = None,
        cooldown_seconds: int = 30,
        max_failures_before_rotate: int = 3,
        auto_rotate_interval: int = 300,
    ):
        self._proxies = proxies or []
        self._cooldown = cooldown_seconds
        self._max_failures = max_failures_before_rotate
        self._auto_rotate_interval = auto_rotate_interval

        self._current_index: int = 0
        self._failure_count: int = 0
        self._blacklisted: Set[str] = set()
        self._block_cooldown: float = 0
        self._direct = DirectConnection()

    def get_next_proxy(self) -> Proxy:
        if time.time() < self._block_cooldown:
            remaining = self._block_cooldown - time.time()
            logger.debug(f"Cooldown active, {remaining:.0f}s remaining")
            return self._direct

        if not self._proxies:
            return self._direct

        available = [
            p for p in self._proxies
            if str(p) not in self._blacklisted
        ]

        if not available:
            self._blacklisted.clear()
            available = self._proxies

        proxy = available[self._current_index % len(available)]
        self._current_index += 1
        return proxy

    def on_success(self, proxy: Proxy) -> None:
        self._failure_count = 0

        if isinstance(proxy, MobileProxy):
            if proxy.time_since_rotation > self._auto_rotate_interval:
                logger.info("Auto-rotating mobile proxy IP")
                proxy.rotate()

    def on_failure(self, proxy: Proxy, should_rotate: bool = True) -> None:
        self._failure_count += 1

        if isinstance(proxy, MobileProxy) and should_rotate:
            if self._failure_count >= self._max_failures:
                logger.warning(
                    f"Failure threshold reached ({self._failure_count}), "
                    "rotating mobile proxy IP"
                )
                proxy.rotate()
                self._failure_count = 0

        self._block_cooldown = time.time() + self._cooldown

    def blacklist_proxy(self, proxy: Proxy) -> None:
        self._blacklisted.add(str(proxy))
        logger.debug(f"Blacklisted proxy: {proxy}")

    @property
    def stats(self) -> dict:
        return {
            "total_proxies": len(self._proxies),
            "blacklisted": len(self._blacklisted),
            "failure_count": self._failure_count,
            "cooldown_active": time.time() < self._block_cooldown,
            "use_direct": len(self._proxies) == 0,
        }


def create_proxy_manager_from_config(configs: List[ProxyConfig]) -> SmartProxyManager:
    """Create SmartProxyManager from ProxyConfig list."""
    proxies = [ServerProxy(c) for c in configs]
    return SmartProxyManager(proxies=proxies)


def create_mobile_proxy(
    proxy_url: str,
    change_ip_api: Optional[str] = None,
    proxy_type: str = "http",
) -> MobileProxy:
    return MobileProxy(
        proxy_url=proxy_url,
        change_ip_api=change_ip_api,
        proxy_type=proxy_type,
    )
