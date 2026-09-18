"""Portable read-only research export. No environment, endpoints or raw prompts."""

import io
import json
import zipfile
from contextlib import ExitStack, closing
from datetime import UTC, datetime
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


def write_export(root: Path, destination: BinaryIO | SpooledTemporaryFile[bytes]) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format_version": 1,
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
            "Ollama retains the deterministic SMA edge proxy and risk veto.",
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
            with archive.open(name, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8") as stream:
                for row in rows:
                    item = dict(row)
                    for key in list(item):
                        if key.endswith("_json") and item[key] is not None:
                            item[key.removesuffix("_json")] = json.loads(item.pop(key))
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
                write_rows(name + "/" + category + ".jsonl", db.execute(sql))
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
