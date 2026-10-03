"""Optional isolated AutoMoney research router. No agents, tools, fallback or retries."""
import asyncio
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
    return {"status": "ok", "version": 1, "cloud_enabled":
            os.environ.get("AUTOMONEY_ALLOW_OPENAI") == "true"}


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
        symbol = context["snapshot"]["symbol"]
        if symbol not in PROPOSAL_SCHEMA["properties"]["symbol"]["enum"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "INVALID_INPUT") from None
    backend, model = data["provider"], data["model"]
    # Server pins models too: a caller cannot select an arbitrarily expensive model.
    expected = os.environ.get("AUTOMONEY_MODEL", "qwen3.8:27b")
    if model != expected or backend != os.environ.get("AUTOMONEY_PROVIDER", "ollama"):
        raise HTTPException(403, "MODEL_NOT_ALLOWED")
    if backend not in ("ollama", "openai"):
        raise HTTPException(403, "PROVIDER_NOT_ALLOWED")
    if backend == "openai" and os.environ.get("AUTOMONEY_ALLOW_OPENAI") != "true":
        raise HTTPException(403, "CLOUD_DISABLED")
    if _lock.locked():
        raise HTTPException(429, "BUSY")
    async with _lock:
        now = time.monotonic()
        limit = max(1, min(96, int(os.environ.get("AUTOMONEY_DAILY_CALL_LIMIT", "48"))))
        with sqlite3.connect(_root / ".automoney-quota.db", timeout=2) as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (timestamp REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM calls WHERE timestamp < ?", (time.time() - 86400,))
            if db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= limit:
                raise HTTPException(429, "DAILY_CALL_LIMIT")
            db.execute("INSERT INTO calls VALUES (?)", (time.time(),))
        # Durable rolling 24-hour attempt quota; failed calls also consume a slot.
        messages = [{"role": "system", "content": data["instructions"]},
                    {"role": "user", "content": data["context"]}]
        if backend == "ollama":
            url = config.qwen_base_url.rstrip("/") + "/api/chat"
            headers = {}
            payload = {"model": model, "messages": messages, "stream": False, "think": False,
                       "format": PROPOSAL_SCHEMA, "keep_alive": "5m",
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
            parse_proposal(message["content"], symbol)
            return {"proposal": json.loads(message["content"]), "provider": backend,
                    "requested_model": model, "model": result.get("model", model),
                    "usage": usage, "request_id": str(uuid.uuid4()),
                    "latency_seconds": round(time.monotonic() - now, 3)}
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "UPSTREAM_TIMEOUT") from None
        except httpx.HTTPError:
            raise HTTPException(502, "UPSTREAM_NETWORK_ERROR") from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise HTTPException(502, "INVALID_PROPOSAL") from None
