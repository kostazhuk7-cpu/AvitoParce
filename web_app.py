"""
Avito Parser - Web Interface
Simple FastAPI app: open browser, type query, get results table.
"""

from __future__ import annotations

import asyncio
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from avito_parser.analytics import AvitoAnalytics
from avito_parser.browser_parser import BrowserParser
from avito_parser.config import AppConfig, SearchConfig
from avito_parser.models import AnalyticsResult, AvitoItem
from avito_parser.parser import AvitoParser
from avito_parser.storage import AvitoStorage

CITY_SLUG_MAP = {
    # Primary
    "moskva": "moskva",
    "moskva_i_mo": "moskva",
    # Major cities
    "sankt-peterburg": "sankt-peterburg",
    "novosibirsk": "novosibirsk",
    "ekaterinburg": "ekaterinburg",
    "kazan": "kazan",
    "krasnodar": "krasnodar",
    "rostov-na-donu": "rostov-na-donu",
    # Central Russia
    "tver": "tver",
    "tula": "tula",
    "vladimir": "vladimir",
    "kaluga": "kaluga",
    "ryazan": "ryazan",
    "yaroslavl": "yaroslavl",
    "smolensk": "smolensk",
    "bryansk": "bryansk",
    "ivanovo": "ivanovo",
    "kostroma": "kostroma",
    "tambov": "tambov",
    "voronezh": "voronezh",
    "lipetsk": "lipetsk",
    "orel": "orel",
    "kursk": "kursk",
    # Nationwide
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = AppConfig.from_env()
    stg = AvitoStorage(cfg.storage.database_path)
    await stg.initialize()
    app.state.avito_config = cfg
    app.state.avito_storage = stg
    app.state.search_results = None
    yield
    if stg:
        await stg.close()


app = FastAPI(title="Avito Parser", version="1.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ── Dependency injection ───────────────────────────────────────────


def get_config(request: Request) -> AppConfig:
    return request.app.state.avito_config


def get_parser(request: Request) -> AvitoParser:
    return AvitoParser(request.app.state.avito_config)


def get_analyzer(request: Request) -> AvitoAnalytics:
    return AvitoAnalytics(profit_margin=0.30)


def get_storage(request: Request) -> AvitoStorage:
    return request.app.state.avito_storage


# ── Template helpers ───────────────────────────────────────────────


def _base_context(request: Request, results=None, search_mode: str = "default", **kwargs) -> dict:
    ctx = {
        "request": request,
        "results": results,
        "is_searching": False,
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


# ── Routes ─────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with search form."""
    return templates.TemplateResponse("index.html", _base_context(request))


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    parser: Annotated[AvitoParser, Depends(get_parser)],
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(5),
):
    """Execute search and return results."""
    error_message = None
    local_results = None

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
            local_results = analyzer.analyze(items, query=query, city=city)
        else:
            error_message = (
                "Объявления не найдены. Avito мог заблокировать запрос (403) "
                "или по вашему запросу нет результатов."
            )
    except Exception as e:
        error_message = f"Ошибка поиска: {type(e).__name__}: {e}"
        logger.error(f"Search error: {e}")

    request.app.state.search_results = local_results
    return templates.TemplateResponse(
        "index.html",
        _base_context(request, results=local_results, error_message=error_message),
    )


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
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(3),
    headless: bool = Form(False),
    min_price: int = Form(0),
    max_price: int = Form(0),
):
    """Search Avito via real browser (Playwright)."""
    error_message = None
    local_results = None

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
            local_results = analyzer.analyze(all_items, query=query, city=city)
            logger.info(f"Browser search complete: {len(all_items)} items")
    except Exception as e:
        error_message = f"Ошибка браузерного поиска: {type(e).__name__}: {e}"
        logger.error(f"Browser search error: {e}")

    request.app.state.search_results = local_results
    return templates.TemplateResponse(
        "index.html",
        _base_context(request, results=local_results, error_message=error_message, search_mode="browser"),
    )


@app.post("/parse-html", response_class=HTMLResponse)
async def parse_html(
    request: Request,
    parser: Annotated[AvitoParser, Depends(get_parser)],
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
    html_content: str = Form(..., alias="html_content"),
    query: str = Form(""),
    city: str = Form("moskva"),
):
    """Parse raw Avito HTML from user's browser."""
    error_message = None
    local_results = None

    try:
        items = await asyncio.to_thread(
            parser.parse_html_content,
            html_content or "",
            query=query,
            city=CITY_SLUG_MAP.get(city, city),
        )
        if items:
            local_results = analyzer.analyze(items, query=query or "пользовательский HTML", city=city)
        else:
            error_message = (
                "Не удалось найти объявления в предоставленном HTML. "
                "Убедитесь, что вы копируете код страницы с результатами поиска Avito."
            )
    except Exception as e:
        error_message = f"Ошибка парсинга HTML: {type(e).__name__}: {e}"
        logger.error(f"HTML parse error: {e}")

    request.app.state.search_results = local_results
    return templates.TemplateResponse(
        "index.html",
        _base_context(request, results=local_results, error_message=error_message),
    )


@app.post("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    storage: Annotated[AvitoStorage, Depends(get_storage)],
    query: str = Form(""),
    city: str = Form(""),
):
    """Show search history."""
    sessions = await storage.get_search_sessions()
    session_items = []
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
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
    query: str = Form(...),
    city: str = Form("moskva"),
    category: str = Form(""),
    pages: int = Form(3),
    headless: bool = Form(False),
    mode: str = Form("all"),
):
    """Run browser search + flip analysis, return results.
    mode: 'quick' (≥20% discount), 'deep' (≥40%), or 'all' (no filter)."""
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
            if mode in ("quick", "deep"):
                threshold = 40 if mode == "deep" else 20
                flip_result.candidates[:] = [
                    c for c in flip_result.candidates
                    if c.discount_percent >= threshold
                ]
            logger.info(
                f"Flip analysis: {len(flip_result.candidates)} candidates from {len(all_items)} items (mode={mode})"
            )
    except Exception as e:
        error_message = f"Ошибка: {type(e).__name__}: {e}"
        logger.error(f"Flip analysis error: {e}")

    return templates.TemplateResponse(
        "index.html",
        _base_context(request, error_message=error_message, flip_result=flip_result, search_mode="flip"),
    )


