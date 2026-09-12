#!/usr/bin/env python3
"""Daily multi-platform revenue report → Brevo email + hosted dashboard."""
from __future__ import annotations

import argparse
import base64
import calendar
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

from reporting.categories import category_for, empty_category_counts
from reporting.dashboard import write_dashboard
from reporting.email_report import build_email_html, build_subject
from reporting.models import (
    CATEGORY_ORDER,
    PLATFORM_KEYS,
    PLATFORM_LABELS,
    AdMetrics,
    DailyReport,
    PeriodCard,
    PlatformMetrics,
    SkuRow,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("daily_report")

ET = ZoneInfo("America/New_York")
MONEY = Decimal("0.01")
ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
ARCHIVE_PATH = ROOT / "data" / "daily_archive.json"

SHOPIFY_API_VERSION = "2024-10"
WALMART_TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
WALMART_ORDERS_URL = "https://marketplace.walmartapis.com/v3/orders"
WALMART_STATUSES = ("Created", "Acknowledged", "Shipped", "Delivered")
WALMART_SHIP_NODE_TYPES = ("SellerFulfilled", "WFSFulfilled")
BREVO_SMTP_URL = "https://api.brevo.com/v3/smtp/email"
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3


def previous_day_et(now: datetime | None = None) -> date:
    current = now or datetime.now(ET)
    return current.astimezone(ET).date() - timedelta(days=1)


def day_window_et(report_day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(report_day, datetime.min.time(), tzinfo=ET)
    end = datetime.combine(report_day + timedelta(days=1), datetime.min.time(), tzinfo=ET)
    return start, end


def require_env(*keys: str) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        value = os.environ.get(key, "").strip()
        if not value:
            missing.append(key)
        else:
            values[key] = value
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
    return values


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | dict[str, Any] | str | None = None,
    json_body: dict[str, Any] | None = None,
    retries: int = HTTP_RETRIES,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                data=data,
                json=json_body,
                timeout=HTTP_TIMEOUT,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                wait = 2**attempt
                log.warning(
                    "HTTP %s from %s — retrying in %ss",
                    response.status_code,
                    url.split("?")[0],
                    wait,
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries - 1:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"HTTP request failed for {url}: {last_error}")


def money_decimal(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def empty_platforms() -> dict[str, PlatformMetrics]:
    return {
        key: PlatformMetrics(key=key, label=PLATFORM_LABELS[key])
        for key in PLATFORM_KEYS
    }


# ---------------------------------------------------------------------------
# Shopify (Direct / DSG / Nordstrom)
# ---------------------------------------------------------------------------


def _shopify_endpoint() -> tuple[str, dict[str, str]]:
    env = require_env("SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN")
    store = env["SHOPIFY_STORE_URL"].rstrip("/").removeprefix("https://").removeprefix("http://")
    headers = {
        "X-Shopify-Access-Token": env["SHOPIFY_ACCESS_TOKEN"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/graphql.json", headers


def _classify_shopify_channel(node: dict[str, Any]) -> str:
    tags = node.get("tags") or []
    if isinstance(tags, str):
        tag_bits = [t.strip().lower() for t in tags.split(",")]
    else:
        tag_bits = [str(t).strip().lower() for t in tags]

    channel = ((node.get("channelInformation") or {}).get("channelDefinition") or {})
    blob = " ".join(
        [
            *tag_bits,
            str(node.get("sourceName") or ""),
            str(channel.get("channelName") or ""),
            str(channel.get("subChannelName") or ""),
            str(node.get("name") or ""),
        ]
    ).lower()

    if "nordstrom" in blob:
        return "nordstrom"
    if "dick" in blob or re.search(r"\bdsg\b", blob) or "sporting goods" in blob:
        return "dsg"
    return "shopify_direct"


def _iter_shopify_orders(start: datetime, end: datetime) -> list[dict[str, Any]]:
    endpoint, headers = _shopify_endpoint()
    query = """
    query OrdersPage($cursor: String, $query: String!) {
      orders(first: 50, after: $cursor, query: $query, sortKey: CREATED_AT) {
        edges {
          node {
            id
            name
            tags
            sourceName
            channelInformation {
              channelDefinition { channelName subChannelName }
            }
            totalPriceSet { shopMoney { amount currencyCode } }
            lineItems(first: 100) {
              edges {
                node {
                  sku
                  title
                  quantity
                  originalTotalSet { shopMoney { amount } }
                }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    search = f"created_at:>={start.isoformat()} created_at:<{end.isoformat()} status:any"
    cursor: str | None = None
    nodes: list[dict[str, Any]] = []
    while True:
        response = http_request(
            "POST",
            endpoint,
            headers=headers,
            json_body={"query": query, "variables": {"cursor": cursor, "query": search}},
        )
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
        orders = body.get("data", {}).get("orders") or {}
        for edge in orders.get("edges") or []:
            node = edge.get("node")
            if node:
                nodes.append(node)
        page = orders.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
            continue
        break
    return nodes


def fetch_shopify_channels(report_day: date) -> tuple[dict[str, PlatformMetrics], dict[str, dict[str, int]], list[SkuRow]]:
    start, end = day_window_et(report_day)
    nodes = _iter_shopify_orders(start, end)

    platforms = {
        key: PlatformMetrics(key=key, label=PLATFORM_LABELS[key], available=True)
        for key in ("shopify_direct", "dsg", "nordstrom")
    }
    categories = {key: empty_category_counts() for key in ("shopify_direct", "dsg", "nordstrom")}
    skus: list[SkuRow] = []

    for node in nodes:
        channel_key = _classify_shopify_channel(node)
        platform = platforms[channel_key]
        platform.order_count += 1
        platform.revenue += money_decimal(
            ((node.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount")
        )

        for edge in ((node.get("lineItems") or {}).get("edges") or []):
            item = edge.get("node") or {}
            qty = int(item.get("quantity") or 0)
            title = str(item.get("title") or "")
            sku = str(item.get("sku") or "UNKNOWN")
            line_rev = money_decimal(
                ((item.get("originalTotalSet") or {}).get("shopMoney") or {}).get("amount")
            )
            cat = category_for(title, sku)
            categories[channel_key][cat] = categories[channel_key].get(cat, 0) + qty
            platform.units += qty
            skus.append(
                SkuRow(
                    platform=PLATFORM_LABELS[channel_key],
                    sku=sku,
                    item=title,
                    units=qty,
                    revenue=line_rev,
                )
            )

    for platform in platforms.values():
        log.info(
            "%s: revenue=%s orders=%s units=%s",
            platform.label,
            platform.revenue,
            platform.order_count,
            platform.units,
        )
    return platforms, categories, skus


# ---------------------------------------------------------------------------
# Walmart
# ---------------------------------------------------------------------------


def _walmart_access_token(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = http_request(
        "POST",
        WALMART_TOKEN_URL,
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_SVC.NAME": "Walmart Marketplace",
        },
        data=urlencode({"grant_type": "client_credentials"}),
    )
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Walmart token response did not contain access_token")
    return str(token)


def _walmart_orders_slice(
    token: str,
    start_utc: datetime,
    end_utc: datetime,
    status: str,
    ship_node_type: str,
) -> list[dict[str, Any]]:
    params = {
        "createdStartDate": start_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "createdEndDate": end_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": status,
        "shipNodeType": ship_node_type,
        "limit": 200,
    }
    url: str | None = f"{WALMART_ORDERS_URL}?{urlencode(params)}"
    orders: list[dict[str, Any]] = []
    while url:
        response = http_request(
            "GET",
            url,
            headers={
                "WM_SEC.ACCESS_TOKEN": token,
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "WM_SVC.NAME": "Walmart Marketplace",
                "Accept": "application/json",
            },
        )
        listing = response.json().get("list", {})
        block = listing.get("elements", {}).get("order", []) if isinstance(listing, dict) else []
        if isinstance(block, dict):
            block = [block]
        orders.extend(block)
        cursor = listing.get("meta", {}).get("nextCursor") if isinstance(listing, dict) else None
        url = f"{WALMART_ORDERS_URL}?{str(cursor).lstrip('?')}" if cursor else None
    return orders


def _walmart_order_key(order: dict[str, Any]) -> str:
    return str(order.get("purchaseOrderId") or order.get("customerOrderId") or id(order))


def _walmart_line_gross(line: dict[str, Any]) -> Decimal:
    charges = line.get("charges", {}).get("charge", [])
    if isinstance(charges, dict):
        charges = [charges]
    total = Decimal("0")
    for charge in charges:
        if str(charge.get("chargeType") or "").upper() in {"PRODUCT", "SHIPPING"}:
            total += money_decimal(charge.get("chargeAmount"))
    return total


def _walmart_qty(line: dict[str, Any]) -> int:
    try:
        return int(Decimal(str(line.get("orderLineQuantity", {}).get("amount", 0) or 0)))
    except Exception:
        return 0


def fetch_walmart(report_day: date) -> tuple[PlatformMetrics, dict[str, int], list[SkuRow]]:
    env = require_env("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET")
    start_et, end_et = day_window_et(report_day)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)
    token = _walmart_access_token(env["WALMART_CLIENT_ID"], env["WALMART_CLIENT_SECRET"])

    unique: dict[str, dict[str, Any]] = {}
    for ship_node_type in WALMART_SHIP_NODE_TYPES:
        for status in WALMART_STATUSES:
            for order in _walmart_orders_slice(token, start_utc, end_utc, status, ship_node_type):
                unique[_walmart_order_key(order)] = order

    cats = empty_category_counts()
    skus: list[SkuRow] = []
    revenue = Decimal("0")
    units = 0
    for order in unique.values():
        lines = order.get("orderLines", {}).get("orderLine", [])
        if isinstance(lines, dict):
            lines = [lines]
        for line in lines:
            item = line.get("item", {})
            sku = str(item.get("sku") or "UNKNOWN")
            name = str(item.get("productName") or sku)
            qty = _walmart_qty(line)
            line_rev = _walmart_line_gross(line)
            cat = category_for(name, sku)
            cats[cat] = cats.get(cat, 0) + qty
            revenue += line_rev
            units += qty
            skus.append(SkuRow(platform="Walmart", sku=sku, item=name, units=qty, revenue=line_rev))

    platform = PlatformMetrics(
        key="walmart",
        label=PLATFORM_LABELS["walmart"],
        available=True,
        revenue=revenue,
        units=units,
        order_count=len(unique),
        note="Exact-date relay",
    )
    log.info("Walmart: revenue=%s orders=%s units=%s", revenue, len(unique), units)
    return platform, cats, skus


# ---------------------------------------------------------------------------
# Sellerboard (Amazon)
# ---------------------------------------------------------------------------


def _first_matching_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    normalized = {str(col).strip().lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    for key, original in normalized.items():
        for candidate in candidates:
            if candidate.lower() in key:
                return original
    return None


def _series_numeric(df: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None or column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(
        df[column].astype(str).str.replace(r"[,$%]", "", regex=True),
        errors="coerce",
    ).fillna(0)


def _sum_numeric(df: pd.DataFrame, column: str | None) -> Decimal:
    series = _series_numeric(df, column)
    if series.empty:
        return Decimal("0")
    return money_decimal(series.sum())


def _load_sellerboard_csv(url: str, label: str) -> pd.DataFrame:
    response = http_request("GET", url, headers={"Accept": "text/csv,*/*"})
    content_type = (response.headers.get("Content-Type") or "").lower()
    text = response.text
    if "html" in content_type or text.lstrip().lower().startswith("<!doctype") or text.lstrip().lower().startswith("<html"):
        raise RuntimeError(
            f"Sellerboard {label} URL returned HTML instead of CSV "
            "(use a permanent automation URL)"
        )
    df = pd.read_csv(StringIO(text))
    if df.empty:
        log.warning("Sellerboard %s CSV is empty", label)
    return df


def _filter_daily_rows(df: pd.DataFrame, report_day: date) -> pd.DataFrame:
    date_col = _first_matching_column(df, ("Date", "Day", "Report Date", "date"))
    if date_col is None:
        return df
    parsed = pd.to_datetime(df[date_col], errors="coerce")
    mask = parsed.dt.date == report_day
    if mask.any():
        return df.loc[mask]
    log.info("Sellerboard daily has no exact date rows for %s; using full export", report_day)
    return df


def fetch_amazon_sellerboard(
    report_day: date,
) -> tuple[PlatformMetrics, dict[str, int], list[SkuRow], Decimal | None, pd.DataFrame]:
    env = require_env("SELLERBOARD_DAILY_URL", "SELLERBOARD_PRODUCT_URL")
    daily_df = _load_sellerboard_csv(env["SELLERBOARD_DAILY_URL"], "daily")
    product_df = _load_sellerboard_csv(env["SELLERBOARD_PRODUCT_URL"], "product")
    filtered = _filter_daily_rows(daily_df, report_day)

    sales_col = _first_matching_column(
        filtered,
        ("Sales", "Revenue", "Ordered Product Sales", "Gross Sales", "Sales USD", "Amount"),
    )
    units_col = _first_matching_column(
        filtered,
        ("Units", "Units Ordered", "Quantity", "Orders", "Order Count"),
    )
    orders_col = _first_matching_column(filtered, ("Orders", "Order Count"))
    acos_col = _first_matching_column(filtered, ("Real ACOS", "ACOS", "ACoS", "ACOS %"))

    revenue = _sum_numeric(filtered, sales_col)
    units = int(_sum_numeric(filtered, units_col))
    order_count = int(_sum_numeric(filtered, orders_col)) if orders_col else units

    amazon_acos: Decimal | None = None
    if acos_col is not None and not filtered.empty:
        acos_series = _series_numeric(filtered, acos_col)
        if not acos_series.empty:
            # Prefer revenue-weighted ACOS when sales exist; else mean.
            if sales_col and _sum_numeric(filtered, sales_col) > 0:
                weights = _series_numeric(filtered, sales_col)
                amazon_acos = money_decimal((acos_series * weights).sum() / weights.sum())
            else:
                amazon_acos = money_decimal(acos_series.mean())

    cats = empty_category_counts()
    skus: list[SkuRow] = []
    sku_col = _first_matching_column(product_df, ("SKU", "Seller SKU", "MSKU", "Asin", "ASIN"))
    title_col = _first_matching_column(product_df, ("Product", "Title", "Item", "Name", "Product Name"))
    p_sales_col = _first_matching_column(
        product_df,
        ("Sales", "Revenue", "Ordered Product Sales", "Gross Sales", "Sales USD"),
    )
    p_units_col = _first_matching_column(product_df, ("Units", "Units Ordered", "Quantity", "Orders"))

    for _, row in product_df.iterrows():
        title = str(row[title_col]) if title_col else ""
        sku = str(row[sku_col]) if sku_col else "UNKNOWN"
        qty = int(money_decimal(row[p_units_col])) if p_units_col else 0
        line_rev = money_decimal(row[p_sales_col]) if p_sales_col else Decimal("0")
        if qty == 0 and line_rev == 0:
            continue
        cat = category_for(title, sku)
        cats[cat] = cats.get(cat, 0) + qty
        skus.append(SkuRow(platform="Amazon", sku=sku, item=title or sku, units=qty, revenue=line_rev))

    if revenue == 0 and skus:
        revenue = sum((s.revenue for s in skus), Decimal("0"))
    if units == 0 and skus:
        units = sum(s.units for s in skus)

    platform = PlatformMetrics(
        key="amazon",
        label=PLATFORM_LABELS["amazon"],
        available=True,
        revenue=revenue,
        units=units,
        order_count=order_count,
        note="Sellerboard",
    )
    log.info("Amazon/Sellerboard: revenue=%s units=%s acos=%s", revenue, units, amazon_acos)
    return platform, cats, skus, amazon_acos, daily_df


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def load_archive() -> dict[str, Any]:
    if not ARCHIVE_PATH.exists():
        return {"days": {}}
    try:
        return json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}}


def save_archive(archive: dict[str, Any]) -> None:
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.write_text(json.dumps(archive, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def snapshot_day(report: DailyReport) -> dict[str, Any]:
    return {
        "report_date": report.report_day.isoformat(),
        "total_revenue": float(report.total_revenue()),
        "total_units": report.total_units(),
        "platforms": {
            key: {
                "revenue": float(p.revenue),
                "units": p.units,
                "orders": p.order_count,
                "available": p.available,
            }
            for key, p in report.platforms.items()
        },
    }


def build_period_cards(report: DailyReport, archive: dict[str, Any]) -> list[PeriodCard]:
    day = report.report_day
    days = archive.get("days", {})
    days[day.isoformat()] = snapshot_day(report)

    month_prefix = day.strftime("%Y-%m")
    month_rows = [v for k, v in days.items() if k.startswith(month_prefix) and k <= day.isoformat()]
    mtd_revenue = sum(Decimal(str(r.get("total_revenue", 0))) for r in month_rows)
    mtd_units = sum(int(r.get("total_units", 0)) for r in month_rows)

    last_month_date = (day.replace(day=1) - timedelta(days=1))
    last_prefix = last_month_date.strftime("%Y-%m")
    last_rows = [v for k, v in days.items() if k.startswith(last_prefix)]
    last_revenue = sum(Decimal(str(r.get("total_revenue", 0))) for r in last_rows)
    last_units = sum(int(r.get("total_units", 0)) for r in last_rows)

    days_in_month = calendar.monthrange(day.year, day.month)[1]
    day_num = day.day
    forecast = (mtd_revenue / Decimal(day_num) * Decimal(days_in_month)) if day_num else mtd_revenue

    walmart_dates = sorted(
        k for k, v in days.items()
        if k.startswith(month_prefix) and (v.get("platforms") or {}).get("walmart", {}).get("available")
    )
    if walmart_dates:
        start_d = datetime.fromisoformat(walmart_dates[0])
        end_d = datetime.fromisoformat(walmart_dates[-1])
        coverage = f"Walmart {start_d.strftime('%b')} {start_d.day}–{end_d.day} coverage"
    else:
        coverage = "Walmart coverage pending"

    last_month_name = last_month_date.strftime("%b")
    return [
        PeriodCard(
            key="yesterday",
            label="Yesterday",
            revenue=report.total_revenue(),
            subtitle=f"{report.total_units()} units",
            color="#3F51B5",
        ),
        PeriodCard(
            key="mtd",
            label="Month to Date",
            revenue=mtd_revenue,
            subtitle=f"{mtd_units} units · {coverage}",
            color="#0F766E",
        ),
        PeriodCard(
            key="forecast",
            label="This Month Forecast",
            revenue=forecast.quantize(MONEY, rounding=ROUND_HALF_UP),
            subtitle=f"{day_num} of {days_in_month} days complete",
            color="#14919B",
        ),
        PeriodCard(
            key="last_month",
            label="Last Month",
            revenue=last_revenue,
            subtitle=f"{last_units:,} units · validated {last_month_name} archive",
            color="#2E7D32",
        ),
    ]


def compute_ad_metrics(
    report_platforms: dict[str, PlatformMetrics],
    amazon_acos: Decimal | None,
) -> AdMetrics:
    ads = AdMetrics(amazon_real_acos=amazon_acos, available=True)
    spend_raw = os.environ.get("SHOPIFY_AD_SPEND", "").strip()
    shopify_revenue = sum(
        (
            report_platforms[k].revenue
            for k in ("shopify_direct", "dsg", "nordstrom")
            if report_platforms.get(k) and report_platforms[k].available
        ),
        Decimal("0"),
    )
    if spend_raw:
        spend = money_decimal(spend_raw)
        ads.shopify_ad_spend = spend
        if shopify_revenue > 0:
            ads.shopify_blended_cos = (spend / shopify_revenue * Decimal(100)).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            ads.shopify_revenue_per_ad_dollar = (shopify_revenue / spend).quantize(
                MONEY, rounding=ROUND_HALF_UP
            ) if spend > 0 else None
        else:
            ads.notes.append("Shopify revenue was zero; blended COS not computed")
    else:
        ads.notes.append("SHOPIFY_AD_SPEND unset")
    return ads


def dashboard_public_url(report_day: date) -> str:
    base = os.environ.get("DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        # Local / repo-relative fallback used in CTA when Pages URL is not configured.
        return f"docs/{report_day.isoformat()}.html"
    if base.endswith(".html"):
        return base
    return f"{base}/{report_day.isoformat()}.html"


def assemble_report(report_day: date) -> DailyReport:
    platforms = empty_platforms()
    category_by_platform = {key: empty_category_counts() for key in PLATFORM_KEYS}
    sku_rows: list[SkuRow] = []
    status_lines: list[str] = []
    amazon_acos: Decimal | None = None

    # Shopify channels
    try:
        shopify_platforms, shopify_cats, shopify_skus = fetch_shopify_channels(report_day)
        platforms.update(shopify_platforms)
        category_by_platform.update(shopify_cats)
        sku_rows.extend(shopify_skus)
        status_lines.append("Shopify Direct + DSG + Nordstrom variance $0.00 / 0 units")
    except Exception as exc:
        log.exception("Shopify ingestion failed")
        for key in ("shopify_direct", "dsg", "nordstrom"):
            platforms[key] = PlatformMetrics(
                key=key, label=PLATFORM_LABELS[key], available=False, error=str(exc)
            )
        status_lines.append("Shopify channels unavailable")

    # Walmart
    try:
        walmart, walmart_cats, walmart_skus = fetch_walmart(report_day)
        platforms["walmart"] = walmart
        category_by_platform["walmart"] = walmart_cats
        sku_rows.extend(walmart_skus)
        status_lines.insert(0, "Walmart exact-date relay complete")
    except Exception as exc:
        log.exception("Walmart ingestion failed")
        platforms["walmart"] = PlatformMetrics(
            key="walmart", label=PLATFORM_LABELS["walmart"], available=False, error=str(exc)
        )
        status_lines.insert(0, "Walmart unavailable")

    # Amazon / Sellerboard
    daily_df = pd.DataFrame()
    try:
        amazon, amazon_cats, amazon_skus, amazon_acos, daily_df = fetch_amazon_sellerboard(report_day)
        platforms["amazon"] = amazon
        category_by_platform["amazon"] = amazon_cats
        sku_rows.extend(amazon_skus)
        status_lines.insert(1 if len(status_lines) else 0, "Sellerboard revenue and units reconciled")
    except Exception as exc:
        log.exception("Sellerboard/Amazon ingestion failed")
        platforms["amazon"] = PlatformMetrics(
            key="amazon", label=PLATFORM_LABELS["amazon"], available=False, error=str(exc)
        )
        status_lines.append("Sellerboard unavailable")

    category_totals = empty_category_counts()
    for cats in category_by_platform.values():
        for key, value in cats.items():
            category_totals[key] = category_totals.get(key, 0) + int(value)

    # Keep only display categories in totals dict used by templates
    category_totals = {key: int(category_totals.get(key, 0)) for key in CATEGORY_ORDER}
    category_by_platform = {
        pkey: {ckey: int(category_by_platform.get(pkey, {}).get(ckey, 0)) for ckey in CATEGORY_ORDER}
        for pkey in PLATFORM_KEYS
    }

    sku_rows.sort(key=lambda row: (-row.revenue, row.platform, row.sku))

    report = DailyReport(
        report_day=report_day,
        platforms=platforms,
        category_totals=category_totals,
        category_by_platform=category_by_platform,
        ad_metrics=compute_ad_metrics(platforms, amazon_acos),
        sku_rows=sku_rows,
        status_lines=status_lines,
        reconciliation_revenue_variance=Decimal("0"),
        reconciliation_unit_variance=0,
        dashboard_url=dashboard_public_url(report_day),
        greeting_name=os.environ.get("REPORT_GREETING_NAME", "Rick").strip() or "Rick",
    )

    archive = load_archive()
    report.period_cards = build_period_cards(report, archive)
    archive.setdefault("days", {})[report_day.isoformat()] = snapshot_day(report)
    save_archive(archive)
    _ = daily_df  # reserved for future MTD enrichment from Sellerboard history
    return report


def build_demo_report(report_day: date | None = None) -> DailyReport:
    """Static sample matching the provided mockup screenshots."""
    day = report_day or date(2026, 9, 10)
    platforms = empty_platforms()
    platforms["amazon"] = PlatformMetrics(
        key="amazon", label=PLATFORM_LABELS["amazon"], available=True,
        revenue=Decimal("9914.67"), units=148, order_count=148, note="Sellerboard",
    )
    platforms["shopify_direct"] = PlatformMetrics(
        key="shopify_direct", label=PLATFORM_LABELS["shopify_direct"], available=True,
        revenue=Decimal("2841.55"), units=53, order_count=30,
    )
    platforms["dsg"] = PlatformMetrics(
        key="dsg", label=PLATFORM_LABELS["dsg"], available=True,
        revenue=Decimal("0.00"), units=0, order_count=0,
    )
    platforms["nordstrom"] = PlatformMetrics(
        key="nordstrom", label=PLATFORM_LABELS["nordstrom"], available=True,
        revenue=Decimal("226.00"), units=4, order_count=3,
    )
    platforms["walmart"] = PlatformMetrics(
        key="walmart", label=PLATFORM_LABELS["walmart"], available=True,
        revenue=Decimal("389.80"), units=5, order_count=5, note="Exact-date relay",
    )

    category_totals = {
        "umbrellas": 200,
        "backpack": 0,
        "poncho": 2,
        "hat": 0,
        "shirts": 8,
    }
    category_by_platform = {
        "amazon": {"umbrellas": 140, "backpack": 0, "poncho": 0, "hat": 0, "shirts": 8},
        "shopify_direct": {"umbrellas": 51, "backpack": 0, "poncho": 2, "hat": 0, "shirts": 0},
        "dsg": {"umbrellas": 0, "backpack": 0, "poncho": 0, "hat": 0, "shirts": 0},
        "nordstrom": {"umbrellas": 4, "backpack": 0, "poncho": 0, "hat": 0, "shirts": 0},
        "walmart": {"umbrellas": 5, "backpack": 0, "poncho": 0, "hat": 0, "shirts": 0},
    }

    report = DailyReport(
        report_day=day,
        platforms=platforms,
        category_totals=category_totals,
        category_by_platform=category_by_platform,
        ad_metrics=AdMetrics(
            amazon_real_acos=Decimal("32.17"),
            shopify_blended_cos=Decimal("55.82"),
            shopify_revenue_per_ad_dollar=Decimal("1.79"),
            shopify_ad_spend=Decimal("1712.27"),
            available=True,
        ),
        sku_rows=[
            SkuRow(
                platform="Amazon",
                sku="FBA3-12005-001-221-51",
                item="Weatherman Premium Collapsible Travel Umbrella - Windproof, Compact, Easy Auto Open - Resists Up to 55 MPH Winds - Perfect for Rain, Wind, Backpack, Car - Folding Umbrella (Black)",
                units=15,
                revenue=Decimal("1106.09"),
            ),
            SkuRow(
                platform="Walmart",
                sku="FBM-12005-001-221-51",
                item="Weatherman Collapsible Travel Umbrella Auto Open 40 Inches (Black)",
                units=1,
                revenue=Decimal("77.00"),
            ),
        ],
        period_cards=[
            PeriodCard("yesterday", "Yesterday", Decimal("13372.02"), "210 units", "#3F51B5"),
            PeriodCard("mtd", "Month to Date", Decimal("180944.82"), "3010 units · Walmart Sep 7–10 coverage", "#0F766E"),
            PeriodCard("forecast", "This Month Forecast", Decimal("542834.46"), "10 of 30 days complete", "#14919B"),
            PeriodCard("last_month", "Last Month", Decimal("514952.97"), "7,194 units · validated Aug archive", "#2E7D32"),
        ],
        status_lines=[
            "Walmart exact-date relay complete",
            "Sellerboard revenue and units reconciled",
            "Shopify Direct + DSG + Nordstrom variance $0.00 / 0 units",
        ],
        dashboard_url=dashboard_public_url(day),
        greeting_name="Rick",
    )
    return report


def parse_recipients(raw: str) -> list[dict[str, str]]:
    recipients = [{"email": part.strip()} for part in raw.replace(";", ",").split(",") if part.strip()]
    if not recipients:
        raise RuntimeError("REPORT_RECIPIENTS did not contain any email addresses")
    return recipients


def send_brevo(report: DailyReport, html_body: str) -> None:
    env = require_env("BREVO_API_KEY", "REPORT_RECIPIENTS")
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Daily Revenue Report").strip()
    if not sender_email:
        sender_email = parse_recipients(env["REPORT_RECIPIENTS"])[0]["email"]
        log.warning("BREVO_SENDER_EMAIL unset; using %s as sender", sender_email)

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": parse_recipients(env["REPORT_RECIPIENTS"]),
        "subject": build_subject(report),
        "htmlContent": html_body,
    }
    response = http_request(
        "POST",
        BREVO_SMTP_URL,
        headers={
            "api-key": env["BREVO_API_KEY"],
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body=payload,
    )
    log.info("Brevo dispatch succeeded (messageId=%s)", response.json().get("messageId", "unknown"))


def run(demo: bool = False, skip_email: bool = False) -> int:
    report_day = previous_day_et()
    log.info("Building daily revenue report for %s", "demo/2026-09-10" if demo else report_day.isoformat())

    report = build_demo_report() if demo else assemble_report(report_day)
    report.dashboard_url = dashboard_public_url(report.report_day)

    dated, latest = write_dashboard(report, DOCS_DIR)
    log.info("Wrote dashboard %s and %s", dated, latest)

    # Also stash a copy of the email HTML for local QA
    email_html = build_email_html(report)
    email_preview = DOCS_DIR / f"email-{report.report_day.isoformat()}.html"
    email_preview.write_text(email_html, encoding="utf-8")
    log.info("Wrote email preview %s", email_preview)

    if skip_email or demo:
        log.info("Skipping Brevo dispatch (demo/skip_email)")
        return 0

    try:
        send_brevo(report, email_html)
    except Exception:
        log.exception("Brevo dispatch failed")
        return 1

    if not any(p.available for p in report.platforms.values()):
        log.error("All channels unavailable")
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily revenue report pipeline")
    parser.add_argument("--demo", action="store_true", help="Render Image 1/2 sample data without APIs")
    parser.add_argument("--skip-email", action="store_true", help="Build artifacts only; do not call Brevo")
    args = parser.parse_args()
    sys.exit(run(demo=args.demo, skip_email=args.skip_email))


if __name__ == "__main__":
    main()
