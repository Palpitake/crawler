"""Browser analysis driven by pi-agent-core with checkpoint recovery.

Python exposes allow-listed Playwright tools, persists objective evidence, and
enforces budgets. The Agent decides how to inspect, interact, compare, and
submit the parser result.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlparse

from runtime_facts import endpoint_provenance, normalize_auth_facts, page_partition, sanitize_url

from logger import get_logger, log_event
from pi_browser_runtime import PiRuntimeUnavailable, run_pi_browser_agent
from transcript_utils import sanitize_agent_transcript


logger = get_logger("agent.browser")
BROWSER_AGENT_BUILD = "2026.07.23-browser-native-loop-v13.6-mysql-rag"
_LAST_CHECKPOINT_LOG_SIGNATURE: dict[str, str] = {}


BROWSER_AGENT_SYSTEM_PROMPT = """你是通过 pi-agent-core 运行的完整 Browser Agent。网页解析从打开页面到最终提交都由你负责。

你没有任何预先认定的 endpoint、selector、分页结构或网站适配器。Python 只执行工具并返回原始观察，不替你判断结果。

工作原则（业务正确性由你自行判断；Python 不会用固定证据模板替你判定）：
1. 若任务上下文标记 browser_checkpoint.page_reused=true，说明上一轮页面仍存活：禁止重新调用 browser_open，直接复用已有页面和网络证据继续补缺；只有 page_reused=false 时才首先调用 browser_open。存在历史检查点时，先调用 browser_checkpoint_evidence(action="list")，再按 evidence_id 读取需要的旧响应体。
2. 可在同一 turn 批量调用多个互不冲突的只读工具；页面交互工具会顺序执行。根据工具结果持续调整计划，不要重复低收益动作。
3. browser_action_feedback 只返回动作后的 snapshot 与原始网络增量，不会告诉你 accepted/verified；证据是否有效由你判断。
4. 对“全部评论/全部记录”任务，优先观察多个分页请求或明确终页，并结合 URL/query、响应 JSON、记录 ID 和结束字段自行判断是否完整。
5. 若首个接口只有少量记录但 total 很大，继续探索排序菜单、最新评论/全部视图、滚动加载、其他请求族或页面运行时状态。
6. API 方案应尽量提交真实观察到的 url、data_path、字段映射及分页策略；DOM 方案可用 browser_verify_selectors 辅助自检。
7. 动态签名或不透明 cursor 由网页生成时，设置 requires_browser_replay=true，并在 interaction_plan 中记录可重放的语义动作。点击时给 browser_action_feedback 提供 target_descriptor（role/name 或稳定 selector）；interaction_plan 禁止使用只能在当前会话生效的 @eN ref 作为唯一定位信息。
8. 普通 explore 模式下，只有页面证据确实显示目标数据被登录墙阻断时才能调用 browser_manual_login；当 operation_mode=resolve_authentication 时，Supervisor 已通过独立认证能力授权该动作，必须执行专用认证协议。人工确认只代表用户完成操作，不代表认证已验证；调用后必须使用 browser_auth_probe 检查当前页面事实，并在 submit_parser.auth 中明确 authentication_state、authenticated 与 verification_state。
9. 不得读取 Cookie、storage、认证头或凭据，不得调用 shell、文件、上传和表单工具。人工登录后必须保持同一浏览器指纹，不得建议切换 User-Agent、viewport、locale、timezone、浏览器引擎或代理来绕过风控。
10. 一旦已观察到非空目标记录数组、稳定 item ID 和字段结构，立即停止通用 DOM 探索；只读取候选响应体、补充分页信息并调用 submit_parser。证据足够或预算接近耗尽时必须提交。若只缺分页终止证据，可提交 runtime_validation_required=true 的降级合同交给 Code Agent 运行时验证，不得继续无目标 snapshot/evaluate。最终判断、confidence 和分析摘要由你填写。
11. 没有新证据时，同一工具的等价参数最多重试两次；之后必须改变探索维度（DOM、network、response body 或页面交互），或基于当前证据提交结论。
12. API cursor 方案可提交 pagination_contract 记录 execution_mode、terminal 和 observed_transitions。若证据不足，由你在 analysis_summary、confidence 和 warnings 中如实说明，也可以交给 Code Agent 运行时继续确认。
13. 选择 API 主端点时，应优先依据真实非空记录数组、稳定 ID、data_path 和字段映射自行判断；description、metadata、统计或空数组通常只是辅助证据。
14. 评论任务打开页面后应尽早激活评论区并检查新增网络响应；在目标记录仍为 0 时，不要反复 snapshot/evaluate，应优先 browser_activate_comments、browser_action_feedback、browser_network_log(include_body=true) 和 browser_network_response_body。
15. api_endpoints 必须标明 source=observed|historical|hypothesized 与 verified。只有本轮网络/响应体直接观察到的接口才能标 observed；经验猜测必须标 hypothesized。
16. checkpoint 中的证据按 target/login/challenge/pre_login/post_login 分区。页面重开或登录后只优先使用当前 target_post_login 证据；历史证据可参考但不能混为本轮已验证事实。

