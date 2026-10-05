"""Optional isolated AutoMoney research router. No agents, tools, fallback or retries."""
import asyncio
import copy
import hashlib
from decimal import Decimal
import hmac
import json
import os
import time
import uuid
import sqlite3
from pathlib import Path
from dotenv import dotenv_values

import httpx
from fastapi import APIRouter, HTTPException, Request

from config import config
from automoney_contract import PROPOSAL_SCHEMA, parse_proposal

router = APIRouter(prefix="/automoney")
_lock = asyncio.Lock()
_root = Path(__file__).resolve().parent
for _key, _value in dotenv_values(_root / ".automoney.env").items():
    if _key.startswith("AUTOMONEY_") and _value is not None:
        os.environ.setdefault(_key, _value)



def authorize(request):
    token = os.environ.get("AUTOMONEY_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(503, "BRIDGE_DISABLED")
    if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
        raise HTTPException(401, "UNAUTHORIZED")


@router.get("/health")
async def health(request: Request):
    authorize(request)
    return {"status": "ok", "version": 4, "replay_enabled": os.environ.get("AUTOMONEY_REPLAY_ENABLED") == "true", "cloud_enabled":
            os.environ.get("AUTOMONEY_ALLOW_OPENAI") == "true"}


@router.post("/replay")
@router.post("/analyze")
async def analyze(request: Request):
    authorize(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 160000:
            raise HTTPException(413, "INPUT_TOO_LARGE")
    try:
        data = json.loads(body)
        if set(data) != {"provider", "model", "instructions", "context"}:
            raise ValueError()
        if any(not isinstance(v, str) for v in data.values()):
            raise ValueError()
        if not 1 <= len(data["instructions"]) <= 12000 or not 1 <= len(data["context"]) <= 120000:
            raise ValueError()
        context = json.loads(data["context"])
        compact = context.get("research_version") in ("compact-entry-v3", "compact-entry-v4")
        if compact and len((data["instructions"] + data["context"]).encode()) > 10000:
            raise ValueError()
        symbol = context["snapshot"]["symbol"]
        if symbol not in PROPOSAL_SCHEMA["properties"]["symbol"]["enum"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "INVALID_INPUT") from None
    backend, model = data["provider"], data["model"]
    replay = request.url.path.endswith("/replay")
    if replay and os.environ.get("AUTOMONEY_REPLAY_ENABLED") != "true":
        raise HTTPException(403, "REPLAY_DISABLED")
    # Server pins models too: a caller cannot select an arbitrarily expensive model.
    if replay:
        expected = os.environ.get("AUTOMONEY_REPLAY_OPENAI_MODEL", "") if backend == "openai" else os.environ.get("AUTOMONEY_MODEL", "qwen3.8:27b")
        allowed_backend = backend in ("ollama", "openai")
    else:
        expected = os.environ.get("AUTOMONEY_MODEL", "qwen3.8:27b")
        allowed_backend = backend == os.environ.get("AUTOMONEY_PROVIDER", "ollama")
    if not expected or model != expected or not allowed_backend:
        raise HTTPException(403, "MODEL_NOT_ALLOWED")
    if backend not in ("ollama", "openai"):
        raise HTTPException(403, "PROVIDER_NOT_ALLOWED")
    if backend == "openai" and os.environ.get("AUTOMONEY_REPLAY_ALLOW_OPENAI" if replay else "AUTOMONEY_ALLOW_OPENAI") != "true":
        raise HTTPException(403, "CLOUD_DISABLED")
    if _lock.locked():
        raise HTTPException(429, "BUSY")
    async with _lock:
        now = time.monotonic()
        limit = max(1, min(24 if replay else 96, int(os.environ.get(
            "AUTOMONEY_REPLAY_DAILY_CALL_LIMIT" if replay else "AUTOMONEY_DAILY_CALL_LIMIT",
            "12" if replay else "48"))))
        quota_file = ".automoney-replay-quota.db" if replay else ".automoney-quota.db"
        with sqlite3.connect(_root / quota_file, timeout=2) as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (timestamp REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM calls WHERE timestamp < ?", (time.time() - 86400,))
            if db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= limit:
                raise HTTPException(429, "DAILY_CALL_LIMIT")
            db.execute("INSERT INTO calls VALUES (?)", (time.time(),))
        # Durable rolling 24-hour attempt quota; failed calls also consume a slot.
        schema = copy.deepcopy(PROPOSAL_SCHEMA)
        if compact:
            schema["required"].append("research_check")
            receipt = context["input_receipt"]
            schema["properties"]["research_check"] = {
                "type": "object", "additionalProperties": False,
                "required": list(receipt), "properties": {
                    key: {"type": "boolean" if isinstance(value, bool) else
                          "integer" if isinstance(value, int) else "string"}
                    for key, value in receipt.items()}}
        messages = [{"role": "system", "content": data["instructions"]},
                    {"role": "user", "content": data["context"]}]
        if backend == "ollama":
            url = config.qwen_base_url.rstrip("/") + "/api/chat"
            headers = {}
            payload = {"model": model, "messages": messages, "stream": False, "think": False,
                       "format": schema, "keep_alive": "5m",
                       "options": {"num_ctx": 16384, "num_predict": 2048, "temperature": 0}}
        else:
            if not config.openai_api_key:
                raise HTTPException(503, "PROVIDER_NOT_CONFIGURED")
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": "Bearer " + config.openai_api_key}
            payload = {"model": model, "messages": messages, "max_completion_tokens": 2048,
                       "response_format": {"type": "json_object"}}
        try:
            async with asyncio.timeout(120):
                async with httpx.AsyncClient(timeout=115, follow_redirects=False, trust_env=False) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code != 200:
                            raise HTTPException(502, "UPSTREAM_HTTP_ERROR")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 262144:
                                raise HTTPException(502, "UPSTREAM_TOO_LARGE")
            result = json.loads(raw)
            if backend == "ollama":
                if result.get("done") is not True or result.get("done_reason") != "stop":
                    raise ValueError()
                message = result["message"]
                usage = {"input_tokens": result.get("prompt_eval_count", 0),
                         "output_tokens": result.get("eval_count", 0)}
            else:
                choice = result["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ValueError()
                message = choice["message"]
                usage = result.get("usage", {})
            if message.get("tool_calls") or message.get("refusal"):
                raise ValueError()
            from automoney_contract import _object
            proposal = json.loads(message["content"], object_pairs_hook=_object)
            if compact:
                check = proposal.pop("research_check", None)
                if (not isinstance(check, dict) or set(check) != set(receipt)
                        or any(type(check[k]) is not type(v) or check[k] != v
                               for k, v in receipt.items())):
                    raise HTTPException(502, "INPUT_RECEIPT_MISMATCH")
                if proposal.get("time_horizon_minutes") != 240:
                    raise HTTPException(502, "RESEARCH_HORIZON_MISMATCH")
            parsed = parse_proposal(json.dumps(proposal), symbol)
            if compact:
                cap = context["max_buy_notional_usd"] if parsed.action == "BUY" else context["max_sell_notional_usd"]
                if parsed.action != "HOLD" and parsed.requested_notional_usd > Decimal(cap):
                    raise HTTPException(502, "RESEARCH_BUDGET_EXCEEDED")
            return {"proposal": proposal,
                    "input_audit": {"version": 3, "receipt_verified": compact,
                        "instructions_sha256": hashlib.sha256(data["instructions"].encode()).hexdigest(),
                        "context_sha256": hashlib.sha256(data["context"].encode()).hexdigest(),
                        "input_bytes": len((data["instructions"] + data["context"]).encode()),
                        "requested_num_ctx": 16384 if backend == "ollama" else None}, "provider": backend,
                    "requested_model": model, "model": result.get("model", model),
                    "usage": usage, "request_id": str(uuid.uuid4()),
                    "latency_seconds": round(time.monotonic() - now, 3)}
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "UPSTREAM_TIMEOUT") from None
        except httpx.HTTPError:
            raise HTTPException(502, "UPSTREAM_NETWORK_ERROR") from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise HTTPException(502, "INVALID_PROPOSAL") from None
