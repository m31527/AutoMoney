"""Injectable transport. The shipped network transport is deliberately read-only."""

import http.client
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import certifi

from trader.exchange.errors import NetworkError, TradingDisabled


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    @property
    def supports_mutations(self) -> bool: ...

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward a signed request or API key to a redirect destination.
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class ReadOnlyHTTPTransport:
    supports_mutations = False

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        if method != "GET" or body is not None:
            raise TradingDisabled()
        request = urllib.request.Request(url, headers=dict(headers), method=method)
        context = ssl.create_default_context(cafile=certifi.where())
        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return Response(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as error:
            try:
                return Response(error.code, dict(error.headers), error.read())
            finally:
                error.close()
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise NetworkError() from None
