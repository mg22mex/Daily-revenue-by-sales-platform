#!/usr/bin/env python3
"""Daily multi-platform revenue report: Shopify + Walmart + Sellerboard → Brevo."""
from __future__ import annotations

import base64
import html
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("daily_report")

ET = ZoneInfo("America/New_York")
MONEY = Decimal("0.01")

SHOPIFY_API_VERSION = "2024-10"
WALMART_TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
WALMART_ORDERS_URL = "https://marketplace.walmartapis.com/v3/orders"
WALMART_STATUSES = ("Created", "Acknowledged", "Shipped", "Delivered")
WALMART_SHIP_NODE_TYPES = ("SellerFulfilled", "WFSFulfilled")
BREVO_SMTP_URL = "https://api.brevo.com/v3/smtp/email"

HTTP_TIMEOUT = 60
HTTP_RETRIES = 3


@dataclass
class ChannelMetrics:
    """Normalized previous-day metrics for one sales channel."""

    name: str
    available: bool = False
    revenue: Decimal = Decimal("0")
    order_count: int = 0
    currency: str = "USD"
    extra: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def revenue_display(self) -> str:
        if not self.available:
            return "Unavailable"
        return f"{self.currency} {self.revenue.quantize(MONEY, rounding=ROUND_HALF_UP):,.2f}"

    def orders_display(self) -> str:
        if not self.available:
            return "—"
        return f"{self.order_count:,}"


def previous_day_et(now: datetime | None = None) -> date:
    current = now or datetime.now(ET)
    return current.astimezone(ET).date() - timedelta(days=1)


def day_window_et(report_day: date) -> tuple[datetime, datetime]:
    """Inclusive start / exclusive end for a calendar day in America/New_York."""
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
    data: bytes | dict[str, Any] | None = None,
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
                    "HTTP %s from %s — retrying in %ss (attempt %s/%s)",
                    response.status_code,
                    url.split("?")[0],
                    wait,
                    attempt + 1,
                    retries,
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


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------


