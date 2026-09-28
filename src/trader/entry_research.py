"""Descriptive entry research; no simulated orders or automatic parameter selection."""

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

LIMITATIONS = (
    "報價變化與扣除當時預估來回成本的差額，均不是成交損益或策略勝率。"
    "成本已包含預估費用、價差及滑價，只扣一次；未模擬持倉、退出成交或資金限制。"
    "樣本每輪重疊且相關，不是獨立交易；不同方向可能出現在不同時段。"
    "分箱固定為絕對均線差距 0–15、15–30、30–45、45+ bps，並非最佳化門檻。"
    "沒有方向欄位的舊資料不補造；缺少未來報價保持未配對。需用新期間驗證。"
)


def strength(value: Any) -> str:
    if value is None:
        return "UNAVAILABLE"
    number = abs(Decimal(str(value)))
    for upper, label in ((15, "0-15"), (30, "15-30"), (45, "30-45")):
        if number < upper:
            return label
    return "45+"


def research(events: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [e for e in events if "hourly_direction" in e["payload"]]
    entries = [e for e in samples if e["payload"]["comparison_eligible"]]
    quotes: dict[str, dict[datetime, Decimal]] = defaultdict(dict)
    for e in samples:
        p = e["payload"]
        quotes[p["symbol"]][datetime.fromisoformat(p["quote_timestamp"])] = Decimal(
            p["reference_price"]
        )
    timelines = {symbol: sorted(values) for symbol, values in quotes.items()}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    details = []
    for e in entries:
        p = e["payload"]
        symbol = p["symbol"]
        state = p["hourly_direction"]["state"]
        row: dict[str, Any] = {
            "timestamp": e["timestamp"], "symbol": symbol, "direction": state,
            "signal_5m_bps": p["signal_proxy_bps"],
            "signal_1h_signed_bps": p["hourly_direction"].get("signed_gap_bps"),
            "cost_blocked": p["cost_only_would_skip"],
            "estimated_round_trip_cost_bps": p["estimated_round_trip_cost_bps"],
            "forward": {},
        }
        for hours in (1, 4):
            target = datetime.fromisoformat(e["timestamp"]) + timedelta(hours=hours)
            timeline = timelines[symbol]
            index = bisect_left(timeline, target)
            match = None
            if index < len(timeline) and timeline[index] <= target + timedelta(minutes=10):
                price = quotes[symbol][timeline[index]]
                change = (price / Decimal(p["reference_price"]) - 1) * 10000
                match = {
                    "quote_timestamp": timeline[index].isoformat(),
                    "price_return_bps": str(change),
                    "after_estimated_cost_bps": str(
                        change - Decimal(p["estimated_round_trip_cost_bps"])
                    ),
                }
            row["forward"][f"{hours}h"] = match
        details.append(row)
        dimensions = {
            "all": "ALL", "symbol": symbol, "direction": state,
            "cost_gate": "BLOCKED" if row["cost_blocked"] else "PASSED",
            "strength_5m": strength(row["signal_5m_bps"]),
            "strength_1h": strength(row["signal_1h_signed_bps"]),
            "symbol_direction": f"{symbol}/{state}",
            "symbol_direction_5m_1h": (
                f"{symbol}/{state}/{strength(row['signal_5m_bps'])}/"
                f"{strength(row['signal_1h_signed_bps'])}"
            ),
        }
        for dimension, value in dimensions.items():
            groups[dimension, value].append(row)
    summaries = []
    for (dimension, value), rows in sorted(groups.items()):
        horizons = {}
        for horizon in ("1h", "4h"):
            matches = [r["forward"][horizon] for r in rows if r["forward"][horizon] is not None]
            raw = [Decimal(m["price_return_bps"]) for m in matches]
            net = [Decimal(m["after_estimated_cost_bps"]) for m in matches]
            horizons[horizon] = {
                "matched": len(matches), "unmatched": len(rows) - len(matches),
                "mean_price_return_bps": str(sum(raw) / len(raw)) if raw else None,
                "median_price_return_bps": str(median(raw)) if raw else None,
                "mean_after_estimated_cost_bps": str(sum(net) / len(net)) if net else None,
                "median_after_estimated_cost_bps": str(median(net)) if net else None,
                "above_estimated_cost": sum(n > 0 for n in net),
            }
        summaries.append({
            "dimension": dimension, "value": value, "observations": len(rows),
            "forward": horizons,
        })
    return {
        "version": "all-entry-research-v1", "observations": len(entries),
        "excluded_missing_direction": len(events) - len(samples),
        "excluded_not_flat_or_fresh": len(samples) - len(entries),
        "limitations": LIMITATIONS, "groups": summaries, "samples": details,
    }
