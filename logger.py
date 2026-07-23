"""Unified logging for crawler-agent.

Console/file text format is intentionally compact and stable::

    timestamp | LEVEL | component | [event] task_id=... status=... key=value

A machine-readable ``crawler.jsonl`` is written beside ``crawler.log``.  All
structured events should go through :func:`log_event`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from runtime_facts import sanitize_url

_loggers: dict[str, logging.Logger] = {}
_initialized = False
_task_id: ContextVar[str] = ContextVar("crawler_task_id", default="-")

SENSITIVE_PATTERNS = (
    (r"api[_-]?key[=:]\s*[A-Za-z0-9._\-+/=]+", "api_key=***"),
    (r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{10,}", "sk-***"),
    (r"Bearer\s+[A-Za-z0-9._\-+/=]+", "Bearer ***"),
    (r"(password|passwd|pwd|secret|token)[=:]\s*[^\s,;]+", r"\1=***"),
)

FIELD_ORDER = (
    "task_id", "status", "agent", "phase", "runtime", "action", "tool",
    "invocation", "repair_attempt", "model_attempt", "step", "limit",
    "duration_ms", "items", "pages", "confidence", "runtime_status",
    "artifact_status", "review_status", "terminal_reason", "error_type",
    "reason", "path", "message",
)

STATUS_VALUES = {
    "started", "running", "success", "failed", "incomplete", "degraded",
    "blocked", "skipped", "resumed", "saved", "loaded", "required",
    "advisory", "ready", "received", "accepted", "rejected",
    "terminated", "success_with_warnings", "selected", "retained",
    "confirmed", "provisional", "verified", "stalled",
}


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return str(text)
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def set_log_context(*, task_id: Optional[str] = None) -> None:
    if task_id is not None:
        _task_id.set(str(task_id) or "-")


def get_log_context() -> Dict[str, str]:
    return {"task_id": _task_id.get()}


def _safe_value(value: Any, max_chars: int = 800) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, dict):
        return {str(k): _safe_value(v, max_chars=max(80, max_chars // 2)) for k, v in list(value.items())[:80]}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(v, max_chars=max(80, max_chars // 2)) for v in list(value)[:100]]
    value = redact_secrets(str(value))
    if "http://" in value or "https://" in value:
        value = re.sub(r"https?://[^\s\"'<>]+", lambda m: sanitize_url(m.group(0)), value)
    value = value.replace("\r", "\\r").replace("\n", "\\n")
    return value if len(value) <= max_chars else value[: max_chars - 3] + "..."


def _render_value(value: Any) -> str:
    value = _safe_value(value)
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    text = str(value)
    if not text or any(ch.isspace() for ch in text) or any(ch in text for ch in '="'):
        return json.dumps(text, ensure_ascii=False)
    return text


class JsonEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "schema_version": 1,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "event": getattr(record, "event_name", "log.message"),
        }
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        else:
            payload["message"] = redact_secrets(record.getMessage())
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def setup_logging(workspace: str = "runtime", level: str = "INFO", log_file: str = "crawler.log") -> None:
    global _initialized
    if _initialized:
        return

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, level.upper(), logging.INFO)
    text_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(text_fmt)

    file_handler = logging.FileHandler(root / log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(text_fmt)

    json_handler = logging.FileHandler(root / "crawler.jsonl", encoding="utf-8")
    json_handler.setLevel(logging.DEBUG)
    json_handler.setFormatter(JsonEventFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(json_handler)

    for noisy in ("httpx", "httpcore", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _initialized = True


def get_logger(name: str = "crawler") -> logging.Logger:
    if name not in _loggers:
        _loggers[name] = logging.getLogger(name)
    return _loggers[name]


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: str = "INFO",
    status: Optional[str] = None,
    message: Optional[str] = None,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit one normalized structured event.

    ``event`` uses dotted lower-case names (for example ``agent.tool`` or
    ``checkpoint.save``).  ``status`` describes the event outcome and must not
    be used as an error category; use ``error_type`` for that.
    """
    normalized: Dict[str, Any] = {"task_id": fields.pop("task_id", None) or _task_id.get()}
    if status:
        normalized["status"] = status if status in STATUS_VALUES else str(status).lower()
    normalized.update({k: _safe_value(v) for k, v in fields.items() if v is not None})
    if message:
        normalized["message"] = _safe_value(message, 1200)

    ordered: Dict[str, Any] = {}
    for key in FIELD_ORDER:
        if key in normalized:
            ordered[key] = normalized[key]
    for key in sorted(normalized):
        if key not in ordered:
            ordered[key] = normalized[key]

    body = " ".join(f"{key}={_render_value(value)}" for key, value in ordered.items())
    text = f"[{event}]" + (f" {body}" if body else "")
    method = getattr(logger, level.lower(), logger.info)
    method(text, extra={"event_name": event, "event_fields": ordered}, exc_info=exc_info)
