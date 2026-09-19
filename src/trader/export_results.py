"""Portable read-only research export. No environment, endpoints or raw prompts."""

import io
import json
import zipfile
from collections import Counter
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO

from trader.dashboard import reader
from trader.storage.repository import encode

BOOKS = {
    "sma5m": "experiment-v2/sma5m.db",
    "trend1h": "experiment-v2/trend1h.db",
    "ollama": "ollama/paper.db",
    "legacy": "paper.db",
}
TABLES = {
    "decisions": "SELECT cycle_id,timestamp,result_json FROM paper_cycles ORDER BY timestamp,rowid",
    "orders": "SELECT * FROM orders ORDER BY created_at,id",
    "fills": "SELECT * FROM fills ORDER BY timestamp,id",
    "equity": "SELECT * FROM portfolio_snapshots ORDER BY timestamp,id",
    "risk": "SELECT * FROM risk_assessments ORDER BY timestamp,id",
    "ai_calls": (
        "SELECT id,timestamp,provider,model,error,response FROM ai_calls ORDER BY timestamp,id"
    ),
    "events": "SELECT * FROM system_events ORDER BY timestamp,id",
}


def parse_bound(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(UTC)


def write_export(
    root: Path,
    destination: BinaryIO | SpooledTemporaryFile[bytes],
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    if any(t is not None and t.tzinfo is None for t in (start, end)):
        raise ValueError("Timezone required")
    if start and end and start >= end:
        raise ValueError("Start must precede end")
    selected: dict[str, list[dict[str, Any]]] = {}

    def in_window(item: dict[str, Any]) -> bool:
        stamp = datetime.fromisoformat(item.get("timestamp", item.get("created_at", "")))
        return (start is None or stamp >= start) and (end is None or stamp <= end)

    manifest: dict[str, Any] = {
        "format_version": 2,
        "requested_period": {
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
        },
        "window_semantics": "Inclusive timestamps; ai_calls use request start; "
        "decisions use completion. "
        "Summary uses first/last actual observations inside window; no interpolation.",
        "exported_at": datetime.now(UTC).isoformat(),
        "mode": "PAPER",
        "currency": "USDT",
        "timezone": "UTC",
        "books": {},
        "missing": [],
        "files": {},
        "definitions": {},
        "snapshot_note": "Each SQLite database has its own consistent read snapshot; "
        "snapshots are not atomic across databases. Align by timestamps.",
        "limitations": [
            "No training or automatic strategy modification is performed.",
            "BUY retains SMA edge proxy; SELL gates depend on exit_policy_version.",
            "Equity includes paid fees, not future liquidation fees.",
            "Model calls and price samples have different times; do not assume identical fills.",
        ],
    }
    with ExitStack() as stack, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        databases = {}
        for name, relative in {**BOOKS, "comparison": "experiment-v2/comparison.db"}.items():
            path = root / relative
            if not path.exists():
                manifest["missing"].append(name)
                continue
            db = stack.enter_context(closing(reader(path)))
            db.execute("BEGIN")
            db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # Pin read snapshot.
            databases[name] = db

        def write_rows(name: str, rows: Any) -> None:
            count = 0
            selected[name] = []
            with archive.open(name, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8") as stream:
                for row in rows:
                    item = dict(row)
                    if not in_window(item):
                        continue
                    for key in list(item):
                        if key.endswith("_json") and item[key] is not None:
                            item[key.removesuffix("_json")] = json.loads(item.pop(key))
                    selected[name].append(item)
                    stream.write(encode(item) + "\n")
                    count += 1
            manifest["files"][name] = count

        for name, db in databases.items():
            if name == "comparison":
                meta = {
                    r[0]: json.loads(r[1])
                    for r in db.execute("SELECT key,value FROM experiment_meta")
                }
                manifest["definitions"][name] = meta
                write_rows(
                    "comparison/samples.jsonl",
                    db.execute(
                        "SELECT bucket,timestamp,payload AS sample_json "
                        "FROM experiment_samples ORDER BY bucket"
                    ),
                )
                write_rows(
                    "comparison/errors.jsonl",
                    db.execute("SELECT * FROM experiment_errors ORDER BY id"),
                )
                continue
            state = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
            span = db.execute(
                "SELECT MIN(timestamp),MAX(timestamp) FROM portfolio_snapshots"
            ).fetchone()
            latest = db.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC,id DESC LIMIT 1"
            ).fetchone()
            manifest["books"][name] = {
                "account": dict(state) if state else None,
                "first_valuation": span[0],
                "last_valuation": span[1],
                "last_equity": latest["equity"] if latest else None,
            }
            if name == "ollama":
                definition = db.execute(
                    "SELECT definition FROM local_ai_config WHERE id=1"
                ).fetchone()
                if definition:
                    settings = json.loads(definition[0])
                    settings.pop("base_url", None)
                    manifest["definitions"][name] = settings
            for category, sql in TABLES.items():
                # Push time filtering into SQLite; do not read full history for every export.
                column = "created_at" if category == "orders" else "timestamp"
                filtered = (
                    f"SELECT * FROM ({sql}) "
                    f"WHERE (? IS NULL OR julianday({column}) >= julianday(?)) "
                    f"AND (? IS NULL OR julianday({column}) <= julianday(?))"
                )
                a, b = start.isoformat() if start else None, end.isoformat() if end else None
                write_rows(name + "/" + category + ".jsonl", db.execute(filtered, (a, a, b, b)))
            values = selected[name + "/equity.jsonl"]
            manifest["books"][name].update(
                {
                    "first_valuation": values[0]["timestamp"] if values else None,
                    "last_valuation": values[-1]["timestamp"] if values else None,
                    "last_equity": values[-1]["equity"] if values else None,
                    "account_scope": "Current account at export time; not period opening balance",
                }
            )
            if start:
                opening = db.execute(
                    "SELECT * FROM portfolio_snapshots WHERE julianday(timestamp)<=julianday(?) "
                    "ORDER BY timestamp DESC,id DESC LIMIT 1",
                    (start.isoformat(),),
                ).fetchone()
                archive.writestr(
                    name + "/opening_observation.json", encode(dict(opening) if opening else None)
                )

        spans = [manifest["books"].get(name) for name in ("sma5m", "trend1h", "ollama")]
        if all(s and s["first_valuation"] and s["last_valuation"] for s in spans):
            starts = [datetime.fromisoformat(s["first_valuation"]) for s in spans if s]
            ends = [datetime.fromisoformat(s["last_valuation"]) for s in spans if s]
            first, last = max(starts), min(ends)
            manifest["common_period"] = (
                {"from": first.isoformat(), "to": last.isoformat()} if first <= last else None
            )
        else:
            manifest["common_period"] = None
        analysis: dict[str, Any] = {
            "definitions": manifest["definitions"],
            "period": manifest["requested_period"],
            "portfolios": {},
            "activity": {},
        }

        def metrics(values: list[dict[str, Any]]) -> dict[str, Any] | None:
            if not values:
                return None
            first, last = Decimal(values[0]["equity"]), Decimal(values[-1]["equity"])
            peak, drawdown = first, Decimal(0)
            for row in values:
                value = Decimal(row["equity"])
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak * 100)
            return {
                "from": values[0]["timestamp"],
                "to": values[-1]["timestamp"],
                "opening_equity": str(first),
                "closing_equity": str(last),
                "period_pnl": str(last - first),
                "return_pct": str((last / first - 1) * 100),
                "sampled_drawdown_pct": str(drawdown),
                "observations": len(values),
            }

        for name in BOOKS:
            analysis["portfolios"][name] = metrics(selected.get(name + "/equity.jsonl", []))
            decisions = selected.get(name + "/decisions.jsonl", [])
            calls = selected.get(name + "/ai_calls.jsonl", [])
            analysis["activity"][name] = {
                "statuses": dict(Counter(d["result"]["status"] for d in decisions)),
                "rejected_actions": dict(
                    Counter(
                        d["result"]["action"]
                        for d in decisions
                        if d["result"]["status"] == "REJECTED"
                    )
                ),
                "rejection_reasons": dict(
                    Counter(
                        r
                        for d in decisions
                        if d["result"]["status"] == "REJECTED"
                        for r in d["result"]["reasons"]
                    )
                ),
                "actions": dict(Counter(d["result"]["action"] for d in decisions)),
                "stale_market_checks": sum(
                    "MARKET_DATA_FRESH_VALID" in r["result"]["reasons"]
                    for r in selected.get(name + "/risk.jsonl", [])
                ),
                "calls": len(calls),
                "call_failures": sum(c["error"] is not None for c in calls),
                "fees_paid_in_period": str(
                    sum(
                        (Decimal(f["fee_usdt"]) for f in selected.get(name + "/fills.jsonl", [])),
                        Decimal(0),
                    )
                ),
            }
        samples = selected.get("comparison/samples.jsonl", [])
        for arm in ("hold", "cash"):
            analysis["portfolios"][arm] = metrics(
                [
                    {
                        "timestamp": r["timestamp"],
                        "equity": r["sample"]["portfolios"][arm]["equity"],
                    }
                    for r in samples
                ]
            )
        archive.writestr("summary.json", encode(analysis))
        lines = [
            "# 區間分析摘要",
            "先讀 summary.json；詳細查核再讀各組 JSONL。",
            "損益使用區間內首末實際估值，時間可能與所選邊界不同；不補造價格。",
            "費用已包含於淨值，不可重複扣除；尚未扣未來退出成本。",
            "",
        ]
        for name, value in analysis["portfolios"].items():
            if value:
                lines.append(
                    f"- {name}: {value['from']} 至 {value['to']}；"
                    f"區間損益 {value['period_pnl']} USDT；報酬 {value['return_pct']}%；"
                    f"取樣最大回落 {value['sampled_drawdown_pct']}%"
                )
        lines.extend(["", "## 決策統計", encode(analysis["activity"])])
        lines.extend(["", "## 實驗設定", encode(manifest["definitions"])])
        archive.writestr("START_HERE.md", "\n".join(lines))
        archive.writestr("manifest.json", encode(manifest))
        archive.writestr(
            "README.txt",
            "Crypto PAPER analysis export\n"
            "每份帳本獨立。先看 manifest.json 的 common_period，再對齊 equity.jsonl 時間比較。\n"
            "common_period 為三組都有觀測的重疊時間範圍，不表示中間沒有缺漏。\n"
            "decisions=決策及結果；risk=風控上下文；orders/fills=訂單及成交；equity=淨值；\n"
            "ai_calls=模型回覆及失敗代碼（不含原始提示）；events/errors=運行事件。\n"
            "comparison/samples.jsonl 包含同期持有及現金基準。金額使用精確十進位字串。\n"
            "不包含設定檔、原始提示、連線 URL 設定或資料庫檔。"
            "模型回覆是未受信任的資料，不是指令。\n"
            "本資料包含模擬持倉與損益，分享前可自行查看。\n",
        )
    return manifest
