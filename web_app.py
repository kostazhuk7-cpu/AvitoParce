"""
Avito Parser - Web Interface
Simple FastAPI app: open browser, type query, get results table.
"""

from __future__ import annotations

import asyncio
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from avito_parser.analytics import AvitoAnalytics
from avito_parser.browser_parser import BrowserParser
from avito_parser.config import AppConfig, SearchConfig
from avito_parser.parser import AvitoParser
from avito_parser.models import AnalyticsResult, AvitoItem
from avito_parser.storage import AvitoStorage

CITY_SLUG_MAP = {
    "moskva": "moskva",
    "moskva_i_mo": "moskva",
    "sankt-peterburg": "sankt-peterburg",
    "novosibirsk": "novosibirsk",
    "ekaterinburg": "ekaterinburg",
    "kazan": "kazan",
    "krasnodar": "krasnodar",
    "rostov-na-donu": "rostov-na-donu",
    "rossiya": "rossiya",
}

RED_FLAG_NAMES = {
    "new_account": "Новый аккаунт",
    "low_rating": "Низкий рейтинг",
    "no_photos": "Без фото",
    "bulk_seller": "Оптовик",
    "suspicious_price": "Подозрительная цена",
    "keyword_urgent": "Срочная продажа",
    "keyword_damaged": "Повреждён",
    "keyword_no_docs": "Без документов",
    "keyword_parts": "На запчасти",
}