提交结果应包含足够让 Code Agent 执行的页面事实、你的置信度、分析摘要和未解决问题；具体字段由 submit_parser schema 表达。"""


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "site").lower()
    except Exception:
        return "site"


def _safe_session(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._") or "default"


def _auth_directory() -> Path:
    return Path(os.getenv(
        "BROWSER_AUTH_STATE_DIR",
        str(Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "browser_auth_states"),
    ))


def _bounded_result(value: Any, max_chars: int = 40_000) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        encoded = str(value)
    if len(encoded) <= max_chars:
        return value
    truncated = {
        "ok": bool(value.get("ok", True)) if isinstance(value, dict) else True,
        "truncated": True,
        "original_chars": len(encoded),
        "json_preview": encoded[:max_chars],
    }
    if isinstance(value, dict) and isinstance(value.get("_agent_evidence"), dict):
        truncated["_agent_evidence"] = value["_agent_evidence"]
    return truncated


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_CURSOR_KEY_RE = re.compile(r"cursor|offset|page|pagination|continuation|next", re.I)
_ITEM_ID_KEYS = {
    "id", "cid", "rpid", "reply_id", "comment_id", "item_id", "record_id",
}


def _fingerprint(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _tool_progress_key(name: str, arguments: Dict[str, Any], target_url: str) -> str:
    """Normalize equivalent actions so superficial argument changes cannot bypass no-progress limits."""
    args = dict(arguments or {})
    if name == "browser_open":
        payload = {"url": target_url}
    elif name in {"browser_click", "browser_action_feedback"}:
        descriptor = args.get("target_descriptor") if isinstance(args.get("target_descriptor"), dict) else {}
        payload = {
            "action": args.get("action", "click"),
            "target": args.get("selector") or args.get("target") or descriptor.get("selector") or descriptor.get("name") or descriptor.get("text"),
        }
    elif name in {"browser_network_response_body", "browser_checkpoint_evidence"}:
        payload = {
            "index": args.get("index"),
            "evidence_id": args.get("evidence_id"),
            "action": args.get("action"),
            "partition": args.get("partition"),
        }
    elif name == "browser_evaluate":
        payload = {"javascript": re.sub(r"\s+", " ", str(args.get("javascript") or "")).strip()}
    elif name == "browser_network_log":
        payload = {
            "url_pattern": args.get("url_pattern"),
            "after_index": args.get("after_index"),
            "include_body": bool(args.get("include_body")),
        }
    else:
        payload = args
    return f"{name}:{_fingerprint(payload)}"


def _url_family(raw_url: str) -> str:
    try:
        parsed = urlparse(str(raw_url or ""))
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        return f"{host}{path}" if host else ""
    except Exception:
        return ""



def _has_nonempty_object_list(value: Any, depth: int = 0) -> bool:
    if depth > 10:
        return False
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            return True
        return any(_has_nonempty_object_list(item, depth + 1) for item in value[:100])
    if isinstance(value, dict):
        return any(_has_nonempty_object_list(item, depth + 1) for item in list(value.values())[:300])
    return False


def _normalize_json_path(path: Any) -> str:
    text = str(path or "").strip()
    text = re.sub(r"^\$\.?", "", text)
    text = re.sub(r"\[(?:\*|\d*)\]", "", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip(".")


def _extract_record_list_shapes(
    value: Any,
    path: str = "$",
    depth: int = 0,
) -> Dict[str, set]:
    """Map actual record-array paths to the keys observed on ID-bearing rows."""
    shapes: Dict[str, set] = {}
    if depth > 10:
        return shapes
    if isinstance(value, list):
        record_rows = []
        for item in value[:1000]:
            if not isinstance(item, dict):
                continue
            has_direct_id = any(
                str(key).lower() in _ITEM_ID_KEYS
                and isinstance(candidate, (str, int))
                for key, candidate in list(item.items())[:300]
            )
            if has_direct_id:
                record_rows.append(item)
        if record_rows:
            normalized = _normalize_json_path(path)
            keys = set()
            for row in record_rows[:30]:
                keys.update(str(key) for key in list(row.keys())[:300])
            if normalized and keys:
                shapes[normalized] = keys
        for index, item in enumerate(value[:100]):
            if isinstance(item, (dict, list)):
                child_path = f"{path}[{index}]"
                for child, keys in _extract_record_list_shapes(
                    item, child_path, depth + 1
                ).items():
                    shapes.setdefault(child, set()).update(keys)
    elif isinstance(value, dict):
        for key, candidate in list(value.items())[:300]:
            if isinstance(candidate, (dict, list)):
                child_path = f"{path}.{key}"
                for child, keys in _extract_record_list_shapes(
                    candidate, child_path, depth + 1
                ).items():
                    shapes.setdefault(child, set()).update(keys)
    return shapes


def _extract_record_list_ids(value: Any, depth: int = 0) -> set:
    """Return stable IDs found on objects that are actual list records.

    A metadata/description response may contain a top-level ``id`` without
    containing any target records.  Only IDs attached to dictionary elements
    of a non-empty array count as collection evidence.
    """
    found: set = set()
    if depth > 10:
        return found
    if isinstance(value, list):
        for item in value[:1000]:
            if isinstance(item, dict):
                for key, candidate in list(item.items())[:300]:
                    if str(key).lower() in _ITEM_ID_KEYS and isinstance(candidate, (str, int)):
                        found.add(str(candidate))
                # Nested arrays such as child replies are valid collection
                # evidence too, but scalar IDs buried in metadata objects are
                # deliberately ignored.
                for candidate in list(item.values())[:300]:
                    if isinstance(candidate, (dict, list)):
                        found.update(_extract_record_list_ids(candidate, depth + 1))
            elif isinstance(item, list):
                found.update(_extract_record_list_ids(item, depth + 1))
    elif isinstance(value, dict):
        for candidate in list(value.values())[:300]:
            if isinstance(candidate, (dict, list)):
                found.update(_extract_record_list_ids(candidate, depth + 1))
    return found



def _extract_record_list_keys(value: Any, depth: int = 0) -> set:
    """Return top-level keys of list records that carry a stable ID."""
    found: set = set()
    if depth > 10:
        return found
    if isinstance(value, list):
        for item in value[:1000]:
            if isinstance(item, dict):
                has_direct_id = any(
                    str(key).lower() in _ITEM_ID_KEYS
                    and isinstance(candidate, (str, int))
                    for key, candidate in list(item.items())[:300]
                )
                if has_direct_id:
                    found.update(str(key) for key in list(item.keys())[:300])
                for candidate in list(item.values())[:300]:
                    if isinstance(candidate, (dict, list)):
                        found.update(_extract_record_list_keys(candidate, depth + 1))
            elif isinstance(item, list):
                found.update(_extract_record_list_keys(item, depth + 1))
    elif isinstance(value, dict):
        for candidate in list(value.values())[:300]:
            if isinstance(candidate, (dict, list)):
                found.update(_extract_record_list_keys(candidate, depth + 1))
    return found

def _collect_evidence(value: Any) -> Dict[str, set]:
    """Extract objective, site-agnostic evidence handles from one tool result."""
    found = {
        "request_states": set(),
        "request_families": set(),
        "response_bodies": set(),
        "item_ids": set(),
        "item_response_families": set(),
        "list_response_families": set(),
        "item_record_keys": set(),
        "item_list_shapes": set(),
    }
    seen: set = set()

    def add_url(raw: str) -> None:
        for match in _URL_RE.findall(raw or ""):
            url = match.rstrip("),]}")
            try:
                parsed = urlparse(url)
                family = f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"
                if family:
                    found["request_families"].add(family)
                cursor_query = tuple(sorted(
                    (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                    if _CURSOR_KEY_RE.search(key)
                ))
                if cursor_query:
                    found["request_states"].add(f"{family}?{cursor_query!r}")
            except Exception:
                continue

    def walk(
        node: Any,
        key_hint: str = "",
        depth: int = 0,
        record_context: bool = False,
    ) -> None:
        if depth > 12:
            return
        marker = id(node)
        if isinstance(node, (dict, list)):
            if marker in seen:
                return
            seen.add(marker)
        if isinstance(node, dict):
            raw_url = str(node.get("url") or node.get("observed_url") or "")
            raw_body = node.get("response_body") if "response_body" in node else node.get("body")
            response_summary = (
                node.get("response_summary")
                if isinstance(node.get("response_summary"), dict)
                else {}
            )
            if raw_url and response_summary:
                family = _url_family(raw_url)
                summary_ids = {
                    str(value) for value in (response_summary.get("sample_item_ids") or [])
                    if isinstance(value, (str, int))
                }
                list_candidates = [
                    item for item in (response_summary.get("list_candidates") or [])
                    if isinstance(item, dict) and int(item.get("length", 0) or 0) > 0
                ]
                id_list_candidates = [
                    candidate for candidate in list_candidates
                    if candidate.get("sample_id_keys")
                ]
                if summary_ids and family and id_list_candidates:
                    found["item_response_families"].add(family)
                    found["item_ids"].update(summary_ids)
                    for candidate in id_list_candidates:
                        normalized_path = _normalize_json_path(candidate.get("path"))
                        for key in (candidate.get("sample_keys") or []):
                            found["item_record_keys"].add(f"{family}|{str(key)}")
                            if normalized_path:
                                found["item_list_shapes"].add(
                                    f"{family}|{normalized_path}|{str(key)}"
                                )
                if list_candidates and family:
                    found["list_response_families"].add(family)
            if raw_url and isinstance(raw_body, str) and raw_body.lstrip().startswith(("{", "[")):
                try:
                    parsed_body = json.loads(raw_body)
                except Exception:
                    parsed_body = None
                if parsed_body is not None:
                    family = _url_family(raw_url)
                    record_ids = _extract_record_list_ids(parsed_body)
                    record_keys = _extract_record_list_keys(parsed_body)
                    record_shapes = _extract_record_list_shapes(parsed_body)
                    if record_ids and family and record_shapes:
                        found["item_response_families"].add(family)
                        found["item_ids"].update(record_ids)
                        for key in record_keys:
                            found["item_record_keys"].add(f"{family}|{key}")
                        for record_path, keys in record_shapes.items():
                            for key in keys:
                                found["item_list_shapes"].add(
                                    f"{family}|{record_path}|{key}"
                                )
                    if _has_nonempty_object_list(parsed_body) and family:
                        found["list_response_families"].add(family)
            for key, item in list(node.items())[:1000]:
                key_text = str(key).lower()
                if (
                    record_context
                    and key_text in _ITEM_ID_KEYS
                    and isinstance(item, (str, int))
                ):
                    found["item_ids"].add(str(item))
                if key_text in {"response_body", "body", "json_preview"}:
                    found["response_bodies"].add(_fingerprint(item))
                walk(item, key_text, depth + 1, False)
        elif isinstance(node, list):
            for item in node[:1000]:
                walk(item, key_hint, depth + 1, isinstance(item, dict))
        elif isinstance(node, str):
            add_url(node)
            if key_hint in {"response_body", "body"} or node.lstrip().startswith(("{", "[")):
                if len(node) >= 20:
                    found["response_bodies"].add(hashlib.sha256(
                        node.encode("utf-8", errors="replace")
                    ).hexdigest())
                if len(node) <= 200_000:
                    try:
                        walk(json.loads(node), key_hint, depth + 1, False)
                    except Exception:
                        pass

    walk(value)
    return found


def _merge_interaction_plan(candidate_plan: Any, trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge Pi's semantic plan with replayable actions actually executed."""
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    replay_tools = {
        "browser_action_feedback", "browser_activate_comments",
        "browser_click", "browser_scroll", "browser_infinite_scroll",
        "browser_wait_dynamic", "browser_reload",
    }
    sources: List[Any] = list(candidate_plan) if isinstance(candidate_plan, list) else []
    sources.extend(
        {"tool": item.get("tool"), "args": dict(item.get("args") or {})}
        for item in trace if item.get("tool") in replay_tools and item.get("status") != "blocked"
    )
    for raw in sources:
        if not isinstance(raw, dict) or raw.get("tool") not in replay_tools:
            continue
        step = {"tool": str(raw.get("tool")), "args": dict(raw.get("args") or {})}
        args = step["args"]
        target = str(args.get("target") or args.get("selector") or "")
        descriptor = args.get("target_descriptor")
        if step["tool"] in {"browser_click", "browser_action_feedback"} and target.startswith("@e"):
            if not isinstance(descriptor, dict) or not any(descriptor.values()):
                continue
        signature = _fingerprint(step)
        if signature in seen:
            continue
        seen.add(signature)
        merged.append(step)
    return merged[:30]


def _target_item_evidence_ready(evidence: Dict[str, set]) -> bool:
    """Return True once a real non-empty target record list is objectively observed."""
    return bool(
        len(evidence.get("item_ids", set())) >= 3
        and evidence.get("item_response_families")
        and evidence.get("list_response_families")
        and evidence.get("item_list_shapes")
    )


def _pagination_observation_ready(evidence: Dict[str, set]) -> bool:
    return bool(
        len(evidence.get("request_states", set())) >= 2
        and len(evidence.get("response_bodies", set())) >= 2
    )


