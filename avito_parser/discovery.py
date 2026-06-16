"""
Category Discovery engine — rule-based profitable category scanner.

Scans predefined Avito category slugs, computes price metrics,
and ranks categories by flip profitability potential.
No ML, no NLP, no classifiers — pure statistics.

Usage:
    python -m avito_parser.discovery --max-categories 5 --city moskva
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from avito_parser.analytics import AvitoAnalytics
from avito_parser.config import AppConfig, SearchConfig
from avito_parser.models import CategoryCandidate, DiscoveryResult
from avito_parser.parser import AvitoParser

# Static list of known Avito category slugs with display names
CATEGORY_SLUGS: Dict[str, str] = {
    "noutbuki": "Ноутбуки",
    "telefony": "Телефоны",
    "velosipedy": "Велосипеды",
    "audio_i_video": "Аудио и видео",
    "igry_pristavki_i_programmy": "Игры и приставки",
    "foto_i_videokamery": "Фото и видеокамеры",
    "instrumenty": "Инструменты",
    "bytovaya_tehnika": "Бытовая техника",
    "odezhda_obuv_aksessuary": "Одежда и обувь",
    "detskie_tovary": "Детские товары",
    "tovary_dlya_kompyutera": "Товары для компьютера",
    "remont_i_stroitelstvo": "Ремонт и строительство",
    "sport_i_otdyh": "Спорт и отдых",
    "hobbi_i_razvlecheniya": "Хобби и развлечения",
    "zhivotnye": "Животные",
}

# Profitability heuristic thresholds
MIN_VOLATILITY = 0.10  # stdev/mean must exceed 10%
MIN_MEDIAN_PRICE = 5000  # median price at least 5000 RUB
MAX_RED_FLAG_RATIO = 0.20  # red flag ratio must be below 20%


class CategoryDiscovery:
    """Rule-based engine for discovering profitable Avito categories."""

    def __init__(
        self,
        parser: AvitoParser,
        analytics: AvitoAnalytics,
    ) -> None:
        self._parser = parser
        self._analytics = analytics

    async def discover(
        self,
        city: str = "moskva",
        max_categories: int = 10,
        max_pages: int = 2,
        cache_dir: Optional[Path] = None,
    ) -> DiscoveryResult:
        """
        Scan categories and rank by profitability potential.

        Args:
            city: City slug to scan
            max_categories: Maximum number of categories to scan (capped at len(CATEGORY_SLUGS))
            max_pages: Maximum pages to scan per category (capped at 2)
            cache_dir: Directory for cache files (default: .omo/cache/)

        Returns:
            DiscoveryResult with ranked CategoryCandidate list
        """
        max_categories = min(max_categories, len(CATEGORY_SLUGS))
        max_pages = min(max_pages, 2)

        candidates: List[CategoryCandidate] = []
        slugs_to_scan = list(CATEGORY_SLUGS.items())[:max_categories]

        for slug, name in slugs_to_scan:
            candidate = await self._scan_category(city, slug, name, max_pages)
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(key=lambda c: c.margin_potential, reverse=True)

        result = DiscoveryResult(
            categories=candidates,
            scanned_at=datetime.now(timezone.utc),
        )

        if cache_dir is not None:
            self._save_cache(result, cache_dir)

        return result

    async def _scan_category(
        self,
        city: str,
        slug: str,
        name: str,
        max_pages: int,
    ) -> Optional[CategoryCandidate]:
        """
        Scan a single category and compute profitability metrics.

        Returns CategoryCandidate if profitable, None otherwise.
        """
        search_config = SearchConfig(
            city=city,
            category=slug,
            pages=1,
        )

        try:
            items = await self._parser.search(search_config, max_pages=max_pages)
        except Exception:
            return None

        if not items:
            return None

        prices = [item.price_rub for item in items if item.price_rub > 0]

        if not prices:
            return None

        mean_price = statistics.mean(prices)
        median_price = int(statistics.median(prices))
        item_count = len(items)

        stdev = statistics.stdev(prices) if len(prices) >= 2 else 0.0
        price_volatility = stdev / mean_price if mean_price > 0 else 0.0

        red_flag_count = sum(1 for item in items if item.red_flags)
        red_flag_ratio = red_flag_count / item_count if item_count > 0 else 1.0

        margin_potential = self._compute_margin_potential(
            price_volatility, median_price, red_flag_ratio
        )

        if not self._is_profitable(price_volatility, median_price, red_flag_ratio):
            return None

        return CategoryCandidate(
            name=name,
            slug=slug,
            price_volatility=round(price_volatility, 4),
            margin_potential=round(margin_potential, 2),
            item_count=item_count,
        )

    @staticmethod
    def _is_profitable(
        price_volatility: float,
        median_price: int,
        red_flag_ratio: float,
    ) -> bool:
        """Check if category meets profitability thresholds."""
        return (
            price_volatility > MIN_VOLATILITY
            and median_price > MIN_MEDIAN_PRICE
            and red_flag_ratio < MAX_RED_FLAG_RATIO
        )

    @staticmethod
    def _compute_margin_potential(
        price_volatility: float,
        median_price: int,
        red_flag_ratio: float,
    ) -> float:
        """
        Compute margin potential score.

        Formula: volatility * median_price / 1000 * (1 - red_flag_ratio)
        Higher volatility + higher median price + lower red flags = better potential.
        """
        quality_factor = max(0.0, 1.0 - red_flag_ratio)
        return price_volatility * median_price / 1000.0 * quality_factor

    @staticmethod
    def _save_cache(result: DiscoveryResult, cache_dir: Path) -> None:
        """Save discovery result to JSON cache file."""
        cache_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cache_file = cache_dir / f"discovery_{date_str}.json"

        data = {
            "scanned_at": result.scanned_at.isoformat(),
            "categories": [
                {
                    "name": c.name,
                    "slug": c.slug,
                    "price_volatility": c.price_volatility,
                    "margin_potential": c.margin_potential,
                    "item_count": c.item_count,
                }
                for c in result.categories
            ],
        }

        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _main() -> None:
    """CLI entry point for category discovery."""
    parser = argparse.ArgumentParser(
        description="CategoryDiscovery - scan Avito categories for flip potential",
    )
    parser.add_argument(
        "--max-categories",
        type=int,
        default=5,
        help="Maximum categories to scan (default: 5, max: %(default)s)",
    )
    parser.add_argument(
        "--city",
        type=str,
        default="moskva",
        help="City slug for scanning (default: moskva)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=".omo/cache",
        help="Cache directory (default: .omo/cache)",
    )

    args = parser.parse_args()
    config = AppConfig()
    parser_obj = AvitoParser(config)
    analytics = AvitoAnalytics()
    discovery = CategoryDiscovery(parser_obj, analytics)

    result = await discovery.discover(
        city=args.city,
        max_categories=args.max_categories,
        cache_dir=Path(args.cache_dir),
    )

    print(f"\nCategory Discovery Results ({result.scanned_at.strftime('%Y-%m-%d %H:%M UTC')})")
    print(f"City: {args.city}")
    print(f"Categories scanned: {len(CATEGORY_SLUGS)} available, "
          f"{min(args.max_categories, len(CATEGORY_SLUGS))} requested")
    print(f"Profitable categories found: {len(result.categories)}")
    print("-" * 60)

    if result.categories:
        for i, cat in enumerate(result.categories, 1):
            print(
                f"{i:2}. {cat.name:30s} | vol={cat.price_volatility:.3f} | "
                f"margin={cat.margin_potential:.1f} | items={cat.item_count}"
            )
    else:
        print("No profitable categories found with current thresholds.")
        print(f"(Requires: volatility > {MIN_VOLATILITY}, "
              f"median_price > {MIN_MEDIAN_PRICE}, "
              f"red_flag_ratio < {MAX_RED_FLAG_RATIO})")


if __name__ == "__main__":
    asyncio.run(_main())
