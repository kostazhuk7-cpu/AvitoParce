"""
Pydantic v2 models for Avito data structures.
Validates and structures all scraped data.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from avito_parser.analytics import FlipCandidate


class SortOption(str, Enum):
    """Avito sort options."""

    DATE = "104"
    RELEVANCE = "101"
    PRICE_ASC = "102"
    PRICE_DESC = "103"


class SellerType(str, Enum):
    """Seller type classification."""

    PRIVATE = "private"
    BUSINESS = "business"
    UNKNOWN = "unknown"


class RedFlag(str, Enum):
    """Red flags for suspicious listings."""

    NEW_ACCOUNT = "new_account"
    LOW_RATING = "low_rating"
    NO_PHOTOS = "no_photos"
    BULK_SELLER = "bulk_seller"
    SUSPICIOUS_PRICE = "suspicious_price"
    KEYWORD_URGENT = "keyword_urgent"
    KEYWORD_DAMAGED = "keyword_damaged"
    KEYWORD_NO_DOCS = "keyword_no_docs"
    KEYWORD_PARTS = "keyword_parts"


class AvitoItem(BaseModel):
    """Single Avito listing."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    # Core fields
    item_id: int = Field(..., description="Unique Avito item ID")
    title: str = Field(..., min_length=1, description="Listing title")
    price_rub: int = Field(..., ge=0, description="Price in rubles")
    description: str = Field(default="", description="Listing description")
    url: str = Field(..., description="Full URL to listing")
    publish_date: datetime = Field(..., description="Publication date")

    # Seller info
    seller_name: str = Field(default="Unknown", description="Seller display name")
    seller_id: Optional[str] = Field(default=None, description="Seller unique ID")
    seller_type: SellerType = Field(default=SellerType.UNKNOWN, description="Seller type")
    seller_rating: Optional[float] = Field(default=None, ge=0, le=5, description="Seller rating")
    seller_registration_date: Optional[datetime] = Field(default=None, description="Seller registration date")
    seller_active_listings: Optional[int] = Field(default=None, ge=0, description="Number of active listings")

    # Media
    images: List[str] = Field(default_factory=list, description="Image URLs")
    thumbnail_url: Optional[str] = Field(default=None, description="Thumbnail URL")

    # Engagement metrics
    views_count: Optional[int] = Field(default=None, ge=0, description="Total views")
    today_views: Optional[int] = Field(default=None, ge=0, description="Views today")
    favorites_count: Optional[int] = Field(default=None, ge=0, description="Favorites count")

    # Location
    city: str = Field(default="", description="City name")
    district: Optional[str] = Field(default=None, description="District name")
    address: Optional[str] = Field(default=None, description="Full address")

    # Category
    category_id: Optional[int] = Field(default=None, description="Category ID")
    category_name: Optional[str] = Field(default=None, description="Category name")

    # Condition
    condition: Optional[str] = Field(default=None, description="Item condition (e.g. Отличное, Б/у)")

    # Analysis fields
    is_relevant: bool = Field(default=True, description="Matches search query (fuzzy)")
    is_profitable: bool = Field(default=False, description="Flagged as profitable deal")
    profit_margin: Optional[float] = Field(default=None, description="Profit margin vs market")
    red_flags: List[RedFlag] = Field(default_factory=list, description="Detected red flags")
    market_median_price: Optional[int] = Field(default=None, description="Market median price")

    # Metadata
    scraped_at: datetime = Field(default_factory=datetime.now, description="When this was scraped")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Ensure URL is absolute and points to Avito."""
        if not v.startswith("http"):
            return f"https://www.avito.ru{v}"
        return v

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        """Strip whitespace and normalize title."""
        return " ".join(v.split()).strip()

    @field_validator("price_rub")
    @classmethod
    def parse_price(cls, v: Any) -> int:
        """Parse price from various formats."""
        if isinstance(v, str):
            # Remove spaces, currency symbols, non-digits
            cleaned = "".join(c for c in v if c.isdigit())
            return int(cleaned) if cleaned else 0
        return int(v)


class SearchParams(BaseModel):
    """Search parameters for Avito API."""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(default="moskva", description="City slug")
    category: str = Field(default="", description="Category slug")
    query: str = Field(default="", description="Search query")
    sort: SortOption = Field(default=SortOption.DATE, description="Sort option")
    page: int = Field(default=1, ge=1, description="Page number")
    min_price: Optional[int] = Field(default=None, ge=0, description="Minimum price")
    max_price: Optional[int] = Field(default=None, ge=0, description="Maximum price")
    radius: int = Field(default=200, ge=0, le=500, description="Search radius in km")
    private_only: bool = Field(default=False, description="Only private sellers")
    limit: int = Field(default=30, ge=1, le=100, description="Items per page")

    def to_avito_params(self) -> Dict[str, str]:
        """Convert to Avito URL parameters."""
        params: Dict[str, str] = {}
        if self.query:
            params["q"] = self.query
        if self.sort:
            params["s"] = self.sort.value
        if self.page > 1:
            params["p"] = str(self.page)
        if self.min_price is not None:
            params["pmin"] = str(self.min_price)
        if self.max_price is not None:
            params["pmax"] = str(self.max_price)
        if self.radius < 500:
            params["radius"] = str(self.radius)
        if self.private_only:
            params["user"] = "1"
        return params

    def build_url(self) -> str:
        """Build full Avito search URL."""
        parts = ["https://www.avito.ru"]
        if self.city:
            parts.append(self.city)
        if self.category:
            parts.append(self.category)

        base = "/".join(parts)
        params = self.to_avito_params()

        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            return f"{base}?{query_string}"
        return base


class AnalyticsResult(BaseModel):
    """Analytics result for a search query."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="Search query")
    city: str = Field(..., description="City")
    total_items: int = Field(..., ge=0, description="Total items scanned")
    median_price: int = Field(..., ge=0, description="Median price")
    percentile_25: int = Field(..., ge=0, description="25th percentile price")
    percentile_75: int = Field(..., ge=0, description="75th percentile price")
    mean_price: int = Field(..., ge=0, description="Mean price")
    min_price: int = Field(..., ge=0, description="Minimum price")
    max_price: int = Field(..., ge=0, description="Maximum price")
    profitable_threshold: int = Field(..., ge=0, description="Price threshold for profitable deals")
    profitable_items: List[AvitoItem] = Field(default_factory=list, description="Items below threshold")
    all_items: List[AvitoItem] = Field(default_factory=list, description="All scanned items")
    red_flags_summary: Dict[str, int] = Field(default_factory=dict, description="Red flag counts")
    scanned_at: datetime = Field(default_factory=datetime.now, description="Scan timestamp")


