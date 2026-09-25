"""Same-input filter comparison and observed forward prices, never hypothetical fills."""

from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any


def study(events: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [e for e in events if "hourly_direction" in e["payload"]]
    eligible = [e for e in samples if e["payload"]["comparison_eligible"]]
    candidates = [e for e in eligible if not e["payload"]["cost_only_would_skip"]]
    quotes: dict[str, dict[datetime, Decimal]] = defaultdict(dict)
    for e in samples:
        p = e["payload"]
        quotes[p["symbol"]][datetime.fromisoformat(p["quote_timestamp"])] = Decimal(
            p["reference_price"]
        )
    timelines = {symbol: sorted(values) for symbol, values in quotes.items()}
    cohorts: dict[str, Any] = {}
    for state in ("UP", "DOWN", "FLAT", "UNAVAILABLE"):
        entries = [e for e in candidates if e["payload"]["hourly_direction"]["state"] == state]
        horizons = {}
        for hours in (1, 4):
            returns = []
            for e in entries:
                p = e["payload"]
                # Start at decision time; do not use a future price that predates the horizon.
                target = datetime.fromisoformat(e["timestamp"]) + timedelta(hours=hours)
                timeline = timelines[p["symbol"]]
                index = bisect_left(timeline, target)
                if index == len(timeline) or timeline[index] > target + timedelta(minutes=10):
                    continue
                price = quotes[p["symbol"]][timeline[index]]
                returns.append((price / Decimal(p["reference_price"]) - 1) * 100)
            horizons[str(hours) + "h"] = {
                "matched": len(returns),
                "unmatched": len(entries) - len(returns),
                "mean_price_return_pct": str(sum(returns, Decimal(0)) / len(returns))
                if returns
                else None,
                "median_price_return_pct": str(median(returns)) if returns else None,
            }
        cohorts[state] = {"cost_eligible_observations": len(entries), "forward_prices": horizons}
    return {
        "observations_with_direction": len(samples),
        "flat_fresh_observations": len(eligible),
        "cost_only_allows_model": len(candidates),
        "cost_and_direction_allows_model": sum(
            not e["payload"]["cost_and_direction_would_skip"] for e in eligible
        ),
        "extra_direction_skips": sum(
            e["payload"]["cost_and_direction_would_skip"] for e in candidates
        ),
        "direction_counts": dict(
            Counter(e["payload"]["hourly_direction"]["state"] for e in eligible)
        ),
        "cohorts": cohorts,
        "limitations": "Same observed inputs; not a portfolio backtest or parallel account. "
        "Forward prices: first quote at/after 1h or 4h within 10 minutes, in this export. "
        "Returns exclude all trading costs and are not profit. Missing future data stays null. "
        "Overlapping samples are correlated, and filtering changes future sampling/account paths. "
        "Old exports without direction fields cannot be retrospectively classified.",
    }