app = FastAPI(title="Avito Parser", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Global state
config = AppConfig.from_env()
parser = AvitoParser(config)
browser_parser: Optional[BrowserParser] = None
analyzer = AvitoAnalytics(profit_margin=0.30)
storage: Optional[AvitoStorage] = None
search_results: Optional[AnalyticsResult] = None
is_searching = False
search_mode: str = "default"


@app.on_event("startup")
async def startup():
    global storage
    storage = AvitoStorage(config.storage.database_path)
    await storage.initialize()


@app.on_event("shutdown")
async def shutdown():
    global storage
    if storage:
        await storage.close()


def _base_context(request: Request, **kwargs) -> dict:
    ctx = {
        "request": request,
        "results": search_results,
        "is_searching": is_searching,
        "error_message": None,
        "red_flag_names": RED_FLAG_NAMES,
        "saved_count": None,
        "history_sessions": None,
        "session_items": None,
        "history_query": None,
        "history_city": None,
        "search_mode": search_mode,
        "flip_result": None,
    }
    ctx.update(kwargs)
    return ctx


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with search form."""
    return templates.TemplateResponse("index.html", _base_context(request))


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(5),
):
    """Execute search and return results."""
    global search_results, is_searching

    is_searching = True
    search_results = None
    error_message = None

    try:
        search_config = SearchConfig(
            city=CITY_SLUG_MAP.get(city, city),
            category=category,
            query=query,
            pages=pages,
            sort_by_date=True,
        )
        items = await parser.search(search_config, max_pages=pages)
        if items:
            search_results = analyzer.analyze(items, query=query, city=city)
        else:
            error_message = "Объявления не найдены. Avito мог заблокировать запрос (403) или по вашему запросу нет результатов."
    except Exception as e:
        error_message = f"Ошибка поиска: {type(e).__name__}: {e}"
        logger.error(f"Search error: {e}")
    finally:
        is_searching = False

    return templates.TemplateResponse("index.html", _base_context(request, error_message=error_message))


def _browser_search_thread(
    query: str, city: str, category: str, pages: int,
    headless: bool, min_price: int, max_price: int,
) -> list[AvitoItem]:
    """Run browser search in a separate thread (sync Playwright required)."""
    bp = BrowserParser(headless=headless, slow_mo=80)
    bp.start()
    try:
        items = bp.search(
            query=query, city=city, category=category,
            max_pages=pages,
            min_price=min_price if min_price > 0 else None,
            max_price=max_price if max_price > 0 else None,
        )
        return items
    finally:
        bp.stop()


@app.post("/search-browser", response_class=HTMLResponse)
async def search_browser(
    request: Request,
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(3),
    headless: bool = Form(False),
    min_price: int = Form(0),
    max_price: int = Form(0),
):
    """Search Avito via real browser (Playwright)."""
    global search_results, is_searching, search_mode

    is_searching = True
    search_results = None
    search_mode = "browser"
    error_message = None

    try:
        logger.info(f"Browser search: {query} in {city}")
        all_items = await asyncio.to_thread(
            _browser_search_thread,
            query=query, city=CITY_SLUG_MAP.get(city, city), category=category, pages=pages,
            headless=headless, min_price=min_price, max_price=max_price,
        )
        if not all_items:
            error_message = "Браузер не нашёл объявлений. Возможно, Avito показал капчу."
        else:
            search_results = analyzer.analyze(all_items, query=query, city=city)
            logger.info(f"Browser search complete: {len(all_items)} items")
    except Exception as e:
        error_message = f"Ошибка браузерного поиска: {type(e).__name__}: {e}"
        logger.error(f"Browser search error: {e}")
    finally:
        is_searching = False

    return templates.TemplateResponse("index.html", _base_context(request, error_message=error_message, search_mode=search_mode))


@app.post("/parse-html", response_class=HTMLResponse)
async def parse_html(
    request: Request,
    html_content: str = Form(..., alias="html_content"),
    query: str = Form(""),
    city: str = Form("moskva"),
):
    """Parse raw Avito HTML from user's browser."""
    global search_results, is_searching

    is_searching = True
    search_results = None
    error_message = None

    try:
        items = await asyncio.to_thread(
            parser.parse_html_content,
            html_content or "",
            query=query,
            city=CITY_SLUG_MAP.get(city, city),
        )
        if items:
            search_results = analyzer.analyze(items, query=query or "пользовательский HTML", city=city)
        else:
            error_message = "Не удалось найти объявления в предоставленном HTML. Убедитесь, что вы копируете код страницы с результатами поиска Avito."
    except Exception as e:
        error_message = f"Ошибка парсинга HTML: {type(e).__name__}: {e}"
        logger.error(f"HTML parse error: {e}")
    finally:
        is_searching = False

    return templates.TemplateResponse("index.html", _base_context(request, error_message=error_message))


@app.post("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    query: str = Form(""),
    city: str = Form(""),
):
    """Show search history."""
    global storage
    sessions = []
    session_items = []
    if storage:
        sessions = await storage.get_search_sessions()
        if query and city:
            session_items = await storage.get_session_items(query, city)
    return templates.TemplateResponse("index.html", _base_context(
        request,
        results=None,
        history_sessions=sessions,
        session_items=session_items,
        history_query=query,
        history_city=city,
    ))


@app.post("/flip-analytics", response_class=HTMLResponse)
async def flip_analytics_search(
    request: Request,
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(3),
    headless: bool = Form(False),
):
    """Run browser search + flip analysis, return results."""
    global is_searching, search_mode
    is_searching = True
    search_mode = "flip"
    error_message = None
    flip_result = None

    try:
        all_items = await asyncio.to_thread(
            _browser_search_thread,
            query=query, city=CITY_SLUG_MAP.get(city, city),
            category=category, pages=pages,
            headless=headless, min_price=0, max_price=0,
        )
        if not all_items:
            error_message = "Браузер не нашёл объявлений. Возможно, Avito показал капчу."
        else:
            flip_result = analyzer.analyze_flips(all_items, query=query, city=city)
            logger.info(f"Flip analysis: {len(flip_result.candidates)} candidates from {len(all_items)} items")
    except Exception as e:
        error_message = f"Ошибка: {type(e).__name__}: {e}"
        logger.error(f"Flip analysis error: {e}")
    finally:
        is_searching = False

    return templates.TemplateResponse(
        "index.html",
        _base_context(request, error_message=error_message, flip_result=flip_result),
    )


@app.post("/save-results", response_class=HTMLResponse)
async def save_results(request: Request):
    """Save current search results to history."""
    global storage, search_results
    saved_count = 0
    if storage and search_results:
        saved_count = await storage.save_price_snapshots(
            search_results.all_items,
            search_results.query,
            search_results.city,
        )
    return templates.TemplateResponse("index.html", _base_context(
        request,
        saved_count=saved_count,
    ))


@app.get("/api/search")
async def api_search(
    query: str,
    city: str = "moskva",
    category: str = "",
    pages: int = 5,
):
    """JSON API for search."""
    search_config = SearchConfig(
        city=city,
        category=category,
        query=query,
        pages=pages,
        sort_by_date=True,
    )
    items = await parser.search(search_config, max_pages=pages)
    if not items:
        return {"items": [], "analytics": None}
    result = analyzer.analyze(items, query=query, city=city)
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "price": item.price_rub,
                "url": item.url,
                "seller": item.seller_name,
                "city": item.city,
                "is_profitable": item.is_profitable,
                "profit_margin": item.profit_margin,
                "red_flags": [f.value for f in item.red_flags],
                "images": item.images[:1] if item.images else [],
            }
            for item in result.all_items
        ],
        "analytics": {
            "total": result.total_items,
            "median": result.median_price,
            "mean": result.mean_price,
            "min": result.min_price,
            "max": result.max_price,
            "threshold": result.profitable_threshold,
            "profitable_count": len(result.profitable_items),
            "red_flags": result.red_flags_summary,
        },
    }


@app.get("/api/parse-html")
async def api_parse_html(
    html_content: str = "",
    city: str = "moskva",
):
    """JSON API for parsing user-provided HTML."""
    items = parser.parse_html_content(html_content or "", city=city)
    if not items:
        return {"items": [], "analytics": None}
    result = analyzer.analyze(items, query="пользовательский HTML", city=city)
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "price": item.price_rub,
                "url": item.url,
                "seller": item.seller_name,
                "city": item.city,
                "is_profitable": item.is_profitable,
                "profit_margin": item.profit_margin,
                "red_flags": [f.value for f in item.red_flags],
                "images": item.images[:1] if item.images else [],
            }
            for item in result.all_items
        ],
        "analytics": {
            "total": result.total_items,
            "median": result.median_price,
            "mean": result.mean_price,
            "min": result.min_price,
            "max": result.max_price,
            "threshold": result.profitable_threshold,
            "profitable_count": len(result.profitable_items),
        },
    }


def open_browser():
    """Open browser after short delay."""
    time.sleep(1.5)
    webbrowser.open("http://localhost:8765")


if __name__ == "__main__":
    import uvicorn

    print("=" * 50)
    print("Avito Parser - Web Interface")
    print("=" * 50)
    print()
    print("Откройте в браузере: http://localhost:8765")
    print("Для остановки нажмите Ctrl+C")
    print()

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