def fetch_shopify(report_day: date) -> ChannelMetrics:
    env = require_env("SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN")
    store = env["SHOPIFY_STORE_URL"].rstrip("/").removeprefix("https://").removeprefix("http://")
    token = env["SHOPIFY_ACCESS_TOKEN"]
    start, end = day_window_et(report_day)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    endpoint = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    query = """
    query OrdersPage($cursor: String, $query: String!) {
      orders(first: 100, after: $cursor, query: $query, sortKey: CREATED_AT) {
        edges {
          node {
            id
            name
            createdAt
            displayFinancialStatus
            totalPriceSet {
              shopMoney { amount currencyCode }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    search = f"created_at:>={start_iso} created_at:<{end_iso} status:any"
    cursor: str | None = None
    revenue = Decimal("0")
    order_count = 0
    currency = "USD"

    while True:
        payload = {
            "query": query,
            "variables": {"cursor": cursor, "query": search},
        }
        response = http_request("POST", endpoint, headers=headers, json_body=payload)
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")

        orders = body.get("data", {}).get("orders") or {}
        for edge in orders.get("edges") or []:
            node = edge.get("node") or {}
            money = (node.get("totalPriceSet") or {}).get("shopMoney") or {}
            amount = money_decimal(money.get("amount"))
            revenue += amount
            order_count += 1
            if money.get("currencyCode"):
                currency = str(money["currencyCode"])

        page = orders.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
            continue
        break

    log.info("Shopify: %s orders, revenue=%s %s", order_count, revenue, currency)
    return ChannelMetrics(
        name="Shopify",
        available=True,
        revenue=revenue,
        order_count=order_count,
        currency=currency,
    )


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
        charge_type = str(charge.get("chargeType") or "").upper()
        if charge_type in {"PRODUCT", "SHIPPING"}:
            total += money_decimal(charge.get("chargeAmount"))
    return total


def fetch_walmart(report_day: date) -> ChannelMetrics:
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

    revenue = Decimal("0")
    for order in unique.values():
        lines = order.get("orderLines", {}).get("orderLine", [])
        if isinstance(lines, dict):
            lines = [lines]
        for line in lines:
            revenue += _walmart_line_gross(line)

    order_count = len(unique)
    log.info("Walmart: %s orders, revenue=%s USD", order_count, revenue)
    return ChannelMetrics(
        name="Walmart",
        available=True,
        revenue=revenue,
        order_count=order_count,
        currency="USD",
    )


# ---------------------------------------------------------------------------
# Sellerboard
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


def _sum_numeric_column(df: pd.DataFrame, column: str | None) -> Decimal:
    if column is None or column not in df.columns:
        return Decimal("0")
    series = pd.to_numeric(
        df[column].astype(str).str.replace(r"[,$]", "", regex=True),
        errors="coerce",
    ).fillna(0)
    return money_decimal(series.sum())


def _load_sellerboard_csv(url: str, label: str) -> pd.DataFrame:
    response = http_request("GET", url, headers={"Accept": "text/csv,*/*"})
    content_type = (response.headers.get("Content-Type") or "").lower()
    text = response.text
    if "html" in content_type or text.lstrip().lower().startswith("<!DOCTYPE") or text.lstrip().lower().startswith("<html"):
        raise RuntimeError(
            f"Sellerboard {label} URL returned HTML instead of CSV "
            "(URL may have expired or require a permanent automation token)"
        )
    df = pd.read_csv(StringIO(text))
    if df.empty:
        log.warning("Sellerboard %s CSV is empty", label)
    return df


def fetch_sellerboard(report_day: date) -> ChannelMetrics:
    env = require_env("SELLERBOARD_DAILY_URL", "SELLERBOARD_PRODUCT_URL")
    daily_df = _load_sellerboard_csv(env["SELLERBOARD_DAILY_URL"], "daily")
    product_df = _load_sellerboard_csv(env["SELLERBOARD_PRODUCT_URL"], "product")

    # Prefer daily export for headline sales; fall back to product rollup.
    date_col = _first_matching_column(daily_df, ("Date", "Day", "Report Date", "date"))
    sales_col = _first_matching_column(
        daily_df,
        ("Sales", "Revenue", "Ordered Product Sales", "Gross Sales", "Sales USD", "Amount"),
    )
    orders_col = _first_matching_column(
        daily_df,
        ("Orders", "Order Count", "Units Ordered", "Units", "Quantity"),
    )

    filtered = daily_df
    if date_col is not None:
        parsed = pd.to_datetime(daily_df[date_col], errors="coerce", utc=True)
        target = pd.Timestamp(report_day, tz="UTC")
        mask = parsed.dt.floor("D") == target
        if mask.any():
            filtered = daily_df.loc[mask]
        else:
            # Some daily exports are already scoped to "yesterday" with no date column match.
            log.info(
                "Sellerboard daily CSV has no rows for %s; using full daily export",
                report_day.isoformat(),
            )

    revenue = _sum_numeric_column(filtered, sales_col)
    order_count = int(_sum_numeric_column(filtered, orders_col))

    if revenue == 0 and sales_col is None:
        product_sales_col = _first_matching_column(
            product_df,
            ("Sales", "Revenue", "Ordered Product Sales", "Gross Sales", "Sales USD"),
        )
        product_orders_col = _first_matching_column(
            product_df,
            ("Orders", "Units", "Units Ordered", "Quantity", "Sessions"),
        )
        revenue = _sum_numeric_column(product_df, product_sales_col)
        if order_count == 0:
            order_count = int(_sum_numeric_column(product_df, product_orders_col))

    extra = {
        "daily_rows": int(len(filtered)),
        "product_rows": int(len(product_df)),
        "sales_column": sales_col,
        "orders_column": orders_col,
    }
    log.info(
        "Sellerboard: revenue=%s USD, orders=%s (daily_rows=%s)",
        revenue,
        order_count,
        extra["daily_rows"],
    )
    return ChannelMetrics(
        name="Sellerboard",
        available=True,
        revenue=revenue,
        order_count=order_count,
        currency="USD",
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Aggregation & Brevo
# ---------------------------------------------------------------------------


def safe_fetch(fetcher, report_day: date, channel_name: str) -> ChannelMetrics:
    try:
        return fetcher(report_day)
    except Exception as exc:
        log.exception("%s ingestion failed", channel_name)
        return ChannelMetrics(name=channel_name, available=False, error=str(exc))


def build_html_report(report_day: date, channels: list[ChannelMetrics]) -> str:
    available = [c for c in channels if c.available]
    total_revenue = sum((c.revenue for c in available), Decimal("0"))
    total_orders = sum(c.order_count for c in available)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = []
    for channel in channels:
        status = "OK" if channel.available else "Unavailable"
        err = (
            f'<br><span style="color:#b42318;font-size:12px;">{html.escape(channel.error[:180])}</span>'
            if channel.error
            else ""
        )
        rows.append(
            f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">{html.escape(channel.name)}{err}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">{status}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">{channel.revenue_display()}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">{channel.orders_display()}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Daily Revenue Report</title></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#111827;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        <tr>
          <td style="padding:24px 28px;background:#0f172a;color:#ffffff;">
            <div style="font-size:13px;letter-spacing:0.04em;text-transform:uppercase;opacity:0.8;">Daily Revenue Report</div>
            <div style="font-size:24px;font-weight:600;margin-top:6px;">Report date: {report_day.isoformat()}</div>
            <div style="font-size:13px;margin-top:8px;opacity:0.85;">Window: America/New_York · Generated {generated}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 28px;">
            <table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:18px;">
              <tr>
                <td style="padding:14px 16px;background:#f1f5f9;border-radius:6px;">
                  <div style="font-size:12px;color:#64748b;text-transform:uppercase;">Combined revenue</div>
                  <div style="font-size:22px;font-weight:600;margin-top:4px;">USD {total_revenue.quantize(MONEY, rounding=ROUND_HALF_UP):,.2f}</div>
                </td>
                <td width="12"></td>
                <td style="padding:14px 16px;background:#f1f5f9;border-radius:6px;">
                  <div style="font-size:12px;color:#64748b;text-transform:uppercase;">Combined orders</div>
                  <div style="font-size:22px;font-weight:600;margin-top:4px;">{total_orders:,}</div>
                </td>
              </tr>
            </table>
            <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:14px;">
              <thead>
                <tr style="background:#f8fafc;text-align:left;">
                  <th style="padding:10px 12px;border-bottom:2px solid #e5e7eb;">Channel</th>
                  <th style="padding:10px 12px;border-bottom:2px solid #e5e7eb;">Status</th>
                  <th style="padding:10px 12px;border-bottom:2px solid #e5e7eb;text-align:right;">Revenue</th>
                  <th style="padding:10px 12px;border-bottom:2px solid #e5e7eb;text-align:right;">Orders</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
            <p style="margin:18px 0 0;font-size:12px;color:#64748b;">
              Partial failures are marked Unavailable. Combined totals include only successful channels.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def parse_recipients(raw: str) -> list[dict[str, str]]:
    recipients: list[dict[str, str]] = []
    for part in raw.replace(";", ",").split(","):
        email = part.strip()
        if email:
            recipients.append({"email": email})
    if not recipients:
        raise RuntimeError("REPORT_RECIPIENTS did not contain any email addresses")
    return recipients


def send_brevo(report_day: date, html_body: str, channels: list[ChannelMetrics]) -> None:
    env = require_env("BREVO_API_KEY", "REPORT_RECIPIENTS")
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Daily Revenue Report").strip()
    if not sender_email:
        # Brevo requires a verified sender; fall back to the first recipient if unset.
        sender_email = parse_recipients(env["REPORT_RECIPIENTS"])[0]["email"]
        log.warning("BREVO_SENDER_EMAIL unset; using %s as sender", sender_email)

    available_count = sum(1 for c in channels if c.available)
    subject = (
        f"Daily Revenue Report — {report_day.isoformat()} "
        f"({available_count}/{len(channels)} channels OK)"
    )
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": parse_recipients(env["REPORT_RECIPIENTS"]),
        "subject": subject,
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
    message_id = response.json().get("messageId", "unknown")
    log.info("Brevo dispatch succeeded (messageId=%s)", message_id)


def run() -> int:
    report_day = previous_day_et()
    log.info("Building daily revenue report for %s (America/New_York)", report_day.isoformat())

    channels = [
        safe_fetch(fetch_shopify, report_day, "Shopify"),
        safe_fetch(fetch_walmart, report_day, "Walmart"),
        safe_fetch(fetch_sellerboard, report_day, "Sellerboard"),
    ]

    html_body = build_html_report(report_day, channels)

    try:
        send_brevo(report_day, html_body, channels)
    except Exception:
        log.exception("Brevo dispatch failed")
        return 1

    if not any(c.available for c in channels):
        log.error("All channels unavailable — email sent with Unavailable markers")
        return 2

    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
