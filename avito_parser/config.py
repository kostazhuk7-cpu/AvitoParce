"""
Configuration management for Avito Parser.
Loads settings from .env file with sensible defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProxyConfig:
    """Single proxy configuration."""

    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    @classmethod
    def from_string(cls, proxy_str: str) -> Optional["ProxyConfig"]:
        """
        Parse proxy string format: login:password@host:port
        Returns None if string is empty or invalid.
        """
        proxy_str = proxy_str.strip()
        if not proxy_str:
            return None

        try:
            # Handle login:password@host:port
            if "@" in proxy_str:
                auth, host_port = proxy_str.rsplit("@", 1)
                username, password = auth.split(":", 1)
            else:
                username, password = None, None
                host_port = proxy_str

            host, port_str = host_port.rsplit(":", 1)
            return cls(
                host=host,
                port=int(port_str),
                username=username,
                password=password,
            )
        except (ValueError, IndexError):
            return None

    def to_url(self) -> str:
        """Convert to proxy URL format: http://login:password@host:port"""
        if self.username and self.password:
            return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"

    def __str__(self) -> str:
        """Masked representation for logging."""
        if self.username:
            return f"{self.username}:***@{self.host}:{self.port}"
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class SearchConfig:
    """Search parameters for Avito."""

    city: str = "moskva"
    category: str = ""
    query: str = ""
    radius: int = 200
    sort_by_date: bool = True
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    private_only: bool = False
    pages: int = 5


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limiting configuration."""

    max_concurrent: int = 3
    min_delay: float = 2.0
    max_delay: float = 5.0
    max_retries: int = 3
    retry_wait_min: float = 4.0
    retry_wait_max: float = 60.0


@dataclass(frozen=True)
class MobileProxyConfig:
    """Mobile proxy with IP rotation API."""

    proxy_url: str = ""
    change_ip_api: str = ""
    proxy_type: str = "http"  # "http" or "socks5"

    @property
    def is_configured(self) -> bool:
        return bool(self.proxy_url)


@dataclass(frozen=True)
class StorageConfig:
    """Storage configuration."""

    database_path: str = "data/avito.db"


@dataclass(frozen=True)
class LogConfig:
    """Logging configuration."""

    level: str = "INFO"
    file: Optional[str] = "logs/avito.log"


@dataclass
class AppConfig:
    """Main application configuration."""

    proxies: List[ProxyConfig] = field(default_factory=list)
    mobile_proxy: MobileProxyConfig = field(default_factory=MobileProxyConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "AppConfig":
        """
        Load configuration from .env file.

        Args:
            env_path: Path to .env file. If None, searches current directory and parents.
        """
        # Load .env file
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        # Parse proxies (one per line, comma-separated, or semicolon-separated)
        proxies_raw = os.getenv("PROXIES", "")
        proxies = []
        for line in proxies_raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxy = ProxyConfig.from_string(line)
            if proxy:
                proxies.append(proxy)

        # Also support comma-separated format
        if not proxies and proxies_raw:
            for item in proxies_raw.split(","):
                proxy = ProxyConfig.from_string(item)
                if proxy:
                    proxies.append(proxy)

        # Parse mobile proxy
        mobile_proxy_url = os.getenv("MOBILE_PROXY_URL", "")
        mobile_proxy_change_url = os.getenv("MOBILE_PROXY_CHANGE_URL", "")
        mobile_proxy_type = os.getenv("MOBILE_PROXY_TYPE", "http")

        mobile_proxy = MobileProxyConfig(
            proxy_url=mobile_proxy_url,
            change_ip_api=mobile_proxy_change_url,
            proxy_type=mobile_proxy_type,
        )

        # If mobile proxy is configured, add it to proxies list
        if mobile_proxy.is_configured:
            proxy_config = ProxyConfig.from_string(mobile_proxy_url)
            if proxy_config and proxy_config not in proxies:
                proxies.insert(0, proxy_config)  # Priority

        return cls(
            proxies=proxies,
            mobile_proxy=mobile_proxy,
            search=SearchConfig(
                city=os.getenv("DEFAULT_CITY", "moskva"),
                radius=int(os.getenv("DEFAULT_RADIUS", "200")),
            ),
            rate_limit=RateLimitConfig(
                max_concurrent=int(os.getenv("MAX_CONCURRENT_REQUESTS", "3")),
                min_delay=float(os.getenv("MIN_DELAY_SECONDS", "2.0")),
                max_delay=float(os.getenv("MAX_DELAY_SECONDS", "5.0")),
                max_retries=int(os.getenv("MAX_RETRIES", "3")),
                retry_wait_min=float(os.getenv("RETRY_WAIT_MIN", "4")),
                retry_wait_max=float(os.getenv("RETRY_WAIT_MAX", "60")),
            ),
            storage=StorageConfig(
                database_path=os.getenv("DATABASE_PATH", "data/avito.db"),
            ),
            log=LogConfig(
                level=os.getenv("LOG_LEVEL", "INFO"),
                file=os.getenv("LOG_FILE", "logs/avito.log"),
            ),
        )
