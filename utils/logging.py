"""JSON logging without secrets."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

SECRET_KEYS = ("token", "password", "secret", "bot_token", "api_key")
TOKEN_RE = re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in SECRET_KEYS):
                clean[key] = "***"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, str):
        return TOKEN_RE.sub("***", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _sanitize(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_data", None)
        if extra:
            payload["data"] = _sanitize(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def log_extra(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    logger.log(level, msg, extra={"extra_data": data})