def _normalize_pagination_contract(
    result: Dict[str, Any],
    evidence: Dict[str, set],
    exhaustive_required: bool,
    target_fields: List[str],
) -> Dict[str, Any]:
    pagination = result.get("pagination") if isinstance(result.get("pagination"), dict) else {}
    supplied = (
        result.get("pagination_contract")
        if isinstance(result.get("pagination_contract"), dict) else {}
    )
    transitions = supplied.get("observed_transitions")
    transitions = [item for item in transitions if isinstance(item, dict)] if isinstance(transitions, list) else []
    terminal = supplied.get("terminal") if isinstance(supplied.get("terminal"), dict) else {}
    if (
        not terminal
        and pagination.get("has_more_path")
        and "has_more_inverted" in pagination
    ):
        terminal = {
            "path": pagination.get("has_more_path"),
            "value_means_end": True if pagination.get("has_more_inverted") else False,
        }

    ledger_second_state = _pagination_observation_ready(evidence)
    second_state = bool(
        ledger_second_state
        and (pagination.get("second_page_verified") is True or len(transitions) >= 2)
    )
    terminal_observation = (
        supplied.get("terminal_observation")
        if isinstance(supplied.get("terminal_observation"), dict) else {}
    )
    terminal_observed = bool(
        len(evidence.get("response_bodies", set())) >= 1
        and (
            "terminal_raw" in terminal_observation
            or any(
                item.get("terminal_observed") is True and "terminal_raw" in item
                for item in transitions
            )
        )
    )
    evidence_complete = bool(not exhaustive_required or second_state or terminal_observed)

    execution_mode = str(supplied.get("execution_mode") or "").strip().lower()
    direct_proven = bool(
        execution_mode == "direct_http"
        and second_state
        and pagination.get("next_cursor_path")
    )
    if str(pagination.get("type") or "") == "cursor" and not direct_proven:
        execution_mode = "browser_replay"
    elif execution_mode not in {"direct_http", "browser_replay"}:
        execution_mode = "direct_http"

    issues: List[str] = []
    if str(result.get("data_source") or "").lower() == "api":
        endpoints = [
            item for item in (result.get("api_endpoints") or [])
            if isinstance(item, dict)
        ]
        required_fields = {str(value) for value in target_fields if str(value).strip()}
        backed_endpoints = []
        for endpoint in endpoints:
            endpoint_url = str(endpoint.get("observed_url") or endpoint.get("url") or "")
            family = _url_family(endpoint_url)
            mapping = endpoint.get("field_mapping") if isinstance(endpoint.get("field_mapping"), dict) else {}
            endpoint_path = (urlparse(endpoint_url).path or "").lower()
            auxiliary_endpoint = bool(re.search(
                r"(?:/|^)(?:description|metadata|detail|info|stat|status|config|setting|permission|prohibition)(?:/|$)",
                endpoint_path,
            ))
            data_path = _normalize_json_path(endpoint.get("data_path"))
            shape_prefix = f"{family}|{data_path}|"
            observed_record_keys = {
                value[len(shape_prefix):]
                for value in evidence.get("item_list_shapes", set())
                if data_path and value.startswith(shape_prefix)
            }
            mapping_roots = set()
            for mapping_path in mapping.values():
                normalized_path = _normalize_json_path(mapping_path)
                if data_path and normalized_path.startswith(data_path + "."):
                    normalized_path = normalized_path[len(data_path) + 1:]
                root = normalized_path.split(".", 1)[0].split("[", 1)[0]
                if root:
                    mapping_roots.add(root)
            shape_backed = bool(
                mapping_roots
                and mapping_roots.issubset(observed_record_keys)
            )
            if (
                endpoint_url
                and not auxiliary_endpoint
                and endpoint.get("data_path")
                and required_fields.issubset({str(key) for key in mapping})
                and family in evidence.get("item_response_families", set())
                and shape_backed
            ):
                backed_endpoints.append(endpoint_url)
        if not evidence.get("item_ids"):
            issues.append("missing_observed_item_ids")
        if not endpoints:
            issues.append("missing_api_endpoint")
        elif not backed_endpoints:
            issues.append("endpoint_not_backed_by_nonempty_item_response")

    if exhaustive_required and str(pagination.get("type") or "") != "cursor":
        issues.append("pagination_type_not_cursor")
    if exhaustive_required and not second_state and not terminal_observed:
        issues.append("missing_second_cursor_or_terminal_evidence")
    if exhaustive_required and (
        not terminal.get("path") or "value_means_end" not in terminal
    ):
        issues.append("missing_terminal_semantics")

    item_issue_codes = {
        "missing_observed_item_ids",
        "missing_api_endpoint",
        "endpoint_not_backed_by_nonempty_item_response",
    }
    pagination_issue_codes = {
        "pagination_type_not_cursor",
        "missing_second_cursor_or_terminal_evidence",
        "missing_terminal_semantics",
    }
    item_evidence_complete = not bool(item_issue_codes.intersection(issues))
    pagination_evidence_complete = bool(
        not exhaustive_required
        or not pagination_issue_codes.intersection(issues)
    )
    runtime_validation_required = bool(
        exhaustive_required
        and item_evidence_complete
        and not pagination_evidence_complete
    )

    return {
        **supplied,
        "execution_mode": execution_mode,
        "terminal": terminal,
        "observed_transitions": transitions,
        "item_evidence_complete": item_evidence_complete,
        "pagination_evidence_complete": pagination_evidence_complete,
        "runtime_validation_required": runtime_validation_required,
        "evidence_complete": bool(item_evidence_complete and pagination_evidence_complete),
        "issues": issues,
        "ledger": {
            "request_families": len(evidence.get("request_families", set())),
            "request_states": len(evidence.get("request_states", set())),
            "response_bodies": len(evidence.get("response_bodies", set())),
            "unique_item_ids": len(evidence.get("item_ids", set())),
            "item_response_families": sorted(evidence.get("item_response_families", set()))[:20],
            "list_response_families": sorted(evidence.get("list_response_families", set()))[:20],
            "item_record_keys": sorted(evidence.get("item_record_keys", set()))[:100],
            "item_list_shapes": sorted(evidence.get("item_list_shapes", set()))[:100],
        },
    }



_BROWSER_CHECKPOINT_VERSION = 3
_BROWSER_CHECKPOINT_MAX_REQUESTS = 240
_BROWSER_CHECKPOINT_MAX_BODIES = 24
_BROWSER_CHECKPOINT_BODY_CHARS = 2_000_000


def _checkpoint_evidence_id(kind: str, value: Dict[str, Any]) -> str:
    seed = {
        "kind": str(kind),
        "index": value.get("index"),
        "url": value.get("url") or value.get("observed_url"),
        "description": value.get("description"),
        "response_summary": value.get("response_summary"),
        "body_prefix": str(value.get("response_body") or value.get("body") or "")[:2000],
    }
    return f"{kind}_{_fingerprint(seed)[:20]}"


def _ensure_checkpoint_ids(material: Dict[str, Any]) -> None:
    for kind, key in (("request", "requests"), ("body", "response_bodies")):
        values = material.get(key) or []
        for item in values:
            if isinstance(item, dict) and not item.get("evidence_id"):
                item["evidence_id"] = _checkpoint_evidence_id(kind, item)


def _checkpoint_task_key(config: Dict[str, Any], target_url: str) -> str:
    raw = str(config.get("task_id") or "").strip()
    if raw:
        return _safe_session(raw)[:80]
    return hashlib.sha256(target_url.encode("utf-8", errors="replace")).hexdigest()[:20]


def _browser_checkpoint_path(config: Dict[str, Any], target_url: str) -> Path:
    root = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "checkpoints"
    return root / f"browser_{_checkpoint_task_key(config, target_url)}.json"


def _same_target_page(current_url: str, target_url: str) -> bool:
    try:
        current = urlparse(str(current_url or ""))
        target = urlparse(str(target_url or ""))
        return bool(
            current.scheme in {"http", "https"}
            and current.hostname
            and current.hostname.lower() == (target.hostname or "").lower()
            and current.path.rstrip("/") == target.path.rstrip("/")
        )
    except Exception:
        return False


def _checkpoint_json_value(value: Any, *, max_string: int = 8_000, depth: int = 0) -> Any:
    if depth > 7:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_string]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in list(value.items())[:180]:
            result[str(key)] = _checkpoint_json_value(item, max_string=max_string, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _checkpoint_json_value(item, max_string=max_string, depth=depth + 1)
            for item in list(value)[:220]
        ]
    return str(value)[:max_string]


def _load_browser_checkpoint(
    config: Dict[str, Any],
    target_url: str,
    prior_parser_result: Dict[str, Any],
) -> Dict[str, Any]:
    candidates = [
        config.get("browser_checkpoint"),
        prior_parser_result.get("_checkpoint") if isinstance(prior_parser_result, dict) else None,
    ]
    path = _browser_checkpoint_path(config, target_url)
    if path.is_file():
        try:
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            log_event(logger, "checkpoint.load", level="WARNING", status="failed", agent="browser", scope="browser", path=path, error_type="checkpoint_read_failed", reason=str(exc))
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if int(raw.get("version", 0) or 0) < 1:
            continue
        if str(raw.get("target_url") or "") != str(target_url):
            continue
        return dict(raw)
    return {}


