import json
import logging
import sys

from trader.storage.repository import utc_now


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Only application-controlled messages; no environment, arguments, or traceback dumps.
        return json.dumps(
            {
                "timestamp": utc_now(),
                "severity": record.levelname,
                "event_type": record.getMessage(),
            }
        )


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("trader")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
