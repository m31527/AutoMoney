"""Optional OpenAI Responses provider. Fixed endpoint, no tools, no automatic retries."""

import http.client
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import certifi

from trader.exchange.transport import NoRedirect
from trader.strategy.contract import PROPOSAL_SCHEMA


class ProviderError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("AI_PROVIDER_UNAVAILABLE_OR_REFUSED")


class AIProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str: ...
    def complete(self, instructions: str, context: str) -> str: ...
    def redact(self, text: str) -> str: ...


@dataclass(frozen=True)
class OpenAIProvider:
    model: str
    api_key: str = field(repr=False)
    name: str = field(default="openai", init=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.model):
            raise ValueError("AI_MODEL must be explicitly configured")
        if not self.api_key or not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
            raise ValueError("AI_API_KEY must be configured")

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]")

    def complete(self, instructions: str, context: str) -> str:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": context,
            "store": False,
            "max_output_tokens": 4096,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "trade_proposal",
                    "strict": True,
                    "schema": PROPOSAL_SCHEMA,
                }
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ProviderError()
            return self._extract(json.loads(raw))
        except urllib.error.HTTPError as error:
            error.close()
            raise ProviderError() from None
        except (
            OSError,
            urllib.error.URLError,
            http.client.HTTPException,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
        ):
            raise ProviderError() from None

    @staticmethod
    def _extract(data: Any) -> str:
        if not isinstance(data, dict) or data.get("status") != "completed":
            raise ProviderError()
        if not isinstance(data.get("output"), list):
            raise ProviderError()
        texts = []
        for item in data["output"]:
            if not isinstance(item, dict):
                raise ProviderError()
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or not isinstance(item.get("content"), list)
            ):
                raise ProviderError()
            for block in item["content"]:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "output_text"
                    or not isinstance(block.get("text"), str)
                ):
                    raise ProviderError()
                texts.append(block["text"])
        if len(texts) != 1:
            raise ProviderError()
        result: str = texts[0]
        return result


def load_provider(environ: Mapping[str, str] | None = None) -> AIProvider:
    env = os.environ if environ is None else environ
    if env.get("AI_PROVIDER") == "ollama":
        from trader.strategy.ollama import OllamaProvider

        return OllamaProvider(
            env.get("AI_MODEL", ""),
            env.get("OLLAMA_BASE_URL", "http://ollama:11434"),
            int(env.get("OLLAMA_TIMEOUT_SECONDS", "120")),
        )
    if env.get("AI_PROVIDER") != "openai":
        raise ValueError("Set AI_PROVIDER=openai or ollama explicitly to use an AI strategy")
    return OpenAIProvider(env.get("AI_MODEL", ""), env.get("AI_API_KEY", ""))