def _save_browser_checkpoint(
    config: Dict[str, Any],
    target_url: str,
    checkpoint: Dict[str, Any],
) -> None:
    path = _browser_checkpoint_path(config, target_url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        requests_count = len((checkpoint.get("material") or {}).get("requests") or [])
        bodies_count = len((checkpoint.get("material") or {}).get("response_bodies") or [])
        items_count = len((checkpoint.get("evidence") or {}).get("item_ids") or [])
        # Console logging is based on persisted evidence counts, not transcript
        # churn or stop_reason changes. The full checkpoint is still written.
        signature = f"{requests_count}:{bodies_count}:{items_count}"
        key = str(path)
        if _LAST_CHECKPOINT_LOG_SIGNATURE.get(key) != signature:
            _LAST_CHECKPOINT_LOG_SIGNATURE[key] = signature
            log_event(
                logger, "checkpoint.save", status="saved", agent="browser",
                scope="browser_evidence", path=path,
                persisted_candidate_requests=requests_count,
                persisted_response_bodies=bodies_count,
                persisted_item_ids=items_count,
            )
    except Exception as exc:
        log_event(logger, "checkpoint.save", level="WARNING", status="failed", agent="browser", scope="browser", path=path, error_type="checkpoint_write_failed", reason=str(exc))


def _try_resume_browser_context(
    checkpoint: Dict[str, Any],
    target_url: str,
) -> tuple[bool, Dict[str, Any]]:
    if not checkpoint or not checkpoint.get("mcp_context_reusable"):
        return False, {"ok": False, "reason": "checkpoint_not_reusable"}
    try:
        from mcp_browser_tools import browser_evaluate

        probe = browser_evaluate.invoke({
            "javascript": "() => ({url: location.href, title: document.title, ready: document.readyState})"
        })
        if not isinstance(probe, dict) or not probe.get("ok"):
            return False, {"ok": False, "reason": "page_probe_failed", "probe": probe}
        value = probe.get("result")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {"url": value}
        value = value if isinstance(value, dict) else {}
        current_url = str(value.get("url") or "")
        if not _same_target_page(current_url, target_url):
            return False, {
                "ok": False,
                "reason": "page_target_mismatch",
                "current_url": current_url,
            }
        return True, {
            "ok": True,
            "current_url": current_url,
            "title": str(value.get("title") or "")[:500],
            "ready": value.get("ready"),
        }
    except Exception as exc:
        return False, {"ok": False, "reason": "page_probe_exception", "error": str(exc)[:1000]}


def _new_checkpoint_material(previous: Any = None) -> Dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    material = {
        "requests": [dict(item) for item in (previous.get("requests") or []) if isinstance(item, dict)][-_BROWSER_CHECKPOINT_MAX_REQUESTS:],
        "response_bodies": [dict(item) for item in (previous.get("response_bodies") or []) if isinstance(item, dict)][-_BROWSER_CHECKPOINT_MAX_BODIES:],
        "highest_index": int(previous.get("highest_index", 0) or 0),
        "completed_actions": [dict(item) for item in (previous.get("completed_actions") or []) if isinstance(item, dict)][-40:],
    }
    for collection in ("requests", "response_bodies"):
        for item in material[collection]:
            item.setdefault("partition", "legacy")
            item.setdefault("auth_epoch", 0)
    _ensure_checkpoint_ids(material)
    return material


def _context_from_value(value: Any, fallback: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(fallback or {})
    if isinstance(value, dict):
        for key in ("current_url", "page_url", "href", "resolved_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                context["page_url"] = candidate
                break
        for item in list(value.values())[:120]:
            if isinstance(item, dict):
                child = _context_from_value(item, context)
                if child.get("page_url") != context.get("page_url"):
                    return child
    return context


def _capture_checkpoint_material(
    value: Any,
    material: Dict[str, Any],
    *,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    context = dict(context or {})
    requests_by_key: Dict[str, Dict[str, Any]] = {}
    for item in material.get("requests") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("evidence_id") or (item.get("index") if item.get("index") is not None else _fingerprint(item)))
        requests_by_key[key] = item
    bodies_by_key: Dict[str, Dict[str, Any]] = {}
    for item in material.get("response_bodies") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("evidence_id") or (item.get("index") if item.get("index") is not None else _fingerprint(item)))
        bodies_by_key[key] = item
    seen: set = set()

    def annotate(item: Dict[str, Any], node_context: Dict[str, Any]) -> None:
        item.setdefault("captured_page_url", str(node_context.get("page_url") or "")[:4000])
        item.setdefault("partition", str(node_context.get("partition") or "unknown"))
        item.setdefault("auth_epoch", int(node_context.get("auth_epoch", 0) or 0))
        item.setdefault("captured_at", int(time.time()))

    def walk(node: Any, depth: int = 0, inherited: Optional[Dict[str, Any]] = None) -> None:
        if depth > 7:
            return
        node_context = _context_from_value(node, inherited or context)
        if isinstance(node, (dict, list)):
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
        if isinstance(node, dict):
            highest = node.get("highest_index")
            if isinstance(highest, (int, float, str)):
                try:
                    material["highest_index"] = max(int(material.get("highest_index", 0) or 0), int(highest))
                except Exception:
                    pass
            requests = node.get("requests")
            if isinstance(requests, list):
                for request in requests:
                    if not isinstance(request, dict):
                        continue
                    compact = _checkpoint_json_value(request, max_string=12_000)
                    if not isinstance(compact, dict):
                        continue
                    annotate(compact, node_context)
                    compact.setdefault("evidence_id", _checkpoint_evidence_id("request", compact))
                    index = compact.get("index")
                    key = str(compact.get("evidence_id") or index or _fingerprint(compact))
                    requests_by_key[key] = compact
            raw_url = str(node.get("url") or node.get("observed_url") or "")
            body = node.get("response_body") if "response_body" in node else node.get("body")
            summary = node.get("response_summary") if isinstance(node.get("response_summary"), dict) else None
            index = node.get("index")
            if raw_url and (body is not None or summary):
                compact_body: Dict[str, Any] = {"index": index, "url": raw_url[:4000]}
                annotate(compact_body, node_context)
                if body is not None:
                    compact_body["response_body"] = str(body)[:_BROWSER_CHECKPOINT_BODY_CHARS]
                if summary:
                    compact_body["response_summary"] = _checkpoint_json_value(summary, max_string=8_000)
                compact_body["evidence_id"] = _checkpoint_evidence_id("body", compact_body)
                key = str(compact_body.get("evidence_id") or index or _fingerprint(compact_body))
                bodies_by_key[key] = compact_body
            for item in list(node.values())[:300]:
                walk(item, depth + 1, node_context)
        elif isinstance(node, list):
            for item in node[:300]:
                walk(item, depth + 1, node_context)

    walk(value)
    material["requests"] = list(requests_by_key.values())[-_BROWSER_CHECKPOINT_MAX_REQUESTS:]
    material["response_bodies"] = list(bodies_by_key.values())[-_BROWSER_CHECKPOINT_MAX_BODIES:]
    _ensure_checkpoint_ids(material)


def _browser_body_directory(config: Dict[str, Any], target_url: str) -> Path:
    root = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "checkpoints" / "browser_bodies"
    return root / _checkpoint_task_key(config, target_url)


def _material_for_checkpoint_storage(
    config: Dict[str, Any],
    target_url: str,
    material: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist full bodies as stable files and keep only bounded previews in checkpoint JSON."""
    body_dir = _browser_body_directory(config, target_url)
    body_dir.mkdir(parents=True, exist_ok=True)
    stored = _new_checkpoint_material(material)
    stored_bodies: List[Dict[str, Any]] = []
    for raw in material.get("response_bodies") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        body = str(item.get("response_body") or "")
        if body:
            digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
            body_file = body_dir / f"{digest}.body.txt"
            if not body_file.is_file():
                tmp = body_file.with_suffix(body_file.suffix + ".tmp")
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, body_file)
            item["body_sha256"] = digest
            item["body_file"] = str(body_file.resolve())
            item["body_chars"] = len(body)
            item["response_body"] = body[:2000]
            item["body_preview_truncated"] = len(body) > 2000
        stored_bodies.append(item)
    stored["response_bodies"] = stored_bodies[-_BROWSER_CHECKPOINT_MAX_BODIES:]
    return stored


def _read_checkpoint_body(item: Dict[str, Any], max_chars: int) -> tuple[str, str]:
    body_file = str(item.get("body_file") or "").strip()
    if body_file:
        try:
            path = Path(body_file).resolve()
            workspace = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")).resolve()
            path.relative_to(workspace)
            if path.is_file():
                body = path.read_text(encoding="utf-8", errors="replace")
                return body[:max_chars], "local_body_file"
        except Exception:
            pass
    body = str(item.get("response_body") or "")
    return body[:max_chars], "checkpoint_preview"


def _seed_evidence_from_checkpoint(
    evidence: Dict[str, set],
    checkpoint: Dict[str, Any],
    *,
    include_aggregate: bool = True,
) -> None:
    if not include_aggregate:
        return
    previous = checkpoint.get("evidence") if isinstance(checkpoint.get("evidence"), dict) else {}
    for key in evidence:
        values = previous.get(key)
        if isinstance(values, list):
            evidence[key].update(str(value) for value in values if value is not None)


def _safe_agent_transcript(value: Any, max_messages: int = 120) -> List[Dict[str, Any]]:
    return sanitize_agent_transcript(value, max_messages=max_messages)


def _build_browser_checkpoint(
    *,
    config: Dict[str, Any],
    target_url: str,
    resolved_session: Optional[str],
    page_reused: bool,
    resume_probe: Dict[str, Any],
    evidence: Dict[str, set],
    material: Dict[str, Any],
    trace: List[Dict[str, Any]],
    candidate: Optional[Dict[str, Any]],
    stop_reason: Any,
    agent_transcript: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    completed_actions = []
    for item in trace:
        if not isinstance(item, dict) or item.get("status") == "blocked":
            continue
        if item.get("tool") not in {
            "browser_action_feedback", "browser_click", "browser_scroll",
            "browser_infinite_scroll", "browser_wait_dynamic", "browser_reload",
            "browser_activate_comments", "browser_manual_login",
        }:
            continue
        completed_actions.append({
            "tool": item.get("tool"),
            "args": _checkpoint_json_value(item.get("args") or {}, max_string=2000),
            "status": item.get("status"),
            "partition": item.get("partition"),
        })
    merged_actions: List[Dict[str, Any]] = []
    seen_actions: set = set()
    for action in list(material.get("completed_actions") or []) + completed_actions:
        if not isinstance(action, dict):
            continue
        signature = _fingerprint(action)
        if signature in seen_actions:
            continue
        seen_actions.add(signature)
        merged_actions.append(action)
    material["completed_actions"] = merged_actions[-40:]
    stored_material = _material_for_checkpoint_storage(config, target_url, material)
    partition_counts: Dict[str, Dict[str, int]] = {}
    for collection in ("requests", "response_bodies"):
        for item in stored_material.get(collection) or []:
            if not isinstance(item, dict):
                continue
            partition = str(item.get("partition") or "unknown")
            bucket = partition_counts.setdefault(partition, {"requests": 0, "response_bodies": 0})
            bucket[collection] += 1
    return {
        "version": _BROWSER_CHECKPOINT_VERSION,
        "task_key": _checkpoint_task_key(config, target_url),
        "target_url": target_url,
        "updated_at": int(time.time()),
        "session_name": resolved_session,
        "mcp_context_reusable": True,
        "page_reused_this_attempt": bool(page_reused),
        "resume_probe": _checkpoint_json_value(resume_probe, max_string=3000),
        "stop_reason": str(stop_reason or "")[:500],
        "evidence": {
            key: sorted(str(value) for value in values)[:2000]
            for key, values in evidence.items()
        },
        "evidence_partitions": partition_counts,
        "material": _checkpoint_json_value(stored_material, max_string=_BROWSER_CHECKPOINT_BODY_CHARS),
        "agent_transcript": _safe_agent_transcript(agent_transcript),
        "candidate_summary": _checkpoint_json_value({
            "page_type": (candidate or {}).get("page_type"),
            "data_source": (candidate or {}).get("data_source"),
            "api_endpoints": (candidate or {}).get("api_endpoints", []),
            "pagination": (candidate or {}).get("pagination", {}),
            "pagination_contract": (candidate or {}).get("pagination_contract", {}),
            "auth": (candidate or {}).get("auth", {}),
            "verification_errors": (candidate or {}).get("verification_errors", []),
        }, max_string=10_000),
    }


def _load_auth_infrastructure(
    target_url: str,
    session_name: Optional[str],
    skip_saved: bool,
) -> tuple[Optional[str], Dict[str, Any]]:
    from mcp_browser_tools import browser_load_auth_state, configure_browser_mcp

    resolved = _safe_session(session_name or _domain(target_url))
    state_path = _auth_directory() / f"{resolved}.json"
    should_load = bool(session_name or (state_path.exists() and not skip_saved))
    phase: Dict[str, Any] = {
        "ok": True,
        "attempted": False,
        "state_loaded": False,
        "session_name": None,
        "authentication_state": "anonymous",
        "verification_state": "unverified",
        "error": None,
    }
    if not should_load:
        configure_browser_mcp(None, headless=True, restart=True)
        return None, phase
    try:
        loaded = browser_load_auth_state.invoke({
            "session_name": resolved,
            "state_path": str(state_path),
        })
        if isinstance(loaded, dict) and loaded.get("ok"):
            phase.update({
                "state_loaded": True,
                "session_name": resolved,
                "state_diagnostics": loaded.get("state_diagnostics", {}),
                "authentication_state": "provisional",
                "verification_state": "unverified",
            })
            log_event(logger, "agent.context", status="loaded", agent="browser", phase="explore", auth_state="loaded", authentication_state="provisional", verification_state="unverified", session=resolved)
            return resolved, phase
        phase.update({
            "ok": False,
            "error": (
                loaded.get("error", "load_auth_state_failed")
                if isinstance(loaded, dict) else str(loaded)
            ),
        })
    except Exception as exc:
        phase.update({"ok": False, "error": str(exc)})
    configure_browser_mcp(None, headless=True, restart=True)
    log_event(logger, "agent.context", level="WARNING", status="degraded", agent="browser", phase="explore", auth_state="anonymous", reason=phase["error"])
    return None, phase


def _full_task_prompt(
    target_url: str,
    target_fields: List[str],
    rag_hits: List[Dict[str, Any]],
    max_turns: int,
    max_tools: int,
    failure_feedback: Optional[Dict[str, Any]] = None,
    recovery_context: Optional[Dict[str, Any]] = None,
    operation_mode: str = "explore",
    required_action: Optional[str] = None,
) -> str:
    if operation_mode == "resolve_authentication":
        return f"""执行独立认证协议，不进行普通网页探索。

目标 URL: {target_url}
目标字段: {json.dumps(target_fields, ensure_ascii=False)}
required_action: {required_action or 'manual_login_and_verify'}

这是 Supervisor 通过专用能力下达的认证操作，不是建议性 feedback。严格按以下协议执行：
1. 首先调用 browser_manual_login，打开可见浏览器并等待用户完成登录、验证码或 MFA。
2. 人工确认仅为 provisional；随后必须调用 browser_auth_probe。
3. 根据 auth probe 的当前 URL、登录/挑战信号和目标页匹配情况，自行判断 verified、required、challenge 或 rejected。
4. 最后调用 submit_parser，只提交认证事实和简短分析；不要继续 network、DOM、点击、滚动或接口探索。
5. 登录后不得切换 User-Agent、viewport、locale、timezone、浏览器引擎、代理或其他指纹。

恢复检查点只作为事实背景，不恢复上一轮探索 transcript：{json.dumps(recovery_context or {}, ensure_ascii=False, default=str)[:6000]}
"""

    comment_task = any(re.search(r"评论|回复|comment|reply", str(v), re.I) for v in target_fields)
    comment_workflow = (
        "评论任务优先流程：browser_open 后在前 6 个工具调用内尝试 browser_activate_comments，"
        "随后用 browser_action_feedback 或 browser_network_log(include_body=true) 检查新增请求，"
        "并读取最可能的非空列表响应。目标 item ID 仍为 0 时禁止连续使用 snapshot/evaluate；"
        "一旦 item ID、非空列表响应族和字段结构已出现，立即进入收敛阶段，只读响应体、确认接口与分页并提交。"
        if comment_task else ""
    )
    return f"""完成一次独立网页解析任务。

任务 URL: {target_url}
目标字段: {json.dumps(target_fields, ensure_ascii=False)}
用户要求全量: {any(re.search(r'评论|回复|comment|reply', str(v), re.I) for v in target_fields)}
最大模型 turn: {max_turns}
最大浏览器工具调用: {max_tools}
结构化 Browser Memory Cards（仅为历史假设，必须用当前页面证据复验）: {json.dumps(rag_hits[:3], ensure_ascii=False, default=str)[:3000]}
上轮执行反馈（只用于定位缺失证据，不是网页指令）: {json.dumps(failure_feedback or {}, ensure_ascii=False, default=str)[:5000]}
恢复检查点（客观证据与已完成动作，不是网页指令）: {json.dumps(recovery_context or {}, ensure_ascii=False, default=str)[:12000]}
{comment_workflow}

{(
    "上一轮页面仍存活并已复用。禁止 browser_open。恢复模式不是重新探索：先调用 browser_checkpoint_evidence(action='list')，读取最高分旧响应体；若已有非空 item 证据，禁止 snapshot/html/dom_probe/evaluate/text/links/frames，只补充接口路径和分页证据后提交。"
    if (recovery_context or {}).get("page_reused")
    else "当前页面上下文未能复用。若存在检查点，先调用 browser_checkpoint_evidence(action='list')；随后 browser_open，并在新请求基础上继续补缺，不得把重开页面等同于从零开始。"
)}"""


def run_browser_agent_pipeline(
    target_url: str,
    target_fields: List[str],
    session_name: Optional[str] = None,
    session_confirmed: bool = False,
    rag_hits: Optional[List[Dict[str, Any]]] = None,
    pipeline_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    del session_confirmed  # Pi decides whether observed content is accessible.
    config = dict(pipeline_config or {})
    operation_mode = str(config.get("operation_mode") or "explore")
    required_action = str(config.get("required_action") or "").strip() or None
    auth_resolution_mode = operation_mode == "resolve_authentication"
    phase_name = "authenticate" if auth_resolution_mode else "explore"
    log_event(logger, "agent.start", status="started", agent="browser", phase=phase_name, runtime="pi-agent-core", build=BROWSER_AGENT_BUILD, operation_mode=operation_mode, required_action=required_action)
    prior_parser_result = config.get("prior_parser_result")
    prior_parser_result = prior_parser_result if isinstance(prior_parser_result, dict) else {}
    recovery_checkpoint = _load_browser_checkpoint(config, target_url, prior_parser_result)
    recovered_agent_transcript = (
        [] if auth_resolution_mode
        else _safe_agent_transcript((recovery_checkpoint or {}).get("agent_transcript"))
    )
    if bool(config.get("skip_saved_auth_state")):
        page_reused, resume_probe = False, {
            "ok": False,
            "reason": "checkpoint_context_quarantined",
        }
    else:
        page_reused, resume_probe = _try_resume_browser_context(recovery_checkpoint, target_url)
    if page_reused:
        resolved_session = str(
            recovery_checkpoint.get("session_name") or session_name or ""
        ).strip() or None
        auth_phase = {
            "ok": True,
            "attempted": False,
            "state_loaded": bool(resolved_session),
            "session_name": resolved_session,
            "error": None,
            "checkpoint_resumed": True,
            "resume_probe": resume_probe,
            "authentication_state": str((((recovery_checkpoint.get("candidate_summary") or {}).get("auth") or {}).get("authentication_state") or "provisional")),
            "verification_state": str((((recovery_checkpoint.get("candidate_summary") or {}).get("auth") or {}).get("verification_state") or "unverified")),
        }
        log_event(logger, "checkpoint.restore", status="resumed", agent="browser", scope="browser", session=resolved_session or "anonymous", page_reused=True, requests=len((recovery_checkpoint.get("material") or {}).get("requests") or []), response_bodies=len((recovery_checkpoint.get("material") or {}).get("response_bodies") or []), item_ids=len((recovery_checkpoint.get("evidence") or {}).get("item_ids") or []))
    else:
        resolved_session, auth_phase = _load_auth_infrastructure(
            target_url,
            session_name,
            bool(config.get("skip_saved_auth_state")),
        )
        auth_phase["checkpoint_resumed"] = False
        auth_phase["resume_probe"] = resume_probe
        if recovery_checkpoint:
            log_event(logger, "checkpoint.restore", level="WARNING", status="degraded", agent="browser", scope="browser", page_reused=False, reason=resume_probe.get("reason"), preserved_evidence=True)
    try:
        max_tools = max(12, min(int(
            config.get("browser_agent_max_tools")
            or os.getenv("PI_BROWSER_MAX_TOOLS", "50")
        ), 55))
    except Exception:
        max_tools = 50
    try:
        requested_turns = int(
            config.get("browser_agent_max_turns")
            or os.getenv("PI_BROWSER_MAX_TURNS", str(max_tools + 10))
        )
    except Exception:
        requested_turns = max_tools + 10
    # A one-tool-per-turn pi-agent-core session needs at least one turn per
    # tool plus room to synthesize and call submit_parser. Keep both budgets
    # truthful and reserve ten turns for convergence/submission.
    max_turns = max(12, min(max(requested_turns, max_tools + 10), 60))
    if max_tools >= max_turns:
        max_tools = max(12, max_turns - 5)
    if auth_resolution_mode:
        max_tools = 4
        max_turns = 8
    log_event(logger, "agent.budget", status="ready", agent="browser", phase=phase_name, max_turns=max_turns, max_tools=max_tools, submit_reserve=max_turns - max_tools, operation_mode=operation_mode)
    try:
        timeout_seconds = max(120, min(int(
            config.get("pi_agent_timeout_seconds")
            or os.getenv("PI_AGENT_TIMEOUT_SECONDS", "900")
        ), 1800))
    except Exception:
        timeout_seconds = 900

    from mcp_browser_tools import (
        browser_activate_comments,
        browser_click,
        browser_dom_probe,
        browser_evaluate,
        browser_frames,
        browser_html,
        browser_infinite_scroll,
        browser_links,
        browser_manual_login,
        browser_auth_probe,
        browser_network_log,
        browser_network_response_body,
        browser_open,
        browser_reload,
        browser_scroll,
        browser_snapshot,
        browser_text,
        browser_use_frame,
        browser_verify_selectors,
        browser_wait_dynamic,
    )
    direct_tools = {
        "browser_open": browser_open,
        "browser_snapshot": browser_snapshot,
        "browser_dom_probe": browser_dom_probe,
        "browser_network_log": browser_network_log,
        "browser_network_response_body": browser_network_response_body,
        "browser_activate_comments": browser_activate_comments,
        "browser_auth_probe": browser_auth_probe,
        "browser_verify_selectors": browser_verify_selectors,
        "browser_click": browser_click,
        "browser_scroll": browser_scroll,
        "browser_infinite_scroll": browser_infinite_scroll,
        "browser_wait_dynamic": browser_wait_dynamic,
        "browser_reload": browser_reload,
        "browser_text": browser_text,
        "browser_html": browser_html,
        "browser_links": browser_links,
        "browser_frames": browser_frames,
        "browser_use_frame": browser_use_frame,
        "browser_evaluate": browser_evaluate,
    }
    trace: List[Dict[str, Any]] = []
    evidence: Dict[str, set] = {
        "request_states": set(), "request_families": set(),
        "response_bodies": set(), "item_ids": set(),
        "item_response_families": set(), "list_response_families": set(),
        "item_record_keys": set(),
        "item_list_shapes": set(),
    }
    checkpoint_context_compatible = bool(
        page_reused or (resume_probe or {}).get("reason") not in {"page_target_mismatch", "checkpoint_context_quarantined"}
    )
    _seed_evidence_from_checkpoint(
        evidence, recovery_checkpoint, include_aggregate=checkpoint_context_compatible
    )
    checkpoint_material = _new_checkpoint_material(
        recovery_checkpoint.get("material") if isinstance(recovery_checkpoint, dict) else None
    )
    argument_attempts: Counter = Counter()
    tool_counts: Counter = Counter()
    tool_requested_counts: Counter = Counter()
    tool_blocked_counts: Counter = Counter()
    last_dimension = ""
    dimension_streak = 0
    evaluate_calls = 0
    no_evidence_streak: Counter = Counter()
    action_no_progress: Counter = Counter()
    failed_response_targets: set = set()
    auth_epoch = 1 if resolved_session else 0
    initial_page_url = str((resume_probe or {}).get("current_url") or "")
    current_page_context: Dict[str, Any] = {
        "page_url": initial_page_url,
        "partition": page_partition(initial_page_url, target_url, after_login=bool(resolved_session)),
        "auth_epoch": auth_epoch,
    }

    def update_page_context(value: Any) -> None:
        nonlocal current_page_context
        context = _context_from_value(value, current_page_context)
        page_url = str(context.get("page_url") or current_page_context.get("page_url") or "")
        current_page_context = {
            "page_url": page_url,
            "partition": page_partition(page_url, target_url, after_login=auth_epoch > 0),
            "auth_epoch": auth_epoch,
        }

    def invoke(name: str, arguments: Dict[str, Any]) -> Any:
        nonlocal resolved_session, auth_epoch
        args = dict(arguments)
        if name == "browser_network_log":
            args["max_items"] = min(int(args.get("max_items", 50) or 50), 50)
            args["max_body_items"] = min(int(args.get("max_body_items", 8) or 8), 12)
            args["max_body_chars"] = min(int(args.get("max_body_chars", 30_000) or 30_000), 40_000)
        elif name == "browser_network_response_body":
            args["max_chars"] = min(int(args.get("max_chars", 40_000) or 40_000), 60_000)
        if name == "browser_open":
            # Keep navigation inside the URL explicitly authorized by the user.
            args["url"] = target_url
        if name == "browser_manual_login":
            chosen_session = _safe_session(resolved_session or _domain(target_url))
            state_path = _auth_directory() / f"{chosen_session}.json"
            result = browser_manual_login.invoke({
                "url": target_url,
                "session_name": chosen_session,
                "state_path": str(state_path),
                "timeout_seconds": int(args.get("timeout_seconds", 300) or 300),
                "require_confirmation": True,
            })
            if isinstance(result, dict) and result.get("ok"):
                resolved_session = chosen_session
                auth_epoch += 1
                auth_phase.update({
                    "attempted": True,
                    "state_loaded": True,
                    "session_name": chosen_session,
                    "authentication_state": "provisional",
                    "verification_state": "unverified",
                    "post_login_probe": result.get("post_login_probe") or {},
                    "challenge_detected": bool(result.get("challenge_detected")),
                })
                update_page_context(result.get("post_login_probe") or result)
            return result
        if name == "browser_auth_probe":
            result = browser_auth_probe.invoke({"target_url": target_url})
            update_page_context(result)
            return result
        tool = direct_tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"browser_tool_not_allowed:{name}"}
        try:
            return tool.invoke(args)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def action_feedback(arguments: Dict[str, Any]) -> Dict[str, Any]:
        before = invoke("browser_network_log", {
            "include_body": False,
            "max_items": 100,
        })
        before = before if isinstance(before, dict) else {}
        highest = int(before.get("highest_index", 0) or 0)
        action = str(arguments.get("action") or "").lower()
        if action == "click":
            outcome = invoke("browser_click", {"selector": arguments.get("target", "")})
        elif action == "scroll":
            outcome = invoke("browser_scroll", {
                "direction": arguments.get("direction", "down"),
                "amount": arguments.get("amount", 2),
            })
        elif action == "infinite_scroll":
            outcome = invoke("browser_infinite_scroll", {
                "max_scrolls": arguments.get("max_scrolls", 6),
                "pause_ms": arguments.get("wait_ms", 900),
            })
        elif action == "wait":
            outcome = invoke("browser_wait_dynamic", {
                "timeout_ms": arguments.get("wait_ms", 900),
            })
        elif action == "reload":
            outcome = invoke("browser_reload", {})
        else:
            return {"ok": False, "error": f"unsupported_action:{action}"}
        if int(arguments.get("wait_ms", 0) or 0) > 0 and action not in {"wait", "infinite_scroll"}:
            time.sleep(min(int(arguments.get("wait_ms", 900) or 900), 5000) / 1000)
        network = invoke("browser_network_log", {
            "include_body": True,
            "max_items": 30,
            "max_body_items": 10,
            "max_body_chars": 50_000,
            "after_index": highest,
        })
        snapshot = invoke("browser_snapshot", {"max_text_chars": 8000, "max_links": 60})
        return {
            "ok": not bool(isinstance(outcome, dict) and outcome.get("error")),
            "action": {key: arguments.get(key) for key in (
                "action", "target", "direction", "amount", "max_scrolls", "wait_ms"
            )},
            "action_result": outcome,
            "network_delta": network,
            "snapshot": snapshot,
        }

    last_checkpoint_save_turn = 0
    last_checkpoint_digest = ""

    def checkpoint_evidence(arguments: Dict[str, Any]) -> Dict[str, Any]:
        action = str(arguments.get("action") or "list").strip().lower()
        requested_partition = str(arguments.get("partition") or "").strip()
        try:
            max_chars = max(500, min(int(arguments.get("max_chars", 40_000) or 40_000), 60_000))
        except Exception:
            max_chars = 40_000

        def allowed(item: Dict[str, Any]) -> bool:
            if not requested_partition:
                return True
            return str(item.get("partition") or "legacy") == requested_partition

        if action == "list":
            requests = [
                {
                    "evidence_id": item.get("evidence_id"),
                    "index": item.get("index"),
                    "url": item.get("url") or item.get("observed_url"),
                    "description": str(item.get("description") or "")[:1000],
                    "partition": item.get("partition", "legacy"),
                    "captured_page_url": sanitize_url(str(item.get("captured_page_url") or "")),
                    "auth_epoch": item.get("auth_epoch", 0),
                }
                for item in checkpoint_material.get("requests", [])[-_BROWSER_CHECKPOINT_MAX_REQUESTS:]
                if isinstance(item, dict) and allowed(item)
            ]
            bodies = [
                {
                    "evidence_id": item.get("evidence_id"),
                    "index": item.get("index"),
                    "url": item.get("url"),
                    "body_chars": int(item.get("body_chars") or len(str(item.get("response_body") or ""))),
                    "body_persisted": bool(item.get("body_file")),
                    "response_summary": item.get("response_summary", {}),
                    "partition": item.get("partition", "legacy"),
                    "captured_page_url": sanitize_url(str(item.get("captured_page_url") or "")),
                    "auth_epoch": item.get("auth_epoch", 0),
                }
                for item in checkpoint_material.get("response_bodies", [])[-_BROWSER_CHECKPOINT_MAX_BODIES:]
                if isinstance(item, dict) and allowed(item)
            ]
            return {
                "ok": True,
                "source": "browser_checkpoint",
                "page_reused": bool(page_reused),
                "context_compatible": checkpoint_context_compatible,
                "current_partition": current_page_context.get("partition"),
                "partition_filter": requested_partition or None,
                "highest_index": checkpoint_material.get("highest_index", 0),
                "requests": requests,
                "response_bodies": bodies,
                "completed_actions": checkpoint_material.get("completed_actions", [])[-40:],
                "candidate_summary": recovery_checkpoint.get("candidate_summary", {}),
                "evidence_partitions": recovery_checkpoint.get("evidence_partitions", {}),
                "evidence_ledger": {key: len(values) for key, values in evidence.items()},
            }
        if action == "read_response":
            evidence_id = str(arguments.get("evidence_id") or "").strip()
            if not evidence_id:
                return {"ok": False, "error": "checkpoint_evidence_id_required"}
            for item in checkpoint_material.get("response_bodies", []):
                if not isinstance(item, dict) or str(item.get("evidence_id") or "") != evidence_id:
                    continue
                body, body_source = _read_checkpoint_body(item, max_chars)
                return {
                    "ok": bool(body),
                    "source": "browser_checkpoint",
                    "body_source": body_source,
                    "evidence_id": evidence_id,
                    "index": item.get("index"),
                    "url": item.get("url"),
                    "partition": item.get("partition", "legacy"),
                    "captured_page_url": sanitize_url(str(item.get("captured_page_url") or "")),
                    "auth_epoch": item.get("auth_epoch", 0),
                    "body": body,
                    "body_chars": int(item.get("body_chars") or len(body)),
                    "truncated": int(item.get("body_chars") or len(body)) > max_chars,
                    "response_summary": item.get("response_summary", {}),
                    **({} if body else {"error": "checkpoint_response_body_unavailable"}),
                }
            return {"ok": False, "error": "checkpoint_evidence_not_found", "evidence_id": evidence_id}
        return {"ok": False, "error": f"unsupported_checkpoint_action:{action}"}

    def persist_progress_checkpoint(stop_reason: str = "in_progress", *, force: bool = False) -> None:
        nonlocal last_checkpoint_save_turn, last_checkpoint_digest
        if not force and len(trace) - last_checkpoint_save_turn < 2:
            return
        progress = _build_browser_checkpoint(
            config=config,
            target_url=target_url,
            resolved_session=resolved_session,
            page_reused=page_reused,
            resume_probe=resume_probe,
            evidence=evidence,
            material=checkpoint_material,
            trace=trace,
            candidate=None,
            stop_reason=stop_reason,
            agent_transcript=recovered_agent_transcript,
        )
        digest_payload = dict(progress)
        digest_payload.pop("updated_at", None)
        digest = _fingerprint(digest_payload)
        if digest == last_checkpoint_digest:
            return
        _save_browser_checkpoint(config, target_url, progress)
        last_checkpoint_digest = digest
        last_checkpoint_save_turn = len(trace)

    def save_final_checkpoint(checkpoint: Dict[str, Any]) -> None:
        nonlocal last_checkpoint_digest
        digest_payload = dict(checkpoint)
        digest_payload.pop("updated_at", None)
        digest = _fingerprint(digest_payload)
        if digest == last_checkpoint_digest:
            return
        _save_browser_checkpoint(config, target_url, checkpoint)
        last_checkpoint_digest = digest

    def handle_tool(name: str, arguments: Dict[str, Any]) -> Any:
        nonlocal last_dimension, dimension_streak, evaluate_calls
        tool_requested_counts[name] += 1
        if name == "browser_checkpoint_evidence":
            result = checkpoint_evidence(arguments)
            observed = _collect_evidence(result)
            delta: Dict[str, int] = {}
            for key, values in observed.items():
                before = len(evidence[key])
                evidence[key].update(values)
                delta[key] = len(evidence[key]) - before
            trace.append({
                "turn_tool": len(trace) + 1,
                "tool": name,
                "args": arguments,
                "ok": bool(result.get("ok")),
                "status": "success" if result.get("ok") else "failed",
                "evidence": "reused" if result.get("ok") else "none",
                "evidence_delta": delta,
            })
            tool_counts[name] += 1
            log_event(logger, "agent.tool", level="INFO" if result.get("ok") else "WARNING", status="success" if result.get("ok") else "failed", agent="browser", phase="explore", tool=name, evidence="reused" if result.get("ok") else "none", evidence_delta=delta, step=len(trace), limit=max_tools)
            persist_progress_checkpoint("checkpoint_evidence_read")
            return _bounded_result(result)
        if name == "browser_open" and page_reused:
            tool_blocked_counts[name] += 1
            trace.append({
                "turn_tool": len(trace) + 1,
                "tool": name,
                "args": arguments,
                "ok": False,
                "status": "blocked",
                "evidence": "reused",
                "reason": "browser_context_already_resumed",
            })
            log_event(logger, "agent.tool", level="WARNING", status="blocked", agent="browser", phase="explore", tool="browser_open", evidence="reused", reason="browser_context_already_resumed", step=len(trace), limit=max_tools)
            return {
                "ok": False,
                "error": "browser_context_already_resumed",
                "required_action": "reuse_existing_page_and_checkpoint",
                "do_not_retry_tool": "browser_open",
                "recommended_tools": [
                    "browser_checkpoint_evidence", "browser_network_response_body", "browser_network_log",
                    "browser_action_feedback", "browser_activate_comments",
                ],
                "checkpoint": {
                    "highest_index": checkpoint_material.get("highest_index", 0),
                    "request_indices": [
                        item.get("index") for item in checkpoint_material.get("requests", [])
                        if isinstance(item, dict) and item.get("index") is not None
                    ][-80:],
                    "body_indices": [
                        item.get("index") for item in checkpoint_material.get("response_bodies", [])
                        if isinstance(item, dict) and item.get("index") is not None
                    ][-40:],
                    "unique_item_ids": len(evidence.get("item_ids", set())),
                },
            }
        dimension = (
            "runtime" if name == "browser_evaluate"
            else "network" if name in {"browser_network_log", "browser_network_response_body", "browser_checkpoint_evidence"}
            else "interaction" if name in {
                "browser_action_feedback", "browser_click", "browser_scroll",
                "browser_infinite_scroll", "browser_wait_dynamic", "browser_reload",
            }
            else "dom"
        )
        dimension_streak = dimension_streak + 1 if dimension == last_dimension else 1
        last_dimension = dimension
        signature = f"{name}:{_fingerprint(arguments)}"
        progress_key = _tool_progress_key(name, arguments, target_url)
        argument_attempts[signature] += 1
        if name == "browser_evaluate":
            evaluate_calls += 1
        blocked_reason = ""
        target_items_ready = _target_item_evidence_ready(evidence)
        recovery_convergence = bool(recovery_checkpoint and target_items_ready)
        low_value_after_items = {
            "browser_snapshot", "browser_dom_probe", "browser_evaluate",
            "browser_html", "browser_text", "browser_links",
            "browser_frames", "browser_use_frame",
        }
        if action_no_progress[progress_key] >= 2:
            blocked_reason = "equivalent_action_no_progress"
        elif name == "browser_network_response_body" and progress_key in failed_response_targets:
            blocked_reason = "response_target_marked_unreadable"
        elif target_items_ready and name in low_value_after_items:
            blocked_reason = (
                "recovery_convergence_only"
                if recovery_convergence
                else "target_items_found_converge_to_parser"
            )
        elif target_items_ready and name in {"browser_click", "browser_action_feedback"} and no_evidence_streak[name] >= 2:
            blocked_reason = "pagination_interaction_no_progress"
        if not blocked_reason and argument_attempts[signature] > 2:
            blocked_reason = "equivalent_tool_arguments_exhausted"
        elif not blocked_reason and name == "browser_evaluate" and evaluate_calls > 8:
            blocked_reason = "browser_evaluate_budget_exhausted"
        elif not blocked_reason and name == "browser_evaluate" and dimension_streak > 3:
            blocked_reason = "exploration_dimension_pivot_required"
        elif not blocked_reason and name in {"browser_evaluate", "browser_snapshot", "browser_html"} and no_evidence_streak[name] >= 2:
            blocked_reason = "tool_no_evidence_streak_exhausted"
        if blocked_reason:
            tool_blocked_counts[name] += 1
            trace.append({
                "turn_tool": len(trace) + 1,
                "tool": name,
                "args": arguments,
                "ok": False,
                "status": "blocked",
                "evidence": "none",
                "reason": blocked_reason,
                "partition": current_page_context.get("partition"),
            })
            log_event(logger, "agent.tool", level="WARNING", status="blocked", agent="browser", phase="explore", tool=name, evidence="none", reason=blocked_reason, step=len(trace), limit=max_tools)
            recommended = (
                ["browser_checkpoint_evidence", "browser_activate_comments", "browser_action_feedback", "browser_network_log", "browser_network_response_body"]
                if not evidence["item_ids"]
                else ["browser_checkpoint_evidence", "browser_action_feedback", "browser_network_log", "browser_network_response_body"]
            )
            return {
                "ok": False,
                "error": blocked_reason,
                "required_action": (
                    "read_candidate_response_and_submit"
                    if _target_item_evidence_ready(evidence)
                    else "change_exploration_dimension"
                ),
                "do_not_retry_tool": name,
                "recommended_tools": recommended,
                "allowed_convergence_tools": (
                    ["browser_checkpoint_evidence", "browser_network_response_body", "browser_network_log", "browser_action_feedback", "submit_parser"]
                    if _target_item_evidence_ready(evidence) else recommended
                ),
                "goal": "observe_nonempty_target_items" if not evidence["item_ids"] else "prove_next_cursor_or_terminal_then_submit",
                "evidence_ledger": {
                    "request_states": len(evidence["request_states"]),
                    "response_bodies": len(evidence["response_bodies"]),
                    "unique_item_ids": len(evidence["item_ids"]),
                },
            }
        tool_counts[name] += 1
        if name == "browser_action_feedback":
            result = action_feedback(arguments)
        else:
            result = invoke(name, arguments)
        update_page_context(result)
        _capture_checkpoint_material(result, checkpoint_material, context=current_page_context)
        observed = _collect_evidence(result)
        delta: Dict[str, int] = {}
        for key, values in observed.items():
            before = len(evidence[key])
            evidence[key].update(values)
            delta[key] = len(evidence[key]) - before
        novel = any(delta.values())
        result_failed = bool(isinstance(result, dict) and (result.get("error") or result.get("ok") is False))
        no_evidence_streak[name] = 0 if novel else no_evidence_streak[name] + 1
        if novel:
            action_no_progress[progress_key] = 0
        else:
            action_no_progress[progress_key] += 1
        if name == "browser_network_response_body" and result_failed:
            failed_response_targets.add(progress_key)
        evidence_label = "new" if novel else "none"
        trace.append({
            "turn_tool": len(trace) + 1,
            "tool": name,
            "args": arguments,
            "ok": not result_failed,
            "status": "success" if not result_failed else "failed",
            "evidence": evidence_label,
            "evidence_delta": delta,
            "partition": current_page_context.get("partition"),
            "progress_key": progress_key,
        })
        log_event(logger, "agent.tool", level="INFO" if trace[-1]["ok"] else "WARNING", status="success" if trace[-1]["ok"] else "failed", agent="browser", phase="explore", tool=name, evidence=evidence_label, evidence_delta=delta, step=len(trace), limit=max_tools)
        if novel or name in {"browser_network_response_body", "browser_action_feedback", "browser_network_log"}:
            persist_progress_checkpoint(f"after_{name}", force=novel)
        else:
            persist_progress_checkpoint(f"after_{name}")
        if isinstance(result, dict):
            enriched = dict(result)
            target_items_ready = _target_item_evidence_ready(evidence)
            enriched["_agent_evidence"] = {
                "novel": novel,
                "delta": delta,
                "ledger": {
                    "request_states": len(evidence["request_states"]),
                    "response_bodies": len(evidence["response_bodies"]),
                    "unique_item_ids": len(evidence["item_ids"]),
                    "item_response_families": len(evidence["item_response_families"]),
                    "list_response_families": len(evidence["list_response_families"]),
                    "item_list_shapes": len(evidence["item_list_shapes"]),
                },
                "remaining_tools": max(0, max_tools - len(trace)),
                "phase": "convergence" if target_items_ready else "exploration",
                "required_next_tool": (
                    "browser_network_response_body"
                    if target_items_ready and name == "browser_network_log"
                    else "browser_checkpoint_evidence"
                    if target_items_ready and recovery_checkpoint and name not in {"browser_checkpoint_evidence", "browser_network_response_body"}
                    else None
                ),
                "must_submit_after_missing_pagination_is_identified": bool(target_items_ready),
                "runtime_pagination_fallback_allowed": bool(target_items_ready),
            }
        else:
            enriched = {"ok": True, "result": result, "_agent_evidence": {"novel": novel, "delta": delta}}
        return _bounded_result(enriched)

    recovery_context = {
        "available": bool(recovery_checkpoint),
        "page_reused": bool(page_reused),
        "resume_probe": resume_probe,
        "evidence_counts": {key: len(values) for key, values in evidence.items()},
        "highest_network_index": checkpoint_material.get("highest_index", 0),
        "request_indices": [
            item.get("index") for item in checkpoint_material.get("requests", [])
            if isinstance(item, dict) and item.get("index") is not None
        ][-100:],
        "response_body_evidence": [
            {
                "evidence_id": item.get("evidence_id"),
                "index": item.get("index"),
                "url": item.get("url"),
                "response_summary": item.get("response_summary", {}),
                "body_chars": len(str(item.get("response_body") or "")),
            }
            for item in checkpoint_material.get("response_bodies", [])[-24:]
            if isinstance(item, dict)
        ],
        "completed_actions": checkpoint_material.get("completed_actions", [])[-20:],
        "candidate_summary": recovery_checkpoint.get("candidate_summary", {}),
    }

    def persist_browser_transcript(event: Dict[str, Any]) -> None:
        nonlocal recovered_agent_transcript
        transcript = _safe_agent_transcript(event.get("messages"))
        if not transcript:
            return
        recovered_agent_transcript = transcript
        persist_progress_checkpoint("agent_turn_checkpoint", force=True)

    try:
        pi_result = run_pi_browser_agent(
            system_prompt=BROWSER_AGENT_SYSTEM_PROMPT,
            user_prompt=_full_task_prompt(
                target_url, target_fields, list(rag_hits or []), max_turns, max_tools,
                config.get("failure_feedback") if isinstance(config.get("failure_feedback"), dict) else {},
                recovery_context,
                operation_mode,
                required_action,
            ),
            tool_handler=handle_tool,
            max_turns=max_turns,
            max_tools=max_tools,
            timeout_seconds=timeout_seconds,
            model_name=str(config.get("pi_model_name") or "") or None,
            provider=str(config.get("pi_model_provider") or "") or None,
            base_url=str(config.get("pi_base_url") or "") or None,
            tool_profile="full_browser",
            allowed_tools=(["browser_manual_login", "browser_auth_probe"] if auth_resolution_mode else None),
            operation_mode=operation_mode,
            required_action=required_action,
            agent_name="browser",
            phase_name=phase_name,
            round_num=0,
            initial_messages=recovered_agent_transcript,
            thinking_level=os.getenv("PI_BROWSER_THINKING_LEVEL", "low"),
            checkpoint_handler=persist_browser_transcript,
        )
    except PiRuntimeUnavailable as exc:
        pi_result = {"ok": False, "error": str(exc), "candidate": {}, "tool_calls": []}
    except Exception as exc:
        log_event(logger, "agent.runtime", level="ERROR", status="failed", agent="browser", phase=phase_name, runtime="pi-agent-core", error_type=type(exc).__name__, reason=str(exc), exc_info=True)
        pi_result = {"ok": False, "error": str(exc), "candidate": {}, "tool_calls": []}
    finally:
        persist_progress_checkpoint("agent_session_finished", force=True)

    candidate = pi_result.get("candidate")
    latest_agent_transcript = _safe_agent_transcript(pi_result.get("transcript")) or recovered_agent_transcript
    checkpoint = _build_browser_checkpoint(
        config=config,
        target_url=target_url,
        resolved_session=resolved_session,
        page_reused=page_reused,
        resume_probe=resume_probe,
        evidence=evidence,
        material=checkpoint_material,
        trace=trace,
        candidate=candidate if isinstance(candidate, dict) else {},
        stop_reason=pi_result.get("stop_reason") or pi_result.get("error"),
        agent_transcript=latest_agent_transcript,
    )
    save_final_checkpoint(checkpoint)
    if not isinstance(candidate, dict) or not candidate:
        error = str(pi_result.get("error") or "pi_agent_no_parser_submission")
        log_event(logger, "agent.finish", level="WARNING", status="failed", agent="browser", phase=phase_name, runtime="pi-agent-core", turns=pi_result.get("turns", 0), tools=len(trace), error_type="parser_not_submitted", reason=error[:800])
        return {
            "page_type": "unknown",
            "data_source": "unknown",
            "confidence": 0.0,
            "error": error,
            "_checkpoint": checkpoint,
            "_agent": {
                "runtime": "pi-agent-core",
                "operation_mode": operation_mode,
                "required_action": required_action,
                "full_flow": True,
                "submitted": False,
                "tool_trace": trace,
                "evidence_ledger": {
                    "request_families": sorted(evidence.get("request_families", set()))[:30],
                    "request_states": len(evidence.get("request_states", set())),
                    "response_bodies": len(evidence.get("response_bodies", set())),
                    "unique_item_ids": len(evidence.get("item_ids", set())),
                    "item_response_families": sorted(evidence.get("item_response_families", set()))[:20],
                    "item_record_keys": sorted(evidence.get("item_record_keys", set()))[:100],
                    "item_list_shapes": sorted(evidence.get("item_list_shapes", set()))[:100],
                },
            },
            "_pipeline": {
                "mode": "pi_agent_core",
                "phases": {
                    "login": auth_phase,
                    "agent": {"ok": False, "error": error, "reason": "agent_error"},
                },
                "agent": {"runtime": "pi-agent-core", **pi_result, "tool_trace": trace},
            },
        }

    result = dict(candidate)
    result.setdefault("target_url", target_url)
    result.setdefault("page_type", "unknown")
    result.setdefault("data_source", "unknown")
    try:
        result["confidence"] = max(0.0, min(float(result.get("confidence", 0.75)), 1.0))
    except Exception:
        result["confidence"] = 0.75
    auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
    auth.setdefault("auth_required", result.get("page_type") == "auth_required")
    auth.setdefault("manual_login_required", auth.get("auth_required", False))
    auth["session_name"] = resolved_session
    if auth_phase.get("post_login_probe") and not auth.get("probe"):
        auth["probe"] = auth_phase.get("post_login_probe")
    result["auth"] = auth
    auth_facts = normalize_auth_facts(result, {"phases": {"login": auth_phase}})
    result["auth"] = {**auth, **auth_facts}
    auth_phase.update({
        "authentication_state": auth_facts.get("state"),
        "verification_state": auth_facts.get("verification_state"),
        "authenticated": auth_facts.get("authenticated"),
    })
    result["api_endpoints"] = endpoint_provenance(result)
    result["interaction_plan"] = _merge_interaction_plan(result.get("interaction_plan"), trace)
    exhaustive_required = any(
        re.search(r"评论|回复|comment|reply", str(value), re.I)
        for value in target_fields
    )
    pagination_contract = _normalize_pagination_contract(
        result, evidence, exhaustive_required, target_fields
    )
    result["pagination_contract"] = pagination_contract
    if pagination_contract.get("execution_mode") == "browser_replay":
        for endpoint in result.get("api_endpoints") or []:
            if isinstance(endpoint, dict):
                endpoint["requires_browser_replay"] = True
    evidence_complete = bool(pagination_contract.get("evidence_complete"))
    item_evidence_complete = bool(pagination_contract.get("item_evidence_complete"))
    runtime_pagination_validation = bool(pagination_contract.get("runtime_validation_required"))

    # Evidence normalization is advisory. The Browser Agent owns the semantic
    # judgment and Code Agent may validate missing details by real execution.
    # Python no longer converts fixed evidence patterns into a fatal parser error.
    evidence_warnings = list(result.get("evidence_warnings") or [])
    if result.get("data_source") == "api" and not item_evidence_complete:
        evidence_warnings.append("api_item_evidence_not_confirmed_by_host")
    if result.get("data_source") == "api" and runtime_pagination_validation:
        result["runtime_pagination_validation_required"] = True
        evidence_warnings.append("pagination_requires_runtime_validation")
    elif not evidence_complete:
        evidence_warnings.append("pagination_not_confirmed_by_host")
    if evidence_warnings:
        result["analysis_status"] = result.get("analysis_status") or "ai_review_required"
        result["evidence_warnings"] = list(dict.fromkeys(evidence_warnings))
        warnings = list(result.get("warnings") or [])
        warnings.extend(result["evidence_warnings"])
        result["warnings"] = list(dict.fromkeys(warnings))
    else:
        result.setdefault("analysis_status", "complete")

    # Respect only an error explicitly submitted by the Browser Agent itself.
    # Host-side evidence heuristics are not promoted to errors.
    evidence_error = str(result.get("error") or "").strip()
    actual_tool_calls = len(pi_result.get("tool_calls") or [])
    budget_exhausted = bool(
        pi_result.get("tool_budget_exhausted")
        or actual_tool_calls >= max_tools
    )
    checkpoint = _build_browser_checkpoint(
        config=config,
        target_url=target_url,
        resolved_session=resolved_session,
        page_reused=page_reused,
        resume_probe=resume_probe,
        evidence=evidence,
        material=checkpoint_material,
        trace=trace,
        candidate=result,
        stop_reason=pi_result.get("stop_reason") or evidence_error,
        agent_transcript=latest_agent_transcript,
    )
    save_final_checkpoint(checkpoint)
    result["_checkpoint"] = checkpoint
    result["_agent"] = {
        "runtime": "pi-agent-core",
        "operation_mode": operation_mode,
        "required_action": required_action,
        "full_flow": True,
        "submitted": True,
        "turns": pi_result.get("turns", 0),
        "stop_reason": pi_result.get("stop_reason"),
        "tool_trace": trace,
        "decision_authority": "pi-agent-core-ai",
        "evidence_contract": pagination_contract,
        "budget_exhausted": budget_exhausted,
        "tool_counts": dict(tool_counts),
        "tool_requested_counts": dict(tool_requested_counts),
        "tool_blocked_counts": dict(tool_blocked_counts),
    }
    result["_pipeline"] = {
        "mode": "pi_agent_core",
        "operation_mode": operation_mode,
        "required_action": required_action,
        "phases": {
            "login": auth_phase,
            "agent": {
                "ok": not bool(evidence_error),
                "error": evidence_error or None,
                "reason": "evidence_incomplete" if evidence_error else None,
                "runtime_status": "success",
                "analysis_status": result.get("analysis_status"),
                "authentication_state": auth_facts.get("state"),
            },
        },
        "agent": {
            "runtime": "pi-agent-core",
            "turns": pi_result.get("turns", 0),
            "tool_calls": pi_result.get("tool_calls", []),
            "stop_reason": pi_result.get("stop_reason"),
            "duration_seconds": pi_result.get("duration_seconds"),
            "usage": pi_result.get("usage", {}),
            "tool_trace": trace,
            "evidence_contract": pagination_contract,
            "budget_exhausted": budget_exhausted,
            "tool_counts": dict(tool_counts),
            "tool_requested_counts": dict(tool_requested_counts),
            "tool_blocked_counts": dict(tool_blocked_counts),
        },
    }
    analysis_status = str(result.get("analysis_status") or "ai_review_required")
    unresolved_auth = str(auth_facts.get("state") or "unknown") in {"required", "challenge", "provisional"}
    business_incomplete = bool(evidence_error or unresolved_auth or analysis_status != "complete" or float(result.get("confidence") or 0.0) < 0.5)
    done_status = "incomplete" if business_incomplete else ("success_with_warnings" if budget_exhausted else "success")
    log_event(
        logger, "agent.finish", level="WARNING" if business_incomplete else "INFO",
        status=done_status, agent="browser", phase=phase_name, runtime="pi-agent-core",
        runtime_status="success", analysis_status=analysis_status, artifact_status="parser_submitted",
        authentication_state=auth_facts.get("state"), authenticated=auth_facts.get("authenticated"),
        verification_state=auth_facts.get("verification_state"),
        page_type=result.get("page_type"), data_source=result.get("data_source"),
        confidence=result.get("confidence"), turns=pi_result.get("turns"),
        tool_requests=sum(tool_requested_counts.values()),
        tool_executed=sum(tool_counts.values()),
        tool_blocked=sum(tool_blocked_counts.values()),
        evidence_complete=evidence_complete,
        runtime_pagination_validation=runtime_pagination_validation,
        budget_exhausted=budget_exhausted,
        tool_counts={
            name: {
                "requested": int(tool_requested_counts.get(name, 0)),
                "executed": int(tool_counts.get(name, 0)),
                "blocked": int(tool_blocked_counts.get(name, 0)),
            }
            for name in sorted(set(tool_requested_counts) | set(tool_counts) | set(tool_blocked_counts))
        },
        error_type=("browser_analysis_incomplete" if business_incomplete else None),
        reason=(evidence_error or ("authentication_not_verified" if unresolved_auth else (analysis_status if analysis_status != "complete" else None))),
    )
    return result
