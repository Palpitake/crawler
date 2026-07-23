"""Minimal local tool decorator used by deterministic Python tool adapters.

It provides the small ``.invoke(dict)`` interface required by the Browser and
artifact-validation pipelines without an external tool-wrapper framework.
"""

from __future__ import annotations

from functools import update_wrapper
from typing import Any, Callable, Dict, Optional


class LocalTool:
    def __init__(
        self,
        func: Callable[..., Any],
        *,
        name: Optional[str] = None,
        args_schema: Any = None,
    ) -> None:
        self.func = func
        self.name = name or func.__name__
        self.args_schema = args_schema
        self.description = (func.__doc__ or "").strip()
        update_wrapper(self, func)

    def invoke(self, arguments: Optional[Dict[str, Any]] = None) -> Any:
        payload = arguments or {}
        if not isinstance(payload, dict):
            raise TypeError(f"{self.name}.invoke expects a dict")
        return self.func(**payload)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


def tool(
    name_or_func: Any = None,
    *,
    args_schema: Any = None,
) -> Any:
    """Decorate a function as a local invokable tool.

    Supports ``@tool``, ``@tool("name")``, and
    ``@tool("name", args_schema=Schema)``.
    """
    if callable(name_or_func):
        return LocalTool(name_or_func, args_schema=args_schema)

    explicit_name = str(name_or_func) if name_or_func else None

    def decorate(func: Callable[..., Any]) -> LocalTool:
        return LocalTool(func, name=explicit_name, args_schema=args_schema)

    return decorate
