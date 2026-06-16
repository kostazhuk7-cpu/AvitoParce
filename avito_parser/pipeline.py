"""
Avito Parser - Flip Analysis Pipeline with Telegram notification.

Usage:
    python -m avito_parser.pipeline --query "iPhone 16" --city moskva
    python -m avito_parser.pipeline --query "MacBook Pro" --city sankt-peterburg --notify
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Literal

from loguru import logger

from avito_parser.analytics import AvitoAnalytics, FlipCandidate
from avito_parser.browser_parser import BrowserParser
from avito_parser.config import AppConfig
from avito_parser.models import FlipAlert
from avito_parser.notifier import TelegramNotifier


@dataclass
class PipelineStats:
    """Pipeline execution statistics."""

    candidates_found: int = 0
    alerts_sent: int = 0
    throttled: int = 0


def _determine_alert_type(candidate: FlipCandidate) -> Literal["quick", "deep"]:
    """Classify flip as 'deep' (roi >= 40%) or 'quick'."""
    return "deep" if candidate.roi_percent >= 40.0 else "quick"


def run_flip_pipeline(
    query: str,
    city: str,
    notify: bool = False,
    pages: int = 3,
    headless: bool = True,
) -> List[FlipCandidate]:
    """
    Run flip analysis pipeline: search -> analyze -> filter -> notify.

    Args:
        query: Search query (e.g. "iPhone 16")
        city: City slug (e.g. "moskva")
        notify: Send Telegram alerts if True
        pages: Number of search result pages to scan
        headless: Run browser in headless mode

    Returns:
        Filtered list of flip candidates (roi_percent > 0, flag_count <= 1)

    Raises:
        SystemExit: If notify=True but TELEGRAM_BOT_TOKEN is not configured
    """
    config = AppConfig.from_env()

    if notify and not config.telegram_bot_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. "
            "Add TELEGRAM_BOT_TOKEN to .env or omit --notify for dry-run."
        )
        sys.exit(1)

    # --- Step 1: Search via browser ---
    logger.info(f"Pipeline: searching for '{query}' in {city} (pages={pages})")
    bp = BrowserParser(headless=headless, slow_mo=80)
    bp.start()
    try:
        items = bp.search(query=query, city=city, max_pages=pages)
    finally:
        bp.stop()

    if not items:
        logger.warning("No items found by browser parser")
        return []

    logger.info(f"Browser found {len(items)} items")

    # --- Step 2: Flip analysis ---
    analyzer = AvitoAnalytics()
    flips_result = analyzer.analyze_flips(items, query=query, city=city)

    # --- Step 3: Filter candidates ---
    candidates: List[FlipCandidate] = [
        c for c in flips_result.candidates
        if c.roi_percent > 0 and c.flag_count <= 1
    ]

    logger.info(
        f"Filtered: {len(candidates)}/{len(flips_result.candidates)} candidates "
        f"(roi_percent>0, flag_count<=1)"
    )

    if not candidates:
        logger.info("No candidates passed the filter — pipeline finished")
        return []

    # --- Step 4: Print candidates to console ---
    print(f"\n{'=' * 60}")
    print(f"  FLIP CANDIDATES — {query} @ {city}")
    print(f"{'=' * 60}")
    for i, c in enumerate(candidates, 1):
        print(f"\n  {i}. {c.title}")
        print(f"     💵 Buy: {c.buy_price:,} ₽  →  💲 Resale: {c.estimated_resale:,} ₽")
        print(f"     📈 ROI: {c.roi_percent:.1f}%  📉 Discount: {c.discount_percent:.1f}%")
        print(f"     🧾 Profit: {c.net_profit:,} ₽  ⚑ Flags: {c.flag_count}")
        print(f"     🔗 {c.url}")

    # --- Step 5: Send Telegram alerts ---
    if notify:
        notifier = TelegramNotifier(config)
        stats = PipelineStats(candidates_found=len(candidates))

        for c in candidates:
            alert = FlipAlert(
                flip_candidate=c,
                alert_type=_determine_alert_type(c),
                city=city,
            )
            sent = notifier.send_flip_alert(alert)
            if sent:
                stats.alerts_sent += 1
            else:
                stats.throttled += 1

        logger.info(
            f"Pipeline finished: {stats.candidates_found} candidates, "
            f"{stats.alerts_sent} alerts sent, {stats.throttled} throttled"
        )
    else:
        logger.info(
            f"Dry-run: {len(candidates)} candidates printed "
            f"(use --notify to send Telegram alerts)"
        )

    return candidates


def main() -> None:
    """CLI entry point for the flip pipeline."""
    arg_parser = argparse.ArgumentParser(
        description="Avito Flip Analysis Pipeline — search, analyze, notify",
    )
    arg_parser.add_argument(
        "--query", required=True,
        help="Search query (e.g. 'iPhone 16')",
    )
    arg_parser.add_argument(
        "--city", default="moskva",
        help="City slug (e.g. moskva, sankt-peterburg)",
    )
    arg_parser.add_argument(
        "--notify", action="store_true",
        help="Send Telegram alerts (default: dry-run, console only)",
    )
    arg_parser.add_argument(
        "--pages", type=int, default=3,
        help="Number of search result pages to scan (default: 3)",
    )

    args = arg_parser.parse_args()
    run_flip_pipeline(
        query=args.query,
        city=args.city,
        notify=args.notify,
        pages=args.pages,
    )


if __name__ == "__main__":
    main()
