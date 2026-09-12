"""Shared report data models for email + dashboard rendering."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY = Decimal("0.01")

PLATFORM_KEYS = (
    "amazon",
    "shopify_direct",
    "dsg",
    "nordstrom",
    "walmart",
)

PLATFORM_LABELS = {
    "amazon": "Amazon",
    "shopify_direct": "Shopify Direct",
    "dsg": "DICK'S SPORTING GOODS",
    "nordstrom": "NORDSTROM",
    "walmart": "Walmart",
}

PLATFORM_SHORT = {
    "amazon": "Amazon",
    "shopify_direct": "Shopify Direct",
    "dsg": "DSG",
    "nordstrom": "Nordstrom",
    "walmart": "Walmart",
}

# Email / dashboard category display order
CATEGORY_ORDER = (
    "umbrellas",
    "backpack",
    "poncho",
    "hat",
    "shirts",
)

CATEGORY_LABELS = {
    "umbrellas": "Total umbrellas sold",
    "backpack": "Backpack",
    "poncho": "Poncho",
    "hat": "Hat",
    "shirts": "Shirts",
}


def money_str(value: Decimal | float | int | None, *, unavailable: bool = False) -> str:
    if unavailable or value is None:
        return "Unavailable"
    amount = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    return f"${amount:,.2f}"


def pct_str(value: Decimal | float | None, *, unavailable: bool = False) -> str:
    if unavailable or value is None:
        return "Unavailable"
    return f"{Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP):.2f}%"


def mult_str(value: Decimal | float | None, *, unavailable: bool = False) -> str:
    if unavailable or value is None:
        return "Unavailable"
    return f"{Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP):.2f}x"


@dataclass
class PlatformMetrics:
    key: str
    label: str
    available: bool = False
    revenue: Decimal = Decimal("0")
    units: int = 0
    order_count: int = 0
    note: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def short_label(self) -> str:
        return PLATFORM_SHORT.get(self.key, self.label)


@dataclass
class SkuRow:
    platform: str
    sku: str
    item: str
    units: int
    revenue: Decimal


@dataclass
class AdMetrics:
    amazon_real_acos: Decimal | None = None
    shopify_blended_cos: Decimal | None = None
    shopify_revenue_per_ad_dollar: Decimal | None = None
    shopify_ad_spend: Decimal | None = None
    available: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class PeriodCard:
    key: str
    label: str
    revenue: Decimal
    subtitle: str
    color: str


@dataclass
class DailyReport:
    report_day: date
    platforms: dict[str, PlatformMetrics] = field(default_factory=dict)
    category_totals: dict[str, int] = field(default_factory=dict)
    category_by_platform: dict[str, dict[str, int]] = field(default_factory=dict)
    ad_metrics: AdMetrics = field(default_factory=AdMetrics)
    sku_rows: list[SkuRow] = field(default_factory=list)
    period_cards: list[PeriodCard] = field(default_factory=list)
    status_lines: list[str] = field(default_factory=list)
    reconciliation_revenue_variance: Decimal = Decimal("0")
    reconciliation_unit_variance: int = 0
    dashboard_url: str = ""
    greeting_name: str = "Rick"

    @property
    def target_date(self) -> date:
        """Active report date (previous ET day in production runs)."""
        return self.report_day

    def format_target_date(self) -> str:
        """Human-readable header date: `Month D, YYYY` (e.g. September 11, 2026)."""
        d = self.target_date
        return f"{d.strftime('%B')} {d.day}, {d.year}"

    def total_revenue(self) -> Decimal:
        return sum(
            (p.revenue for p in self.platforms.values() if p.available),
            Decimal("0"),
        )

    def total_units(self) -> int:
        return sum(p.units for p in self.platforms.values() if p.available)

    def ordered_platforms(self) -> list[PlatformMetrics]:
        return [
            self.platforms[key]
            for key in PLATFORM_KEYS
            if key in self.platforms
        ]