@app.post("/save-results", response_class=HTMLResponse)
async def save_results(
    request: Request,
    storage: Annotated[AvitoStorage, Depends(get_storage)],
):
    """Save current search results to history."""
    saved_count = 0
    sr = getattr(request.app.state, "search_results", None)
    if storage and sr:
        saved_count = await storage.save_price_snapshots(
            sr.all_items,
            sr.query,
            sr.city,
        )
    return templates.TemplateResponse("index.html", _base_context(
        request,
        results=sr,
        saved_count=saved_count,
    ))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page — template created later."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── JSON API ───────────────────────────────────────────────────────


@app.get("/api/search")
async def api_search(
    parser: Annotated[AvitoParser, Depends(get_parser)],
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
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
    parser: Annotated[AvitoParser, Depends(get_parser)],
    analyzer: Annotated[AvitoAnalytics, Depends(get_analyzer)],
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


# ── Dashboard JSON API ─────────────────────────────────────────────


def _mode_filter(candidates: list, mode: str, quick_pct: float = 20, deep_pct: float = 40) -> list:
    """Filter flip candidates by mode: quick ≥quick_pct%, deep ≥deep_pct%, all = no filter."""
    if mode == "all":
        return candidates
    threshold = deep_pct if mode == "deep" else quick_pct
    return [c for c in candidates if c.discount_percent >= threshold]


@app.get("/api/trends")
async def api_trends(
    storage: Annotated[AvitoStorage, Depends(get_storage)],
    query: str = Query(..., description="Search query"),
    city: str = Query("moskva"),
    days: int = Query(7, ge=1, le=30, description="Days of history (1-30)"),
):
    """Price trends for a query in a city over N days."""
    try:
        result = await storage.get_market_trends(query, city, days)
        return result
    except Exception as e:
        logger.error(f"Trends error: {e}")
        return {"labels": [], "prices": []}


@app.get("/api/dashboard/flips")
async def api_dashboard_flips(
    request: Request,
    storage: Annotated[AvitoStorage, Depends(get_storage)],
    city: str = Query("moskva"),
    mode: str = Query("all", pattern="^(quick|deep|all)$"),
):
    """Get flip candidates from stored items for a city."""
    try:
        items = await storage.get_items_by_city(city)
        analyzer = get_analyzer(request)
        result = analyzer.analyze_flips(items, query="", city=city)
        candidates = _mode_filter(result.candidates, mode)
        return {"candidates": [c.model_dump() for c in candidates]}
    except Exception as e:
        logger.error(f"Dashboard flips error: {e}")
        return {"candidates": []}


@app.get("/api/flips")
async def api_flips(
    request: Request,
    query: str = Query(..., description="Search query"),
    city: str = Query("moskva"),
    mode: str = Query("all", pattern="^(quick|deep|all)$"),
    pages: int = Query(3, ge=1, le=10),
):
    """JSON API for flip analysis using browser search."""
    try:
        all_items = await asyncio.to_thread(
            _browser_search_thread,
            query=query, city=CITY_SLUG_MAP.get(city, city),
            category="", pages=pages,
            headless=True, min_price=0, max_price=0,
        )
        if not all_items:
            return {"candidates": [], "total_items": 0}

        analyzer = get_analyzer(request)
        result = analyzer.analyze_flips(all_items, query=query, city=city)
        candidates = _mode_filter(result.candidates, mode)
        return {
            "candidates": [c.model_dump() for c in candidates],
            "total_items": result.total_items,
            "median_price": result.median_price,
            "mean_price": result.mean_price,
        }
    except Exception as e:
        logger.error(f"API flips error: {e}")
        return {"candidates": [], "total_items": 0, "error": str(e)}


@app.get("/api/flips/multi")
async def api_flips_multi(
    request: Request,
    query: str = Query(..., description="Search query"),
    cities: str = Query(..., description="Comma-separated city slugs"),
    pages: int = Query(3, ge=1, le=10),
):
    """Multi-region flip analysis across cities."""
    city_list = [c.strip() for c in cities.split(",") if c.strip()]
    if not city_list:
        return JSONResponse(
            {"error": "cities required (comma-separated)"}, status_code=400
        )

    try:

        async def _search_city(city_slug: str) -> tuple[str, list]:
            items = await asyncio.to_thread(
                _browser_search_thread,
                query=query, city=CITY_SLUG_MAP.get(city_slug, city_slug),
                category="", pages=pages,
                headless=True, min_price=0, max_price=0,
            )
            return (city_slug, items)

        tasks = [_search_city(c) for c in city_list]
        city_results = await asyncio.gather(*tasks, return_exceptions=True)

        items_by_region: dict[str, list] = {}
        for result_item in city_results:
            if not isinstance(result_item, tuple):
                logger.error(f"Multi-flip city search error: {result_item}")
                continue
            city_name, items = result_item
            items_by_region[city_name] = items

        analyzer = get_analyzer(request)
        multi_result = analyzer.analyze_flips_multi_region(items_by_region, query)
        return [r.model_dump() for r in multi_result]
    except Exception as e:
        logger.error(f"API flips/multi error: {e}")
        return {"error": str(e)}


@app.get("/api/discovery")
async def api_discovery(
    request: Request,
    city: str = Query("moskva"),
):
    """Category discovery for flip opportunities."""
    try:
        from avito_parser.discovery import CategoryDiscovery
    except ImportError:
        return {"error": "Discovery module not available", "categories": []}

    try:
        config = get_config(request)
        discovery = CategoryDiscovery(config)  # type: ignore[call-arg, misc]
        result = discovery.scan_categories(city, max_categories=10)  # type: ignore[attr-defined]
        return result.model_dump()
    except Exception as e:
        logger.error(f"Discovery error: {e}")
        return {"error": str(e), "categories": []}


@app.get("/api/stats")
async def api_stats(
    storage: Annotated[AvitoStorage, Depends(get_storage)],
    city: str = Query("moskva"),
):
    """Storage statistics and recent price drops."""
    try:
        stats = await storage.get_stats(city)
        drops = await storage.detect_price_drops(min_pct=0.15, days=7)
        return {
            "storage": stats,
            "price_drops_count": len(drops),
            "recent_drops": drops[:10],
        }
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return {
            "error": str(e),
            "storage": {},
            "price_drops_count": 0,
            "recent_drops": [],
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
