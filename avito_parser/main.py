"""
Avito Parser - Main entry point with CLI interface.

Usage:
    python -m avito_parser [OPTIONS]
    python -m avito_parser --preset macbook_moscow
    python -m avito_parser --city moskva --query "MacBook Pro" --category tovary_dlya_kompyutera
    python -m avito_parser --city sankt-peterburg --query iPhone 15 --pages 10
    python -m avito_parser --gui  # Launch graphical interface
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from loguru import logger

from avito_parser.analytics import AvitoAnalytics
from avito_parser.config import AppConfig, SearchConfig
from avito_parser.parser import AvitoParser
from avito_parser.presets import PresetManager
from avito_parser.report import AvitoReport
from avito_parser.storage import AvitoStorage


def setup_logging(config: AppConfig) -> None:
    """Configure loguru logging."""
    # Remove default handler
    logger.remove()

    # Console handler
    logger.add(
        sys.stderr,
        level=config.log.level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    )

    # File handler
    if config.log.file:
        Path(config.log.file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.log.file,
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            rotation="10 MB",
            retention="7 days",
        )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Avito Parser - Production-ready scraping system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --preset macbook_moscow
  %(prog)s --city moskva --query "MacBook Pro M3"
  %(prog)s --city sankt-peterburg --query iPhone 15 --pages 10
  %(prog)s --gui  # Launch graphical interface
        """,
    )

    # Mode selection
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch graphical interface (GUI)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        help="Run saved preset by name",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List all available presets",
    )

    # Search parameters
    parser.add_argument(
        "--city",
        type=str,
        default="moskva",
        help="City slug (default: moskva)",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Search query (not required if using --preset)",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="",
        help="Category slug (e.g., noutbuki, telefony)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        help="Number of pages to scan (default: 5)",
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=None,
        help="Minimum price filter",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=None,
        help="Maximum price filter",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=200,
        help="Search radius in km (default: 200)",
    )
    parser.add_argument(
        "--private-only",
        action="store_true",
        help="Only show private sellers",
    )

    # Output options
    parser.add_argument(
        "--format",
        choices=["txt", "json"],
        default="txt",
        help="Output format (default: txt)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports",
        help="Output directory for reports (default: reports)",
    )
    parser.add_argument(
        "--save-db",
        action="store_true",
        help="Save results to SQLite database",
    )

    # Configuration
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to .env file",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )

    # Analysis parameters
    parser.add_argument(
        "--profit-margin",
        type=float,
        default=0.30,
        help="Profit margin threshold (default: 0.30 = 30%%)",
    )

    return parser.parse_args()


def _launch_gui() -> None:
    """Launch graphical interface."""
    try:
        from avito_gui import AvitoGUI
        app = AvitoGUI()
        app.run()
    except ImportError as e:
        print(f"Ошибка запуска GUI: {e}")
        print("Убедитесь, что tkinter установлен (обычно идёт в комплекте с Python)")
        sys.exit(1)


def _list_presets() -> None:
    """List all available presets."""
    presets = PresetManager()
    presets.print_presets_table()


def _load_preset_config(preset_name: str) -> tuple[dict, float]:
    """Load config from preset. Returns (config_dict, profit_margin)."""
    presets = PresetManager()
    preset = presets.get_preset(preset_name)

    if not preset:
        print(f"Пресет '{preset_name}' не найден!")
        print("Доступные пресеты:")
        _list_presets()
        sys.exit(1)

    return preset, preset.get("profit_margin", 0.30)


async def main() -> None:
    """Main async entry point."""
    args = parse_args()

    # Handle GUI mode
    if args.gui:
        _launch_gui()
        return

    # Handle list presets
    if args.list_presets:
        _list_presets()
        return

    # Handle preset mode
    preset_config = None
    profit_margin = args.profit_margin

    if args.preset:
        preset_config, profit_margin = _load_preset_config(args.preset)
        print(f"Загружен пресет: {preset_config.get('name', args.preset)}")

    # Load configuration
    config = AppConfig.from_env(args.env_file)

    # Override log level from CLI
    if args.log_level:
        from dataclasses import replace
        config = replace(config, log=replace(config.log, level=args.log_level))

    # Setup logging
    setup_logging(config)

    logger.info("=" * 60)
    logger.info("Avito Parser запущен")
    logger.info("=" * 60)

    # Determine query - required either from CLI or preset
    query = args.query or (preset_config.get("query") if preset_config else None)
    if not query:
        print("Ошибка: укажите --query или используйте --preset с запросом")
        sys.exit(1)

    # Build search config from args or preset
    if preset_config:
        search_config = SearchConfig(
            city=preset_config.get("city", args.city),
            category=preset_config.get("category", args.category),
            query=query,
            radius=preset_config.get("radius", args.radius),
            sort_by_date=True,
            min_price=preset_config.get("min_price", args.min_price),
            max_price=preset_config.get("max_price", args.max_price),
            private_only=preset_config.get("private_only", args.private_only),
            pages=preset_config.get("pages", args.pages),
        )
        save_db = preset_config.get("save_db", args.save_db)
    else:
        search_config = SearchConfig(
            city=args.city,
            category=args.category,
            query=query,
            radius=args.radius,
            sort_by_date=True,
            min_price=args.min_price,
            max_price=args.max_price,
            private_only=args.private_only,
            pages=args.pages,
        )
        save_db = args.save_db

    # Initialize components
    parser = AvitoParser(config)
    analytics = AvitoAnalytics(
        profit_margin=profit_margin,
    )
    report = AvitoReport()
    storage = AvitoStorage(config.storage.database_path) if save_db else None

    start_time = time.time()

    try:
        # Initialize storage if needed
        if storage:
            await storage.initialize()

        # Run search
        logger.info(
            f"Поиск: {query} в {search_config.city} "
            f"(категория: {search_config.category or 'все'}, страниц: {search_config.pages})"
        )

        items = await parser.search(search_config, max_pages=search_config.pages)

        if not items:
            logger.warning("Объявления не найдены")
            report.print_search_summary(0, 0, time.time() - start_time)
            return

        # Run analytics
        result = analytics.analyze(
            items,
            query=query,
            city=search_config.city,
        )

        # Print report
        report.print_analytics_report(result)

        # Save to database if requested
        if storage:
            saved_count = await storage.save_items(result.all_items)
            logger.info(f"Сохранено {saved_count} объявлений в базу данных")

        # Save report to file
        report_path = report.save_report(
            result,
            output_path=args.output_dir,
            format=args.format,
        )
        logger.info(f"Отчёт сохранён: {report_path}")

        # Print summary
        elapsed = time.time() - start_time
        report.print_search_summary(
            total_items=result.total_items,
            profitable_count=len(result.profitable_items),
            elapsed_seconds=elapsed,
        )

        # Print client stats
        stats = parser.stats
        logger.info(
            f"Статистика клиента: "
            f"всего запросов={stats['total_requests']}, "
            f"успешных={stats['successful_requests']}, "
            f"ошибок={stats['failed_requests']}, "
            f"капча={stats['captcha_redirects']}, "
            f"rate_limit={stats['rate_limits']}"
        )

    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        raise
    finally:
        # Cleanup
        await parser.close()
        if storage:
            await storage.close()


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
