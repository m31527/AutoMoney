"""Authenticated, bounded OpenTeddy shadow research client; never executes orders."""

import hashlib
import http.client
import json
import os
import urllib.error
import urllib.request
from typing import Any

from trader.exchange.transport import NoRedirect
from trader.strategy.ollama import OllamaProvider
from trader.strategy.provider import ProviderError


class OpenTeddyProvider:
    name = "openteddy"

    def __init__(
        self, *, backend: str | None = None, model: str | None = None, replay: bool = False
    ) -> None:
        self.model = model or os.environ["OPENTEDDY_MODEL"]
        self.endpoint = "/automoney/replay" if replay else "/automoney/analyze"
        self.backend = backend or os.environ.get("OPENTEDDY_PROVIDER", "ollama")
        self.base_url = os.environ["OPENTEDDY_URL"].rstrip("/")
        self.token = os.environ["OPENTEDDY_TOKEN"]
        OllamaProvider(self.model, self.base_url)  # Validate origin and model before sending token.
        if self.backend not in ("ollama", "openai") or len(self.token) < 32:
            raise ValueError(
                "OpenTeddy requires an explicit provider and token of at least 32 chars"
            )
        self.last_metadata: dict[str, Any] = {}

    def redact(self, text: str) -> str:
        return text.replace(self.token, "[REDACTED]")

    def complete(self, instructions: str, context: str) -> str:
        self.last_metadata = {"provider": self.backend, "requested_model": self.model}
        payload = {
            "provider": self.backend,
            "model": self.model,
            "instructions": instructions,
            "context": context,
        }
        request = urllib.request.Request(
            self.base_url + self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=125) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            data = json.loads(raw)
            if (
                data["provider"] != self.backend
                or data["requested_model"] != self.model
                or not isinstance(data["proposal"], dict)
            ):
                raise ValueError()
            self.last_metadata.update(
                {k: data[k] for k in ("model", "request_id", "usage", "latency_seconds")}
            )
            if json.loads(context).get("research_version") in (
                "compact-entry-v3",
                "compact-entry-v4",
            ):
                audit = data.get("input_audit", {})
                if (
                    audit.get("receipt_verified") is not True
                    or audit.get("instructions_sha256")
                    != hashlib.sha256(instructions.encode()).hexdigest()
                    or audit.get("context_sha256") != hashlib.sha256(context.encode()).hexdigest()
                ):
                    self.last_metadata["error_code"] = "INPUT_AUDIT_FAILED"
                    raise ProviderError()
                self.last_metadata["input_audit"] = audit
            return json.dumps(data["proposal"])
        except urllib.error.HTTPError as error:
            self.last_metadata["error_code"] = f"HTTP_{error.code}"
            try:
                detail = json.loads(error.read(4096)).get("detail")
                if detail in {
                    "UPSTREAM_HTTP_ERROR",
                    "UPSTREAM_TIMEOUT",
                    "UPSTREAM_NETWORK_ERROR",
                    "INVALID_PROPOSAL",
                    "INPUT_RECEIPT_MISMATCH",
                    "RESEARCH_HORIZON_MISMATCH",
                    "RESEARCH_BUDGET_EXCEEDED",
                    "DAILY_CALL_LIMIT",
                    "BUSY",
                    "MODEL_NOT_ALLOWED",
                    "CLOUD_DISABLED",
                    "REPLAY_DISABLED",
                    "PROVIDER_NOT_CONFIGURED",
                    "BRIDGE_DISABLED",
                    "UNAUTHORIZED",
                }:
                    self.last_metadata["error_code"] = detail
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            finally:
                error.close()
            raise ProviderError() from None
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            self.last_metadata["error_code"] = "TRANSPORT_OR_INVALID_RESPONSE"
            raise ProviderError() from None
