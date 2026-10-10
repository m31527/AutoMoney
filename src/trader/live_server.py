"""Private control service. Dashboard proxies exact routes; mutations require bearer auth."""

import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from trader.live_control import operate
from trader.storage.repository import encode

ACTIONS = {"preflight", "prepare", "approve", "reconcile", "stop", "resume"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def reply(self, code: int, value: object) -> None:
        body = encode(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/status":
            self.reply(404, {"error": "NOT_FOUND"})
            return
        self.run_action("status", {})

    def do_POST(self) -> None:
        token = os.getenv("LIVE_CONTROL_TOKEN", "")
        if len(token) < 32 or not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + token
        ):
            self.reply(401, {"error": "AUTH_REQUIRED"})
            return
        action = self.path.removeprefix("/")
        if action not in ACTIONS or self.headers.get("Content-Type") != "application/json":
            self.reply(400, {"error": "INVALID_REQUEST"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "INVALID_REQUEST"})
            return
        self.run_action(action, body)

    def run_action(self, action: str, body: dict[str, object]) -> None:
        try:
            self.reply(200, operate(Path("/live"), Path("/soak"), action, body))
        except BlockingIOError:
            self.reply(409, {"error": "CONTROL_BUSY"})
        except sqlite3.Error:
            self.reply(503, {"error": "CONTROL_STORAGE_UNAVAILABLE"})
        except Exception as error:
            # Never serialize exception repr, transport URLs, keys or exchange raw bodies.
            message = (
                str(error)
                if isinstance(error, ValueError)
                else getattr(error, "code", "CONTROL_FAILED")
            )
            if not message or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in message):
                message = "CONTROL_FAILED"
            self.reply(409, {"error": message})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()


if __name__ == "__main__":
    main()
