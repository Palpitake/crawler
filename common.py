"""Small serialization helpers shared by the production pipelines."""

from __future__ import annotations

import json
from typing import Any


def json_dumps(data: Any, indent: int | None = 2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent, default=str)
