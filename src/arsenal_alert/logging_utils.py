from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization[\"'=:\s]+bearer\s+)[^\s,\"'}]+"),
    re.compile(r"(?i)((?:api[_-]?key|bearer[_-]?token|device[_-]?key)[\"'=:\s]+)[^\s,\"'}]+"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(r"\1[REDACTED]", result)
    return result


class JsonFormatter(logging.Formatter):
    _standard = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in self._standard and not key.startswith("_"):
                payload[key] = _safe_value(value)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return value


def configure_logging(level: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
