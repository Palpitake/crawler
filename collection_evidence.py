"""Pure, site-agnostic evidence rules for browser-owned collections.

This module intentionally has no LangChain or browser dependency so the same
acceptance contract can be exercised repeatedly in deterministic tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import parse_qs, urlparse


VOLATILE_QUERY_KEYS = {
    "w_rid", "wts", "x-bogus", "a_bogus", "_signature", "signature",
    "sign", "timestamp", "ts", "t", "_", "callback", "nonce",
}

ROW_ID_KEYS = (
    "cid", "comment_id", "reply_id", "rpid", "id", "item_id", "uuid",
)


def request_url(request: Dict[str, Any]) -> str:
    url = str(request.get("url") or "")
    if url:
        return url
    description = str(request.get("description") or "")
    match = re.search(r"https?://[^\s\]\)]+", description)
    return match.group(0).rstrip(".,;") if match else ""


def request_family(url: Optional[str]) -> Dict[str, str]:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return {}
    host = str(parsed.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", str(parsed.path or "/"))
    return {"host": host, "path": path or "/"} if host else {}


def matches_request_family(request: Dict[str, Any], family: Dict[str, str]) -> bool:
    return not family or request_family(request_url(request)) == family


def select_request_window(
    requests: Sequence[Dict[str, Any]],
    *,
    max_items: int,
    after_index: int = 0,
) -> Dict[str, Any]:
    """Select the newest delta without losing its pre-filter high-water mark."""
    items = [dict(request) for request in requests if isinstance(request, dict)]
    highest_index = max(
        (int(request.get("index", 0) or 0) for request in items),
        default=0,
    )
    matched_before_index = len(items)
    if after_index > 0:
        items = [
            request for request in items
            if int(request.get("index", 0) or 0) > after_index
        ]
    limit = max(1, int(max_items))
    selected = items[-limit:] if after_index > 0 else items[:limit]
    return {
        "requests": selected,
        "highest_index": highest_index,
        "matched_before_index": matched_before_index,
        "matched_after_index": len(items),
    }


def freeze_request_evidence(requests: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detach accepted evidence from mutable network-log snapshots."""
    return [deepcopy(request) for request in requests if isinstance(request, dict)]


def request_state_fingerprint(request: Dict[str, Any]) -> str:
    """Ignore regenerated signatures while preserving cursor/filter state."""
    url = request_url(request)
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    state = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if str(key).lower() in VOLATILE_QUERY_KEYS:
            continue
        state.append((str(key), tuple(str(value) for value in values)))
    return json.dumps(
        [request_family(url), sorted(state)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_query_value(request: Dict[str, Any], key: str) -> str:
    if not str(key or "").strip():
        return ""
    try:
        values = parse_qs(
            urlparse(request_url(request)).query,
            keep_blank_values=True,
        ).get(str(key), [""])
    except Exception:
        return ""
    return str(values[0]) if values else ""


def json_path_get(value: Any, path: str) -> Any:
    current = value
    normalized = str(path or "").strip().lstrip("$").lstrip(".")
    if not normalized:
        return current
    parts = [part for part in re.split(r"\.|\[([^\]]+)\]", normalized) if part not in {None, ""}]
    for part in parts:
        token = str(part).strip("'\"")
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def row_fingerprints(
    payload: Any,
    data_path: str,
    field_mapping: Optional[Dict[str, str]] = None,
) -> Set[str]:
    """Return stable identities without assuming a website-specific schema."""
    rows = json_path_get(payload, data_path)
    if not isinstance(rows, list):
        return set()
    mapping = field_mapping if isinstance(field_mapping, dict) else {}
    fingerprints: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = ""
        for key in ROW_ID_KEYS:
            value = row.get(key)
            if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
                identity = "id:" + json.dumps([key, str(value)], ensure_ascii=False, separators=(",", ":"))
                break
        if not identity and mapping:
            projected = {
                str(field): json_path_get(row, str(path))
                for field, path in mapping.items()
            }
            if projected:
                digest = hashlib.sha256(
                    json.dumps(
                        projected,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                        separators=(",", ":"),
                    ).encode("utf-8", errors="replace")
                ).hexdigest()
                identity = f"content:{digest}"
        if identity:
            fingerprints.add(identity)
    return fingerprints


def body_row_fingerprints(
    body: str,
    data_path: str,
    field_mapping: Optional[Dict[str, str]] = None,
) -> Set[str]:
    try:
        payload = json.loads(str(body or ""))
    except Exception:
        return set()
    return row_fingerprints(payload, data_path, field_mapping)


def evaluate_transaction_evidence(
    *,
    baseline_states: Iterable[str],
    baseline_rows: Iterable[str],
    requests: Sequence[Dict[str, Any]],
    data_path: str,
    field_mapping: Optional[Dict[str, str]] = None,
    cursor_param: Optional[str] = None,
    baseline_cursor_values: Iterable[str] = (),
) -> Dict[str, Any]:
    """Accept only a new request state that yields at least one new row."""
    baseline_state_set = {str(value) for value in baseline_states}
    baseline_row_set = {str(value) for value in baseline_rows}
    states: Set[str] = set()
    cursor_values: Set[str] = set()
    rows: Set[str] = set()
    bodies_read = 0
    for request in requests:
        if not isinstance(request, dict):
            continue
        states.add(request_state_fingerprint(request))
        if cursor_param:
            cursor_values.add(request_query_value(request, str(cursor_param)))
        body = str(request.get("response_body") or "")
        if body:
            bodies_read += 1
            rows.update(body_row_fingerprints(body, data_path, field_mapping))
    new_states = states - baseline_state_set
    new_cursor_values = cursor_values - {
        str(value) for value in baseline_cursor_values
    }
    new_rows = rows - baseline_row_set
    if not requests:
        reason = "no_same_family_request"
    elif not new_states:
        reason = "no_new_request_state"
    elif cursor_param and not new_cursor_values:
        reason = "no_new_cursor_state"
    elif not bodies_read:
        reason = "response_body_not_captured"
    elif not rows:
        reason = "collection_rows_not_found"
    elif not new_rows:
        reason = "no_new_unique_rows"
    else:
        reason = "new_state_with_new_unique_rows"
    return {
        "accepted": reason == "new_state_with_new_unique_rows",
        "reason": reason,
        "request_count": len([item for item in requests if isinstance(item, dict)]),
        "body_count": bodies_read,
        "observed_state_count": len(states),
        "new_request_state_count": len(new_states),
        "new_cursor_state_count": len(new_cursor_values),
        "observed_row_count": len(rows),
        "new_unique_row_count": len(new_rows),
        "new_states": sorted(new_states),
        "new_cursor_values": sorted(new_cursor_values),
        "new_rows": sorted(new_rows),
    }


