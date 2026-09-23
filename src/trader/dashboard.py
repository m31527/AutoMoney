"""Local, read-only dashboard. Reads SQLite without migrations or trading imports."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from trader.storage.repository import encode

SOURCES = {
    "sma5m": "experiment-v2/sma5m.db",
    "trend1h": "experiment-v2/trend1h.db",
    "legacy": "paper.db",
    "errors": "experiment-v2/comparison.db",
    "events": "paper.db",
    "ollama": "ollama/paper.db",
    "ai-events": "ollama/paper.db",
    "ai-calls": "ollama/paper.db",
}
WEB = Path(__file__).parent / "web"


def reader(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def ai_summary(root: Path) -> dict[str, Any]:
    path = root / "ollama/paper.db"
    if not path.exists():
        return {"available": False}
    with closing(reader(path)) as db:
        db.execute("BEGIN")
        definition = db.execute("SELECT definition FROM local_ai_config WHERE id=1").fetchone()
        account = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if not definition or not account:
            return {"available": False}
        latest = db.execute(
            "SELECT timestamp,result_json FROM paper_cycles "
            "ORDER BY timestamp DESC,rowid DESC LIMIT 1"
        ).fetchone()
        counts = db.execute("SELECT COUNT(*),SUM(error IS NOT NULL) FROM ai_calls").fetchone()
        trades = db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        portfolio = json.loads(latest[1])["portfolio"] if latest else {}
        mode = db.execute(
            "SELECT payload_json FROM system_events WHERE event_type='AI_PREFILTER_MODE' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prefilter_enabled = json.loads(mode[0])["enabled"] if mode else False
        skipped = db.execute(
            "SELECT COUNT(*) FROM paper_cycles WHERE "
            "json_extract(result_json,'$.strategy_reason')='AI_PREFILTER_COST_BLOCKED'"
        ).fetchone()[0]
        diagnostic = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_call_diagnostics'").fetchone():
            row = db.execute(
                "SELECT diagnostics_json FROM ai_call_diagnostics ORDER BY call_id DESC LIMIT 1"
            ).fetchone()
            diagnostic = json.loads(row[0]) if row else None
        return {
            "available": True,
            "prefilter_enabled": prefilter_enabled,
            "prefilter_skipped": skipped,
            "model": json.loads(definition[0])["model"],
            "started_at": account["initialized_at"],
            "updated_at": latest[0] if latest else None,
            "calls": counts[0],
            "failures": counts[1] or 0,
            "trades": trades,
            "cash": account["cash"],
            "fees": account["fees"],
            "equity": portfolio.get("equity"),
            "net_pnl": portfolio.get("net_pnl"),
            "policy_version": diagnostic["policy_version"] if diagnostic else None,
            "last_inference_seconds": diagnostic["inference_seconds"] if diagnostic else None,
        }


def holdings(root: Path) -> list[dict[str, Any]]:
    result = []
    for name in ("sma5m", "trend1h", "ollama"):
        path = root / SOURCES[name]
        if not path.exists():
            continue
        with closing(reader(path)) as db:
            row = db.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC,id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            continue
        positions = json.loads(row["positions_json"])
        for position in positions:
            position["market_value"] = str(
                Decimal(position["quantity"]) * Decimal(position["market_price"])
            )
        result.append(
            {
                "strategy": name,
                "timestamp": row["timestamp"],
                "cash": row["cash"],
                "equity": row["equity"],
                "positions": positions,
            }
        )
    return result


def summary(root: Path) -> dict[str, Any]:
    ai = ai_summary(root)
    path = root / "experiment-v2/comparison.db"
    if not path.exists():
        return {"ready": False, "ai": ai, "holdings": holdings(root), "markets": {}}
    with closing(reader(path)) as db:
        db.execute("BEGIN")
        meta = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM experiment_meta")}
        definition = meta["definition"]
        capital = Decimal(definition["risk"]["starting_capital_usd"])
        rows = db.execute(
            "SELECT timestamp,payload FROM experiment_samples ORDER BY bucket"
        ).fetchall()
        error_count = db.execute("SELECT COUNT(*) FROM experiment_errors").fetchone()[0]
        last_error = db.execute(
            "SELECT timestamp,code FROM experiment_errors ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not rows:
        return {
            "ready": False,
            "error_count": error_count,
            "ai": ai,
            "holdings": holdings(root),
            "markets": {},
        }
    arms = ("sma5m", "trend1h", "hold", "cash")
    peaks = dict.fromkeys(arms, capital)
    drawdowns = dict.fromkeys(arms, Decimal(0))
    points = []
    step = max(1, (len(rows) + 598) // 599)
    latest: dict[str, Any] = {}
    for index, row in enumerate(rows):
        latest = json.loads(row[1])
        values = {arm: Decimal(latest["portfolios"][arm]["equity"]) for arm in arms}
        for arm, value in values.items():
            if not value.is_finite():
                raise ValueError("Invalid equity")
            peaks[arm] = max(peaks[arm], value)
            drawdowns[arm] = max(drawdowns[arm], (peaks[arm] - value) / peaks[arm] * 100)
        if index % step == 0 or index == len(rows) - 1:
            points.append(
                {"timestamp": row[0], "values": {k: str(v - capital) for k, v in values.items()}}
            )
    portfolios = latest["portfolios"]
    for arm in arms:
        equity = Decimal(portfolios[arm]["equity"])
        portfolios[arm].update(
            {
                "net_pnl": str(equity - capital),
                "return_pct": str((equity / capital - 1) * 100),
                "drawdown_pct": str(drawdowns[arm]),
            }
        )
    control = root / "paper.db"
    killed = None
    if control.exists():
        with closing(reader(control)) as db:
            state = db.execute("SELECT killed FROM system_state WHERE id=1").fetchone()
            killed = bool(state[0]) if state else None
    stamp = datetime.fromisoformat(rows[-1][0])
    return {
        "ready": True,
        "markets": latest.get("markets", {}),
        "holdings": holdings(root),
        "ai": ai,
        "capital": str(capital),
        "started_at": meta["baseline"]["timestamp"],
        "updated_at": rows[-1][0],
        "age_seconds": max(0, (datetime.now(UTC) - stamp).total_seconds()),
        "samples": len(rows),
        "error_count": error_count,
        "killed": killed,
        "last_error": dict(last_error) if last_error else None,
        "portfolios": portfolios,
        "points": points,
        "risk": definition["risk"],
    }


def records(root: Path, source: str, status: str, page: int) -> dict[str, Any]:
    if (
        source not in SOURCES
        or status not in ("ALL", "FILLED", "REJECTED", "HOLD")
        or not 1 <= page <= 1000000
    ):
        raise ValueError("Invalid filter")
    path = root / SOURCES[source]
    if not path.exists():
        return {"rows": [], "total": 0, "page": page, "pages": 1}
    size = 30
    with closing(reader(path)) as db:
        db.execute("BEGIN")
        if source == "ai-calls":
            total = db.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0]
            selected = db.execute(
                "SELECT id,timestamp,model,error FROM ai_calls ORDER BY id DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            )
            output = [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "status": "ERROR" if r["error"] else "INFO",
                    "reason": r["error"] or "模型已回覆，成交與否請查看 Ollama AI 紀錄",
                    "model": r["model"],
                }
                for r in selected
            ]
        elif source in ("errors", "events", "ai-events"):
            table = "experiment_errors" if source == "errors" else "system_events"
            total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            selected = db.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            )
            output = [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "status": "ERROR" if source == "errors" else r["severity"],
                    "reason": r["code"] if source == "errors" else r["event_type"],
                }
                for r in selected
            ]
        else:
            where = "" if status == "ALL" else " WHERE json_extract(result_json,'$.status')=?"
            args: tuple[Any, ...] = () if status == "ALL" else (status,)
            total = db.execute("SELECT COUNT(*) FROM paper_cycles" + where, args).fetchone()[0]
            selected = db.execute(
                "SELECT cycle_id,timestamp,result_json FROM paper_cycles"
                + where
                + " ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?",
                (*args, size, (page - 1) * size),
            )
            output = []
            for row in selected:
                result = json.loads(row["result_json"])
                order = db.execute(
                    "SELECT symbol,executed_qty,average_fill_price,fee FROM orders WHERE id=?",
                    (result.get("order_id"),),
                ).fetchone()
                symbol = result.get("symbol")
                if not symbol:
                    assessment = db.execute(
                        "SELECT proposal_json FROM risk_assessments WHERE id=?",
                        (result["assessment_id"],),
                    ).fetchone()
                    symbol = json.loads(assessment[0])["symbol"] if assessment else "—"
                output.append(
                    {
                        "id": row["cycle_id"],
                        "timestamp": row["timestamp"],
                        "symbol": symbol,
                        "action": result["action"],
                        "status": result["status"],
                        "reasons": result["reasons"] if result["status"] == "REJECTED" else [],
                        "signal": result.get("signal_proxy_bps"),
                        "cost": result.get("estimated_round_trip_cost_bps"),
                        "order": dict(order) if order else None,
                        "model_reason": result.get("strategy_reason")
                        if source == "ollama"
                        else None,
                    }
                )
    return {
        "rows": output,
        "total": total,
        "page": page,
        "pages": max(1, (total + size - 1) // size),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, root: Path, **kwargs: Any) -> None:
        self.root = root
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not print arbitrary URL/query strings into logs.
        pass

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        try:
            if url.path in assets:
                name, mime = assets[url.path]
                self.send(200, (WEB / name).read_bytes(), mime)
                return
            if url.path == "/api/export":
                from trader.export_results import parse_bound, write_export

                with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as output:
                    query = parse_qs(url.query, max_num_fields=8)
                    write_export(
                        self.root,
                        output,
                        start=parse_bound(query.get("start", [None])[0]),
                        end=parse_bound(query.get("end", [None])[0]),
                    )
                    size = output.tell()
                    output.seek(0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(size))
                    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="paper-analysis-{stamp}.zip"'
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(output, self.wfile)
                return
            if url.path == "/api/summary":
                result = summary(self.root)
            elif url.path == "/api/records":
                query = parse_qs(url.query, max_num_fields=8)
                result = records(
                    self.root,
                    query.get("source", ["sma5m"])[0],
                    query.get("status", ["ALL"])[0],
                    int(query.get("page", ["1"])[0]),
                )
            else:
                self.send(404, b"Not found", "text/plain")
                return
            self.send(200, encode(result).encode(), "application/json; charset=utf-8")
        except (ValueError, OverflowError):
            self.send(400, b'{"error":"Invalid request or data"}', "application/json")
        except (OSError, sqlite3.Error, KeyError, TypeError, ArithmeticError):
            self.send(503, b'{"error":"Data temporarily unavailable"}', "application/json")


def serve(root: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), partial(DashboardHandler, root=root))
    print(f"PAPER dashboard listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
