"""Product category classification shared across channels."""
from __future__ import annotations

import re


def category_for(name: str, sku: str = "") -> str:
    """Map product title/SKU to report category keys."""
    text = f"{name} {sku}".lower()
    if re.search(r"\bdry[- ]?pack\b", text):
        return "other"
    if "backpack" in text or "back pack" in text:
        return "backpack"
    if "poncho" in text:
        return "poncho"
    if re.search(r"\b(hat|cap)\b", text):
        return "hat"
    if re.search(r"\b(shirt|tee|t-shirt|polo)\b", text):
        return "shirts"
    if "umbrella" in text:
        return "umbrellas"
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
