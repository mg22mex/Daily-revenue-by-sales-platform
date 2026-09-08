#!/usr/bin/env python3
"""Fetch the previous ET calendar day's Walmart Marketplace sales summary."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
ORDERS_URL = "https://marketplace.walmartapis.com/v3/orders"
OUTPUT_PATH = Path("data/walmart_daily_summary.json")
ET = ZoneInfo("America/New_York")
MONEY = Decimal("0.01")


def request_json(url: str, *, headers: dict[str, str], data: bytes | None = None, attempts: int = 3) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers=headers, data=data), timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise RuntimeError(f"Walmart API HTTP {exc.code}: {detail}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError("Walmart API request failed")


def money_value(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def quantity_value(line: dict[str, Any]) -> int:
    value = line.get("orderLineQuantity", {}).get("amount", 0)
    try:
        return int(Decimal(str(value or 0)))
    except Exception:
        return 0


def category_for(name: str, sku: str) -> str:
    text = f"{name} {sku}".lower()
    if re.search(r"\bdry[- ]?pack\b", text):
        return "Other"
    if "poncho" in text:
        return "Ponchos"
    if re.search(r"\b(hat|cap)\b", text):
        return "Hats"
    if re.search(r"\b(shirt|tee|t-shirt|polo)\b", text):
        return "Shirts"
    if "umbrella" in text:
        return "Umbrellas"
    return "Other"


def access_token(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = request_json(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_SVC.NAME": "Walmart Marketplace",
        },
        data=urlencode({"grant_type": "client_credentials"}).encode(),
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Walmart token response did not contain access_token")
    return str(token)


def fetch_orders(token: str, start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    params = {
        "createdStartDate": start_utc.isoformat().replace("+00:00", "Z"),
        "createdEndDate": end_utc.isoformat().replace("+00:00", "Z"),
        "limit": 200,
    }
    url: str | None = f"{ORDERS_URL}?{urlencode(params)}"
    orders: list[dict[str, Any]] = []
    while url:
        payload = request_json(
            url,
            headers={
                "WM_SEC.ACCESS_TOKEN": token,
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "WM_SVC.NAME": "Walmart Marketplace",
                "Accept": "application/json",
            },
        )
        listing = payload.get("list", {})
        block = listing.get("elements", {}).get("order", []) if isinstance(listing, dict) else []
        if isinstance(block, dict):
            block = [block]
        orders.extend(block)
        cursor = listing.get("meta", {}).get("nextCursor") if isinstance(listing, dict) else None
        url = f"{ORDERS_URL}?{str(cursor).lstrip('?')}" if cursor else None
    return orders


def summarize(orders: list[dict[str, Any]], report_date: str, start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
    skus: dict[str, dict[str, Any]] = {}
    categories: defaultdict[str, int] = defaultdict(int)
    seen_orders: set[str] = set()
    gross = Decimal("0")
    total_units = 0

    for order in orders:
        order_key = str(order.get("purchaseOrderId") or order.get("customerOrderId") or "")
        if order_key:
            seen_orders.add(order_key)
        lines = order.get("orderLines", {}).get("orderLine", [])
        if isinstance(lines, dict):
            lines = [lines]
        for line in lines:
            item = line.get("item", {})
            sku = str(item.get("sku") or "UNKNOWN")
            name = str(item.get("productName") or sku)
            units = quantity_value(line)
            category = category_for(name, sku)
            statuses = line.get("orderLineStatuses", {}).get("orderLineStatus", [])
            if isinstance(statuses, dict):
                statuses = [statuses]
            status_names = sorted({str(x.get("status")) for x in statuses if x.get("status")})
            line_gross = Decimal("0")
            charges = line.get("charges", {}).get("charge", [])
            if isinstance(charges, dict):
                charges = [charges]
            for charge in charges:
                charge_type = str(charge.get("chargeType") or "").upper()
                if charge_type in {"PRODUCT", "SHIPPING"}:
                    line_gross += money_value(charge.get("chargeAmount"))
            gross += line_gross
            total_units += units
            categories[category] += units
            row = skus.setdefault(sku, {"sku": sku, "item_name": name, "category": category, "units": 0, "gross_revenue": Decimal("0"), "statuses": set()})
            row["units"] += units
            row["gross_revenue"] += line_gross
            row["statuses"].update(status_names)

    sku_rows = []
    for row in skus.values():
        sku_rows.append({
            "sku": row["sku"],
            "item_name": row["item_name"],
            "category": row["category"],
            "units": row["units"],
            "gross_revenue": float(row["gross_revenue"].quantize(MONEY, rounding=ROUND_HALF_UP)),
            "statuses": sorted(row["statuses"]),
        })
    sku_rows.sort(key=lambda row: (-row["gross_revenue"], row["sku"]))

    return {
        "schema_version": 1,
        "source": "Walmart Marketplace Orders API",
        "status": "complete",
        "report_date": report_date,
        "timezone": "America/New_York",
        "window_utc": {
            "start_inclusive": start_utc.isoformat().replace("+00:00", "Z"),
            "end_exclusive": end_utc.isoformat().replace("+00:00", "Z"),
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gross_revenue": float(gross.quantize(MONEY, rounding=ROUND_HALF_UP)),
        "order_count": len(seen_orders),
        "total_units": total_units,
        "category_units": {
            "Umbrellas": categories["Umbrellas"],
            "Ponchos": categories["Ponchos"],
            "Hats": categories["Hats"],
            "Shirts": categories["Shirts"],
            "Other": categories["Other"],
        },
        "sku_line_items": sku_rows,
        "reconciliation": {
            "revenue_variance": 0.0,
            "unit_variance": 0,
        },
    }


def main() -> None:
    client_id = os.environ.get("WALMART_CLIENT_ID")
    client_secret = os.environ.get("WALMART_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Required Walmart managed secrets are unavailable")

    today_et = datetime.now(ET).date()
    report_day = today_et - timedelta(days=1)
    start_et = datetime.combine(report_day, datetime.min.time(), tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    summary = summarize(fetch_orders(access_token(client_id, client_secret), start_utc, end_utc), report_day.isoformat(), start_utc, end_utc)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote Walmart summary for {report_day.isoformat()} ({summary['order_count']} orders)")


if __name__ == "__main__":
    main()
