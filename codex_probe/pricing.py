"""Load pricing.json and compute the cost of a call.

Prices are data, not code, because they change. pricing.json looks like:

    {
      "checked_on": "2026-11-13",
      "models": {
        "some-model": {"input": 1.0, "cached_input": 0.1, "output": 4.0}
      }
    }

All prices are USD per 1 million tokens. A local model is listed with zeros.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def load_pricing(path) -> dict:
    """Return the {model: prices} table. A missing file gives an empty table."""
    file = Path(path)
    if not file.exists():
        return {}
    return json.loads(file.read_text()).get("models", {})


def compute_cost(model: Optional[str], usage: Optional[dict], pricing: dict) -> Optional[float]:
    """Cost of one call in USD, or None when it cannot be known.

    None (not 0) is returned when there is no usage data or the model has no
    price entry, so "unknown" is never confused with "free".
    """
    if usage is None or model not in pricing:
        return None
    prices = pricing[model]
    input_price = prices.get("input", 0)
    # If no cached price is listed, cached tokens cost the normal input price.
    cached_price = prices.get("cached_input", input_price)
    output_price = prices.get("output", 0)

    cached = usage["cached_input_tokens"]
    # Cached tokens are part of input_tokens, so bill the two parts separately.
    fresh = usage["input_tokens"] - cached
    cost = (
        fresh * input_price + cached * cached_price + usage["output_tokens"] * output_price
    ) / 1_000_000
    return round(cost, 8)
