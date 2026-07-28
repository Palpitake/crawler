"""Small, dependency-free helpers shared by the production pipelines."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def json_dumps(data: Any, indent: int | None = 2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent, default=str)


def json_line(data: Any) -> str:
    """Serialize one value for the Python/Node JSONL bridge."""
    return json_dumps(data, indent=None) + "\n"


def safe_int(value: Any, default: int | None = None) -> int | None:
    """Convert a value to ``int`` without leaking parsing failures."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, data: Any) -> None:
    """Persist JSON without exposing a partially written checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json_dumps(data), encoding="utf-8")
    os.replace(temporary, path)
