from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from .config import get_settings

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_REDACTED_KEYS = {
    "earthdata_token",
    "api_key",
    "api_keys",
    "authorization",
    "password",
    "token",
    "cookie",
    "map_key",
    "firms_map_key",
}


def _secret_values() -> list[str]:
    """Configured credentials, so they can be scrubbed from anywhere in a log line."""
    try:
        settings = get_settings()
    except Exception:
        return []
    values = [settings.earthdata_token, getattr(settings, "firms_map_key", None)]
    values.extend(settings.api_keys)
    return [v for v in values if v and len(str(v)) >= 6]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if rid := request_id.get():
            payload["request_id"] = rid
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key.lower() in _REDACTED_KEYS:
                    value = "***redacted***"
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        for secret in _secret_values():
            line = line.replace(str(secret), "***redacted***")
        return line


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "botocore", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_fields(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields}, stacklevel=2)
