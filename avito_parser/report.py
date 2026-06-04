"""
Report generation module for analytics output.
Generates formatted reports in terminal and file formats.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from avito_parser.analytics import AvitoAnalytics
from avito_parser.models import AnalyticsResult, AvitoItem, RedFlag


# Red flag descriptions in Russian
RED_FLAG_DESCRIPTIONS = {
    RedFlag.NEW_ACCOUNT: "Новый аккаунт",
    RedFlag.LOW_RATING: "Низкий рейтинг",
    RedFlag.NO_PHOTOS: "Нет фото",
    RedFlag.BULK_SELLER: "Оптовик",
    RedFlag.SUSPICIOUS_PRICE: "Подозрительно низкая цена",
    RedFlag.KEYWORD_URGENT: "Срочная продажа",
    RedFlag.KEYWORD_DAMAGED: "Повреждён/не работает",
    RedFlag.KEYWORD_NO_DOCS: "Без документов",
    RedFlag.KEYWORD_PARTS: "На запчасти",
}


class AvitoReport:
    """
    Generates formatted analytics reports.
    Supports terminal (Rich) and plain text output.
    """

    def __init__(self) -> None:
        self._console = Console()

    def print_analytics_report(self, result: AnalyticsResult) -> None:
        """
        Print formatted analytics report to terminal.

        Args:
            result: AnalyticsResult from analyzer
        """
        # Header
        self._console.print()
        self._console.print(
            Panel(
                f"[bold cyan]ANALYTICS REPORT: {result.query} ({result.city})[/bold cyan]",
                style="cyan",
            )
        )

        # Statistics table
        stats_table = Table(title="Статистика рынка", show_header=False)
        stats_table.add_column("Параметр", style="bold")
        stats_table.add_column("Значение", justify="right")

        stats_table.add_row("Объявлений просканировано", str(result.total_items))
        stats_table.add_row("Медианная цена", f"{result.median_price:,} руб.")
        stats_table.add_row("Средняя цена", f"{result.mean_price:,} руб.")
        stats_table.add_row("25-й перцентиль", f"{result.percentile_25:,} руб.")
        stats_table.add_row("75-й перцентиль", f"{result.percentile_75:,} руб.")
        stats_table.add_row("Минимальная цена", f"{result.min_price:,} руб.")
        stats_table.add_row("Максимальная цена", f"{result.max_price:,} руб.")
        stats_table.add_row(
            "[bold green]Лимит «выгодной цены»[/bold green]",
            f"[bold green]{result.profitable_threshold:,} руб. "
            f"(-{int((1 - result.profitable_threshold / max(result.median_price, 1)) * 100)}%)[/bold green]",
        )

        self._console.print(stats_table)
        self._console.print()

        # Profitable items
        if result.profitable_items:
            self._print_profitable_items(result)
        else:
            self._console.print(
                "[yellow]Выгодных предложений не найдено.[/yellow]"
            )

        # Red flags summary
        if result.red_flags_summary:
            self._print_red_flags_summary(result.red_flags_summary)

        # Footer
        self._console.print()
        self._console.print(
            f"[dim]Отчёт сформирован: {result.scraped_at.strftime('%Y-%m-%d %H:%M:%S')}[/dim]"
        )
        self._console.print()

    def _print_profitable_items(self, result: AnalyticsResult) -> None:
        """Print top profitable items."""
        self._console.print(
            f"[bold red]ТОП-{len(result.profitable_items)} ВЫГОДНЫХ ПРЕДЛОЖЕНИЙ:[/bold red]"
        )
        self._console.print()

        for idx, item in enumerate(result.profitable_items[:10], 1):
            # Calculate discount percentage
            discount = int((item.profit_margin or 0) * 100)
            market_price = item.market_median_price or 0

            # Build item header
            header = (
                f"[bold]{idx}. {item.title}[/bold] | "
                f"Цена: [green]{item.price_rub:,}[/green] "
                f"(Рынок: {market_price:,}) | "
                f"Скидка: [red]{discount}%[/red]"
            )
            self._console.print(header)

            # Seller info
            seller_info = f"   -> Продавец: {item.seller_name}"
            if item.seller_rating:
                seller_info += f" (рейтинг: {item.seller_rating})"
            if item.seller_active_listings:
                seller_info += f", {item.seller_active_listings} объявлений"
            self._console.print(f"   [dim]{seller_info}[/dim]")

            # Red flags
            if item.red_flags:
                flags_text = ", ".join(
                    RED_FLAG_DESCRIPTIONS.get(f, f.value) for f in item.red_flags
                )
                self._console.print(f"   [red]Красные флаги: {flags_text}[/red]")

            # Link
            self._console.print(f"   [blue]{item.url}[/blue]")
            self._console.print()

    def _print_red_flags_summary(self, summary: dict) -> None:
        """Print red flags summary."""
        self._console.print()
        self._console.print("[bold yellow]СВОДКА КРАСНЫХ ФЛАГОВ:[/bold yellow]")

        flags_table = Table(show_header=True)
        flags_table.add_column("Тип флага", style="bold")
        flags_table.add_column("Количество", justify="right")

        for flag_name, count in sorted(summary.items(), key=lambda x: x[1], reverse=True):
            # Convert flag value to description
            try:
                flag_enum = RedFlag(flag_name)
                description = RED_FLAG_DESCRIPTIONS.get(flag_enum, flag_name)
            except ValueError:
                description = flag_name

            flags_table.add_row(description, str(count))

        self._console.print(flags_table)

    def generate_text_report(self, result: AnalyticsResult) -> str:
        """
        Generate plain text report.

        Args:
            result: AnalyticsResult from analyzer

        Returns:
            Formatted text report
        """
        lines = [
            "=" * 60,
            f"ANALYTICS REPORT: {result.query} ({result.city})",
            "=" * 60,
            "",
            f"Объявлений просканировано: {result.total_items}",
            f"Медианная цена: {result.median_price:,} руб.",
            f"Средняя цена: {result.mean_price:,} руб.",
            f"Лимит «выгодной цены»: {result.profitable_threshold:,} руб. "
            f"(-{int((1 - result.profitable_threshold / max(result.median_price, 1)) * 100)}%)",
            "",
        ]

        # Profitable items
        if result.profitable_items:
            lines.append(f"ТОП-{len(result.profitable_items)} ВЫГОДНЫХ ПРЕДЛОЖЕНИЙ:")
            lines.append("")

            for idx, item in enumerate(result.profitable_items[:10], 1):
                discount = int((item.profit_margin or 0) * 100)
                market_price = item.market_median_price or 0

                lines.append(
                    f"{idx}. {item.title} | Цена: {item.price_rub:,} "
                    f"(Рынок: {market_price:,}) | Скидка: {discount}%"
                )
                lines.append(f"   -> Продавец: {item.seller_name}")
                if item.red_flags:
                    flags = [RED_FLAG_DESCRIPTIONS.get(f, f.value) for f in item.red_flags]
                    lines.append(f"   Красные флаги: {', '.join(flags)}")
                lines.append(f"   Ссылка: {item.url}")
                lines.append("")
        else:
            lines.append("Выгодных предложений не найдено.")

        # Red flags summary
        if result.red_flags_summary:
            lines.append("")
            lines.append("СВОДКА КРАСНЫХ ФЛАГОВ:")
            for flag, count in sorted(
                result.red_flags_summary.items(), key=lambda x: x[1], reverse=True
            ):
                try:
                    flag_enum = RedFlag(flag)
                    desc = RED_FLAG_DESCRIPTIONS.get(flag_enum, flag)
                except ValueError:
                    desc = flag
                lines.append(f"  {desc}: {count}")

        lines.append("")
        lines.append(
            f"Отчёт сформирован: {result.scraped_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        lines.append("=" * 60)

        return "\n".join(lines)

    def save_report(
        self,
        result: AnalyticsResult,
        output_path: str = "reports",
        format: str = "txt",
    ) -> str:
        """
        Save report to file.

        Args:
            result: AnalyticsResult
            output_path: Directory to save report
            format: Output format (txt, json)

        Returns:
            Path to saved report
        """
        # Ensure directory exists
        Path(output_path).mkdir(parents=True, exist_ok=True)

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{result.city}_{result.query}_{timestamp}.{format}"
        filepath = Path(output_path) / filename

        if format == "txt":
            report_text = self.generate_text_report(result)
            filepath.write_text(report_text, encoding="utf-8")
        elif format == "json":
            report_data = {
                "query": result.query,
                "city": result.city,
                "total_items": result.total_items,
                "statistics": {
                    "median_price": result.median_price,
                    "mean_price": result.mean_price,
                    "percentile_25": result.percentile_25,
                    "percentile_75": result.percentile_75,
                    "min_price": result.min_price,
                    "max_price": result.max_price,
                    "profitable_threshold": result.profitable_threshold,
                },
                "profitable_items": [
                    {
                        "item_id": item.item_id,
                        "title": item.title,
                        "price": item.price_rub,
                        "market_price": item.market_median_price,
                        "profit_margin": item.profit_margin,
                        "url": item.url,
                        "seller": item.seller_name,
                        "red_flags": [f.value for f in item.red_flags],
                    }
                    for item in result.profitable_items[:20]
                ],
                "red_flags_summary": result.red_flags_summary,
                "scraped_at": result.scraped_at.isoformat(),
            }
            filepath.write_text(
                json.dumps(report_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            raise ValueError(f"Unsupported format: {format}")

        logger.info(f"Report saved: {filepath}")
        return str(filepath)

    def print_search_summary(
        self,
        total_items: int,
        profitable_count: int,
        elapsed_seconds: float,
    ) -> None:
        """Print search completion summary."""
        self._console.print()
        self._console.print(
            Panel(
                f"[bold green]Поиск завершён![/bold green]\n\n"
                f"Просканировано: {total_items} объявлений\n"
                f"Выгодных найдено: {profitable_count}\n"
                f"Время: {elapsed_seconds:.1f} сек.",
                title="Итоги",
                style="green",
            )
        )
