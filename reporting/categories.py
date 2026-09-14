"""Product category classification shared across channels."""
from __future__ import annotations

import re

# Venture Dry Pack backpack family (Black / Steel Blue / Sage, etc.).
BACKPACK_SKU_RE = re.compile(
    r"(?:^|[^A-Z0-9])(?:FBA[-_ ]*)?WM-?40002(?:[-_ ]|$)",
    re.IGNORECASE,
)


def category_for(name: str, sku: str = "") -> str:
    """Map product title/SKU to report category keys.

    Amazon umbrella titles often include marketing copy like
    ``Perfect for Rain, Wind, Backpack, Car``. Those must stay **umbrellas**,
    not backpacks. Real backpacks are ``WM-40002-*`` Venture Dry Pack SKUs
    (and titles that name a dry pack / hiking backpack without being an umbrella).
    """
    title = str(name or "")
    sku_text = str(sku or "")
    text = f"{title} {sku_text}".lower()

    # Explicit backpack SKU family first (WM-40002-001 / 135 / 310, with FBA prefixes).
    if BACKPACK_SKU_RE.search(sku_text) or BACKPACK_SKU_RE.search(title):
        return "backpack"

    # Product-type keywords: umbrella / poncho / apparel beat use-case "backpack".
    if "umbrella" in text:
        return "umbrellas"
    if "poncho" in text:
        return "poncho"
    if re.search(r"\b(hat|cap)\b", text):
        return "hat"
    if re.search(r"\b(shirt|tee|t-shirt|polo)\b", text):
        return "shirts"

    # True backpack / dry-pack products (not marketing mentions on umbrellas).
    if re.search(r"\bdry[- ]?pack\b", text):
        return "backpack"
    if re.search(r"\b(travel|hiking|outdoor)\s+backpack\b", text):
        return "backpack"
    if re.search(r"\bbackpack\b", text) and "umbrella" not in text:
        return "backpack"

    return "other"


def empty_category_counts() -> dict[str, int]:
    return {
        "umbrellas": 0,
        "backpack": 0,
        "poncho": 0,
        "hat": 0,
        "shirts": 0,
        "other": 0,
    }
