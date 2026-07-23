"""Utilities for safely persisting and restoring Pi Agent transcripts.

Tool calls and their tool results are treated as one atomic group.  This avoids
restoring a transcript that begins with an orphan tool result or ends with an
assistant tool call whose result was never persisted, both of which produce
provider-side 400 errors.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Set


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _role(message: Dict[str, Any]) -> str:
    return str(message.get("role") or "")


def _assistant_tool_call_ids(message: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id") is not None:
            ids.add(str(call.get("id")))
    for block in message.get("content") or []:
        if not isinstance(block, dict) or str(block.get("type")) not in {"toolCall", "tool_call"}:
            continue
        value = block.get("id") or block.get("toolCallId") or block.get("tool_call_id")
        if value is not None:
            ids.add(str(value))
    return ids


def _assistant_has_tool_calls(message: Dict[str, Any]) -> bool:
    if message.get("tool_calls"):
        return True
    return any(
        isinstance(block, dict) and str(block.get("type")) in {"toolCall", "tool_call"}
        for block in (message.get("content") or [])
    )


def _tool_result_id(message: Dict[str, Any]) -> str:
    for key in ("toolCallId", "tool_call_id", "tool_use_id", "id"):
        value = message.get(key)
        if value is not None:
            return str(value)
    return ""


def _is_tool_result(message: Dict[str, Any]) -> bool:
    return _role(message) in {"tool", "toolResult", "tool_result"}


def sanitize_agent_transcript(value: Any, *, max_messages: int = 160) -> List[Dict[str, Any]]:
    """Return a provider-valid transcript with atomic tool-call groups.

    Orphan tool results and incomplete assistant tool-call groups are dropped.
    The tail limit is applied to whole groups, never to individual messages.
    """
    if not isinstance(value, list):
        return []
    messages: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            messages.append(_clone(item))
        except Exception:
            continue

    groups: List[List[Dict[str, Any]]] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if _is_tool_result(message):
            # A result without the preceding assistant tool call is unusable.
            i += 1
            continue
        if _role(message) == "assistant" and _assistant_has_tool_calls(message):
            group = [message]
            expected_ids = _assistant_tool_call_ids(message)
            observed_ids: Set[str] = set()
            j = i + 1
            while j < len(messages) and _is_tool_result(messages[j]):
                result = messages[j]
                result_id = _tool_result_id(result)
                # If IDs exist, reject results for a different tool call.
                if expected_ids and result_id and result_id not in expected_ids:
                    break
                group.append(result)
                if result_id:
                    observed_ids.add(result_id)
                j += 1
            complete = len(group) > 1
            if expected_ids:
                complete = expected_ids.issubset(observed_ids)
            if complete:
                groups.append(group)
            i = j
            continue
        groups.append([message])
        i += 1

    selected: List[List[Dict[str, Any]]] = []
    count = 0
    for group in reversed(groups):
        if selected and count + len(group) > max_messages:
            break
        if not selected and len(group) > max_messages:
            continue
        selected.append(group)
        count += len(group)
    selected.reverse()
    return [message for group in selected for message in group]
