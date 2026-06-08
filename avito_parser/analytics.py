"""
Analytics module for price analysis, anomaly detection, and red flag identification.
Includes fuzzy title matching for query relevance filtering.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger
from pydantic import BaseModel, ConfigDict

from avito_parser.models import (
    AnalyticsResult,
    AvitoItem,
    RedFlag,
)

# Red flag keywords (Russian)
RED_FLAG_KEYWORDS_URGENT = [
    "срочно", "быстро", "немедленно", "в течение дня", "сегодня",
    "распродажа", "liquidation", "скидка", "дешево",
]
RED_FLAG_KEYWORDS_DAMAGED = [
    "битый", "после аварии", "не работает", "на запчасти",
    "ремонт", "восстановление", "дефект", "трещина",
]
RED_FLAG_KEYWORDS_NO_DOCS = [
    "без документов", "без ПТС", "без СТС", "без договора",
    "без чека", "без гарантии",
]


def _tokenize(text: str) -> Set[str]:
    """Split text into normalized lowercase tokens (words), filtering short ones."""
    return {t for t in re.split(r"[\s,;:+/\\|()\"'!?.]+", text.lower()) if len(t) >= 2}


def _fuzzy_match(query: str, title: str, min_overlap_ratio: float = 0.5) -> bool:
    """
    Check if query matches title using fuzzy token overlap.

    Logic:
    - Split query and title into word tokens (lowercased, >=2 chars)
    - For each query token, check if it's a SUBSTRING of any title token
    - Match succeeds if enough query tokens match (>= min_overlap_ratio)
    - Short queries (1 token) require exact substring match in title
    """
    query_tokens = _tokenize(query)
    title_tokens = _tokenize(title)
    title_lower = title.lower()

    if not query_tokens:
        return True  # empty query = match all

    # Special case: single-token query — require substring in full title
    # Numeric single tokens use word boundaries — "16" does NOT match "16GB"
    if len(query_tokens) == 1:
        qt = next(iter(query_tokens))
        if qt.isdigit():
            return bool(re.search(r'\b' + qt + r'\b', title_lower))
        return qt in title_lower

    # Numeric query tokens (like "16" in "iPhone 16") MUST match with word boundaries
    # Prevents "iPhone 16" matching "iPhone 6", "iPhone 5S 16GB", etc.
    for qt in query_tokens:
        if qt.isdigit() and not re.search(r'\b' + qt + r'\b', title_lower):
            return False

    # Multi-token: each query token must be a substring of at least one title token
    matched = 0
    for qt in query_tokens:
        # Direct substring in full title (catches "s10+" in "Samsung Galaxy S10+")
        if qt in title_lower:
            matched += 1
            continue
        # Token-level substring: query token is substring of some title token
        if any(qt in tt for tt in title_tokens):
            matched += 1
            continue
        # Reverse: title token is substring of query token (catches "s10" matching "s10+")
        if any(tt in qt for tt in title_tokens if not tt.isdigit()):
            matched += 1
            continue

    ratio = matched / len(query_tokens)
    return ratio >= min_overlap_ratio


class FlipCandidate(BaseModel):
    """Item with flip potential analysis: buy low, resell high."""

    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    condition: str
    buy_price: int
    estimated_resale: int
    commission: int
    net_profit: int
    roi_percent: float
    discount_percent: float
    flag_count: int
    red_flags: List[str]
    item_id: int


class FlipsResult(BaseModel):
    """Result of flip analysis on a set of items."""

    model_config = ConfigDict(extra="forbid")

    query: str
    city: str
    total_items: int
    median_price: int
    mean_price: int
    candidates: List[FlipCandidate]
    scanned_at: datetime


class AvitoAnalytics:
    """
    Analytics engine for Avito listings.
    Detects price anomalies, red flags, and generates insights.
    """

    def __init__(
        self,
        profit_threshold_percentile: float = 25.0,
        profit_margin: float = 0.30,
        relevance_threshold: float = 0.5,
    ):
        """
        Args:
            profit_threshold_percentile: Percentile below which items are flagged
            profit_margin: Minimum discount vs median to flag as profitable (0.30 = 30%)
            relevance_threshold: Min fuzzy match ratio for query relevance (0.5 = 50%)
        """
        self._profit_threshold_percentile = profit_threshold_percentile
        self._profit_margin = profit_margin
        self._relevance_threshold = relevance_threshold

    def analyze(
        self,
        items: List[AvitoItem],
        query: str,
        city: str,
    ) -> AnalyticsResult:
        """
        Run full analytics on a list of items.

        Args:
            items: List of parsed AvitoItem objects
            query: Search query
            city: City name

        Returns:
            AnalyticsResult with statistics and flagged items
        """
        if not items:
            return AnalyticsResult(
                query=query,
                city=city,
                total_items=0,
                median_price=0,
                percentile_25=0,
                percentile_75=0,
                mean_price=0,
                min_price=0,
                max_price=0,
                profitable_threshold=0,
                profitable_items=[],
                all_items=[],
                red_flags_summary={},
            )

        # --- Fuzzy relevance marking ---
        items_marked: List[AvitoItem] = []
        for item in items:
            is_rel = _fuzzy_match(query, item.title, self._relevance_threshold)
            items_marked.append(item.model_copy(update={"is_relevant": is_rel}))

        relevant_items = [i for i in items_marked if i.is_relevant]
        logger.info(
            f"Fuzzy match: {len(relevant_items)}/{len(items_marked)} items "
            f"relevant for query '{query}'"
        )

        # --- Statistics ONLY from relevant items ---
        prices = [item.price_rub for item in relevant_items if item.price_rub > 0]

        if not prices:
            logger.warning("No valid prices found in items")
            return AnalyticsResult(
                query=query,
                city=city,
                total_items=len(items),
                median_price=0,
                percentile_25=0,
                percentile_75=0,
                mean_price=0,
                min_price=0,
                max_price=0,
                profitable_threshold=0,
                profitable_items=[],
                all_items=items,
                red_flags_summary={},
            )

        # Calculate statistics
        median_price = int(statistics.median(prices))
        sorted_prices = sorted(prices)
        n = len(sorted_prices)

        percentile_25 = sorted_prices[int(n * 0.25)] if n > 3 else sorted_prices[0]
        percentile_75 = sorted_prices[int(n * 0.75)] if n > 3 else sorted_prices[-1]
        mean_price = int(statistics.mean(prices))

        # Calculate profitable threshold
        profitable_threshold = int(median_price * (1 - self._profit_margin))

        # --- Red flags for relevant items only ---
        flagged_items: List[AvitoItem] = []
        red_flags_counter: Counter = Counter()

        for item in relevant_items:
            red_flags = self._detect_red_flags(item, median_price)
            item_with_flags = item.model_copy(update={"red_flags": red_flags})

            is_profitable = (
                item.price_rub > 0
                and item.price_rub <= profitable_threshold
            )

            if is_profitable:
                # Calculate profit margin vs market
                profit_margin = (median_price - item.price_rub) / median_price
                item_with_flags = item_with_flags.model_copy(
                    update={
                        "is_profitable": True,
                        "profit_margin": round(profit_margin, 3),
                        "market_median_price": median_price,
                    }
                )
                flagged_items.append(item_with_flags)

            # Count red flags
            for flag in red_flags:
                red_flags_counter[flag.value] += 1

        # Sort profitable items by discount (best deals first)
        flagged_items.sort(
            key=lambda x: x.profit_margin or 0,
            reverse=True,
        )

        logger.info(
            f"Analytics complete: {len(relevant_items)} relevant / {len(items_marked)} total, "
            f"median={median_price}, threshold={profitable_threshold}, "
            f"profitable={len(flagged_items)}"
        )

        return AnalyticsResult(
            query=query,
            city=city,
            total_items=len(items_marked),
            median_price=median_price,
            percentile_25=percentile_25,
            percentile_75=percentile_75,
            mean_price=mean_price,
            min_price=min(prices) if prices else 0,
            max_price=max(prices) if prices else 0,
            profitable_threshold=profitable_threshold,
            profitable_items=flagged_items,
            all_items=items_marked,
            red_flags_summary=dict(red_flags_counter),
            scanned_at=datetime.now(timezone.utc),
        )

    GOOD_FLIP_CONDITIONS = {"Новое", "Отличное", "Б/у"}

    def analyze_flips(
        self,
        items: List[AvitoItem],
        query: str,
        city: str,
        commission_rate: float = 0.10,
        resale_factor: float = 0.90,
    ) -> FlipsResult:
        """
        Analyze items for flip potential (buy low, resell high).

        Args:
            items: Parsed AvitoItem list
            query: Search query
            city: City name
            commission_rate: Platform commission (0.10 = 10%)
            resale_factor: Conservative resale % of median (0.90 = 90%)

        Returns:
            FlipsResult with candidates sorted by net profit
        """
        if not items:
            return FlipsResult(
                query=query, city=city, total_items=0,
                median_price=0, mean_price=0, candidates=[],
                scanned_at=datetime.now(timezone.utc),
            )

        # Mark relevance using fuzzy match
        marked: List[AvitoItem] = []
        for item in items:
            is_rel = _fuzzy_match(query, item.title, self._relevance_threshold)
            marked.append(item.model_copy(update={"is_relevant": is_rel}))

        relevant = [i for i in marked if i.is_relevant]
        prices = [i.price_rub for i in relevant if i.price_rub > 0]

        if not prices:
            return FlipsResult(
                query=query, city=city, total_items=len(items),
                median_price=0, mean_price=0, candidates=[],
                scanned_at=datetime.now(timezone.utc),
            )

        median_price = int(statistics.median(prices))
        mean_price = int(statistics.mean(prices))
        estimated_resale = int(median_price * resale_factor)

        # Build candidates: good condition + below median + few flags
        candidates: List[FlipCandidate] = []
        for item in relevant:
            cond = (item.condition or "").strip()
            is_good_cond = any(gc.lower() in cond.lower() for gc in self.GOOD_FLIP_CONDITIONS) if cond else False

            if not is_good_cond:
                continue
            if item.price_rub <= 0 or item.price_rub >= median_price:
                continue

            red_flags = self._detect_red_flags(item, median_price)
            if len(red_flags) > 1:
                continue

            commission = int(estimated_resale * commission_rate)
            net_profit = estimated_resale - item.price_rub - commission
            discount_percent = ((median_price - item.price_rub) / median_price) * 100
            roi_percent = (net_profit / item.price_rub) * 100 if item.price_rub > 0 else 0

            candidates.append(FlipCandidate(
                title=item.title[:60],
                url=item.url,
                condition=cond or "не указано",
                buy_price=item.price_rub,
                estimated_resale=estimated_resale,
                commission=commission,
                net_profit=net_profit,
                roi_percent=round(roi_percent, 1),
                discount_percent=round(discount_percent, 1),
                flag_count=len(red_flags),
                red_flags=[f.value for f in red_flags],
                item_id=item.item_id,
            ))

        candidates.sort(key=lambda c: c.net_profit, reverse=True)

        logger.info(
            f"Flip analysis: {len(candidates)} candidates from {len(relevant)} relevant items, "
            f"median={median_price}, resale≈{estimated_resale}"
        )

        return FlipsResult(
            query=query, city=city, total_items=len(items),
            median_price=median_price, mean_price=mean_price,
            candidates=candidates,
            scanned_at=datetime.now(timezone.utc),
        )

    def _detect_red_flags(self, item: AvitoItem, median_price: int) -> List[RedFlag]:
        """
        Detect red flags for a single item.

        Args:
            item: AvitoItem to analyze
            median_price: Market median price

        Returns:
            List of detected RedFlag enums
        """
        flags: List[RedFlag] = []

        # Check price anomaly (suspiciously low)
        if median_price > 0 and item.price_rub > 0:
            price_ratio = item.price_rub / median_price
            if price_ratio < 0.5:  # More than 50% below market
                flags.append(RedFlag.SUSPICIOUS_PRICE)

        # Check for urgent keywords in description
        description_lower = (item.description or "").lower()
        title_lower = item.title.lower()
        combined_text = f"{title_lower} {description_lower}"

        for keyword in RED_FLAG_KEYWORDS_URGENT:
            if keyword in combined_text:
                flags.append(RedFlag.KEYWORD_URGENT)
                break

        # Check for damaged keywords
        for keyword in RED_FLAG_KEYWORDS_DAMAGED:
            if keyword in combined_text:
                flags.append(RedFlag.KEYWORD_DAMAGED)
                break

        # Check for no documents keywords
        for keyword in RED_FLAG_KEYWORDS_NO_DOCS:
            if keyword in combined_text:
                flags.append(RedFlag.KEYWORD_NO_DOCS)
                break

        # Check for parts keywords
        if "на запчасти" in combined_text or "запчасти" in combined_text:
            flags.append(RedFlag.KEYWORD_PARTS)

        # Check for no photos
        if not item.images or len(item.images) < 1:
            flags.append(RedFlag.NO_PHOTOS)

        # Check seller info
        if item.seller_type == "new":
            flags.append(RedFlag.NEW_ACCOUNT)

        if item.seller_rating is not None and item.seller_rating < 3.0:
            flags.append(RedFlag.LOW_RATING)

        # Check for bulk seller (many active listings)
        if item.seller_active_listings is not None and item.seller_active_listings > 50:
            flags.append(RedFlag.BULK_SELLER)

        return flags

    def calculate_price_distribution(
        self, items: List[AvitoItem]
    ) -> Dict[str, int]:
        """
        Calculate price distribution for histogram.

        Returns:
            Dictionary with price ranges as keys and counts as values
        """
        prices = [item.price_rub for item in items if item.price_rub > 0]
        if not prices:
            return {}

        min_price = min(prices)
        max_price = max(prices)

        # Create price ranges (10 buckets)
        range_size = max(1, (max_price - min_price) // 10)
        distribution: Dict[str, int] = {}

        for i in range(10):
            range_start = min_price + (i * range_size)
            range_end = range_start + range_size
            label = f"{range_start:,}-{range_end:,}"
            count = sum(1 for p in prices if range_start <= p < range_end)
            distribution[label] = count

        # Add remaining items to last bucket
        if max_price >= min_price + (10 * range_size):
            last_label = f"{min_price + (9 * range_size):,}-{max_price:,}"
            distribution[last_label] = distribution.get(last_label, 0) + sum(
                1 for p in prices if p >= min_price + (9 * range_size)
            )

        return distribution

    def find_similar_items(
        self,
        target_item: AvitoItem,
        all_items: List[AvitoItem],
        title_similarity_threshold: float = 0.7,
    ) -> List[AvitoItem]:
        """
        Find items with similar titles (for price comparison).

        Uses simple word overlap for similarity (no ML needed).
        """
        target_words = set(target_item.title.lower().split())
        similar = []

        for item in all_items:
            if item.item_id == target_item.item_id:
                continue

            item_words = set(item.title.lower().split())
            if not target_words or not item_words:
                continue

            # Calculate Jaccard similarity
            intersection = len(target_words & item_words)
            union = len(target_words | item_words)
            similarity = intersection / union if union > 0 else 0

            if similarity >= title_similarity_threshold:
                similar.append(item)

        return similar

    def detect_price_outliers(
        self,
        items: List[AvitoItem],
        z_score_threshold: float = 2.0,
    ) -> List[AvitoItem]:
        """
        Detect price outliers using Z-score.

        Args:
            items: List of items
            z_score_threshold: Z-score threshold for outlier detection

        Returns:
            List of outlier items
        """
        prices = [item.price_rub for item in items if item.price_rub > 0]
        if len(prices) < 3:
            return []

        mean = statistics.mean(prices)
        stdev = statistics.stdev(prices)

        if stdev == 0:
            return []

        outliers = []
        for item in items:
            if item.price_rub > 0:
                z_score = (item.price_rub - mean) / stdev
                if abs(z_score) > z_score_threshold:
                    outliers.append(item)

        return outliers
