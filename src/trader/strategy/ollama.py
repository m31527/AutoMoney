"""Ollama native chat API: local structured proposals, never tool execution."""

import http.client
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import certifi

from trader.exchange.transport import NoRedirect
from trader.strategy.contract import PROPOSAL_SCHEMA
from trader.strategy.provider import ProviderError


@dataclass(frozen=True)
class OllamaProvider:
    model: str
    base_url: str = "http://ollama:11434"
    timeout_seconds: int = 120
    name: str = field(default="ollama", init=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", self.model):
            raise ValueError("AI_MODEL must be an explicit Ollama model tag")
        url = urllib.parse.urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or any(c.isspace() for c in self.base_url)
        ):
            raise ValueError("OLLAMA_BASE_URL must be a server origin without credentials")
        _ = url.port  # Also reject malformed ports before a request is attempted.
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600:
            raise ValueError("OLLAMA_TIMEOUT_SECONDS must be between 1 and 600")

    def redact(self, text: str) -> str:
        return text  # No API key is accepted or sent by the local provider.

    def complete(self, instructions: str, context: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": context},
            ],
            "format": PROPOSAL_SCHEMA,
            "options": {"temperature": 0, "num_predict": 2048, "num_ctx": 32768},
            "keep_alive": "10m",
        }
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ProviderError()
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("done") is not True
                or data.get("done_reason") != "stop"
                or data.get("error")
                or not isinstance(data.get("message"), dict)
            ):
                raise ProviderError()
            message = data["message"]
            if (
                message.get("role") != "assistant"
                or message.get("tool_calls")
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ProviderError()
            result: str = message["content"]
            return result
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