class SellerProfile(BaseModel):
    """Seller profile information."""

    model_config = ConfigDict(extra="forbid")

    seller_id: str = Field(..., description="Seller unique ID")
    name: str = Field(..., description="Seller name")
    registration_date: Optional[datetime] = Field(default=None, description="Registration date")
    rating: Optional[float] = Field(default=None, ge=0, le=5, description="Seller rating")
    active_listings: int = Field(default=0, ge=0, description="Active listings count")
    total_sold: Optional[int] = Field(default=None, ge=0, description="Total items sold")
    avg_price: Optional[int] = Field(default=None, ge=0, description="Average listing price")
    response_time: Optional[str] = Field(default=None, description="Response time")
    profile_url: str = Field(..., description="Profile URL")
    scraped_at: datetime = Field(default_factory=datetime.now, description="Scrape timestamp")


class CategoryCandidate(BaseModel):
    """Category with flip potential metrics for discovery scanning."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Category display name")
    slug: str = Field(..., description="Category URL slug")
    price_volatility: float = Field(..., ge=0, description="Price volatility score")
    margin_potential: float = Field(..., ge=0, description="Estimated margin potential")
    item_count: int = Field(..., ge=0, description="Number of items in category")


class FlipAlert(BaseModel):
    """Alert about a flip opportunity — quick or deep flip."""

    model_config = ConfigDict(extra="forbid")

    flip_candidate: "FlipCandidate" = Field(..., description="The flip candidate item")
    alert_type: Literal["quick", "deep"] = Field(..., description="Flip type: quick (≤20% margin) or deep (>20% margin)")
    city: str = Field(..., description="City where the item was found")


class CityConfig(BaseModel):
    """City configuration for multi-city scanning."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., description="City URL slug (e.g. 'moskva')")
    name: str = Field(..., description="City display name")
    is_primary: bool = Field(default=False, description="Primary city for default searches")


class DiscoveryResult(BaseModel):
    """Result of category discovery scan — candidate categories found."""

    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryCandidate] = Field(default_factory=list, description="Discovered category candidates")
    scanned_at: datetime = Field(default_factory=datetime.now, description="Scan timestamp")


class RegionalFlipResult(BaseModel):
    """Multi-region flip analysis result for a single city."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    city: str = Field(..., description="City name")
    candidates: List[Any] = Field(default_factory=list, description="FlipCandidate objects")
    median_price: int = Field(..., ge=0, description="Median price in region")
    mean_price: int = Field(..., ge=0, description="Mean price in region")
    buy_advice: str = Field(..., description="Buying advice for this region")
    sell_advice: str = Field(..., description="Selling advice for this region")
