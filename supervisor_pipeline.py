"""Autonomous crawler orchestration driven by a pi-agent-core capability loop.

The Supervisor chooses capabilities from current evidence instead of following a
predetermined graph. Python handlers provide idempotent side effects, security
boundaries, advisory evidence summaries, checkpoints, and minimum artifact facts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse

from dotenv import load_dotenv

from api_logger import get_all_summaries, reset_all_trackers
from logger import get_logger, log_event, set_log_context
from transcript_utils import sanitize_agent_transcript
from common import json_dumps
from runtime_facts import (
    ERROR_CATEGORY_BY_ROOT, classify_runtime_failure, code_entry_mode,
    endpoint_provenance, normalize_auth_facts, progress_delta,
    progress_snapshot, sanitize_url,
)

_pipeline_log = get_logger("pipeline")
_supervisor_log = get_logger("agent.supervisor")
SUPERVISOR_AGENT_BUILD = "2026.07.22-supervisor-native-loop-v13.5-auth-protocol"

def _append_log(
    state: CrawlerState,
    level: str,
    capability: str,
    message: str,
    *,
    status: str = "running",
    **fields: Any,
) -> None:
    entry = {
        "schema_version": 1,
        "timestamp": _now_iso(),
        "level": level,
        "component": "pipeline",
        "event": "pipeline.capability",
        "task_id": state.get("thread_id", "-"),
        "capability": capability,
        "status": status,
        "message": message,
        **fields,
    }
    entries = list(state.get("log_entries", []))
    entries.append(entry)
    state["log_entries"] = entries[-1000:]

    log_event(
        _pipeline_log,
        "pipeline.capability",
        level=level,
        status=status,
        task_id=state.get("thread_id", "-"),
        action=capability,
        message=message,
        **fields,
    )

    _ensure_dirs()
    safe_tid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(state.get("thread_id") or "supervisor"))
    with (LOG_DIR / f"{safe_tid}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json_dumps(entry, indent=None) + "\n")



load_dotenv(override=True)


# =============================================================================
# 状态定义
# =============================================================================

class CrawlerState(TypedDict, total=False):
    user_request: str
    target_url: str
    target_fields: List[str]
    output_format: str
    max_items: Optional[int]
    need_code_return: bool
    thread_id: str

    page_type: str
    data_source: str
    page_metadata: Dict[str, Any]
    parser_result: Dict[str, Any]
    browser_checkpoint: Dict[str, Any]
    browser_pipeline_info: Dict[str, Any]
    parser_confidence: float
    parser_attempts: int
    interaction_plan: List[Dict[str, Any]]
    browser_feedback: Dict[str, Any]

    need_login: str
    auth_status: str
    auth_facts: Dict[str, Any]
    session_name: Optional[str]
    login_url: Optional[str]
    manual_login_required: bool
    login_attempts: int

    generated_code: str
    code_framework: str
    code_version: int
    code_file: str
    code_checkpoint: Dict[str, Any]
    execution_result: Dict[str, Any]
    last_successful_execution_result: Dict[str, Any]
    best_execution_result: Dict[str, Any]
    execution_history: List[Dict[str, Any]]
    artifact_versions: List[Dict[str, Any]]
    pending_recheck_policy: Dict[str, Any]
    accepted_artifact_snapshot: Dict[str, str]
    selection_state: Dict[str, Any]
    failed_code_attempt: Dict[str, Any]
    browser_attempt_result: Dict[str, Any]

    error_info: Dict[str, Any]
    retry_count: int
    web_retry_count: int
    max_retries: int

    rag_hits: List[Dict[str, Any]]
    use_cached_strategy: bool
    final_output: Dict[str, Any]
    fix_exhausted: bool

    rag_checked: bool
    parser_valid: bool
    execution_attempted: bool
    error_category: str
    capability_history: List[str]
    agent_transcript: List[Dict[str, Any]]
    terminal_finalized: bool
    resumable: bool
    log_entries: List[Dict[str, Any]]
    task_started_at: float
    capability_counts: Dict[str, int]
    progress_history: List[Dict[str, Any]]
    no_progress_streaks: Dict[str, int]
    code_entry_mode: str


# =============================================================================
# 常量
# =============================================================================

DEFAULT_MAX_RETRIES = 3
DEFAULT_OUTPUT_FORMAT = "csv"
DEFAULT_PARSER_THRESHOLD = 0.65
LOW_CONFIDENCE_STOP = 0.45

WORKSPACE_ROOT = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace"))
RUNTIME_DIR = WORKSPACE_ROOT / "runtime"
STATE_DIR = RUNTIME_DIR / "states"
LOG_DIR = RUNTIME_DIR / "logs"
OUTPUT_DIR = RUNTIME_DIR / "output"
RAG_DIR = RUNTIME_DIR / "rag"
SCREENSHOT_DIR = Path(os.getenv("BROWSER_SCREENSHOT_DIR", str(WORKSPACE_ROOT / "browser_screenshots")))
AUTH_DIR = Path(os.getenv("BROWSER_AUTH_STATE_DIR", str(WORKSPACE_ROOT / "browser_auth_states")))

LEGACY_ROOT_ALIASES = {
    # Older checkpoints and agents may still use these names. Normalize them at
    # the boundary instead of keeping parallel error taxonomies alive.
    "runtime_error": "crawler_runtime_error",
    "file_write_error": "crawler_runtime_error",
    "code_inspection_failed": "crawler_runtime_error",
    "pi_code_runtime_error": "crawler_runtime_error",
    "unsupported_hash_algorithm": "crawler_runtime_error",
    "auth_state_not_reused": "authentication_unverified",
    "login_required": "authentication_required",
    "session_not_found": "authentication_required",
    "captcha_required": "challenge_required",
    "mfa_required": "challenge_required",
    "http_403": "access_denied",
    "permission_denied": "access_denied",
    "robots_disallowed": "blocked_by_site_policy",
    "selector_not_found": "parser_error",
    "field_missing": "parser_error",
    "json_path_error": "api_contract_error",
    "api_schema_changed": "api_contract_error",
    "crawler_spec_invalid": "api_contract_error",
    "parser_incomplete": "parser_error",
    "browser_agent_no_progress": "parser_error",
    "comment_region_not_activated": "parser_error",
    "pagination_failed": "pagination_error",
    "pagination_incomplete": "pagination_error",
    "duplicate_pages": "pagination_error",
    "infinite_scroll_failed": "pagination_error",
    "pagination_trigger_not_found": "pagination_error",
    "pagination_evidence_incomplete": "pagination_error",
    "api_item_evidence_incomplete": "parser_error",
    "pagination_contract_conflict": "pagination_error",
    "playwright_timeout": "timeout",
    "dynamic_wait_failed": "timeout",
    "tool_budget_exhausted": "tool_budget_exhausted",
    "empty_data": "empty_data",
    "rate_limited": "rate_limited",
    "blocked_by_site_policy": "blocked_by_site_policy",
}


# =============================================================================
# 工具函数
# =============================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    for p in (RUNTIME_DIR, STATE_DIR, LOG_DIR, OUTPUT_DIR, RAG_DIR, SCREENSHOT_DIR, AUTH_DIR):
        p.mkdir(parents=True, exist_ok=True)


def _requires_complete_comment_pagination(state: CrawlerState) -> bool:
    comment_target = any(
        re.search(r"评论|回复|comment|reply", str(field), re.I)
        for field in state.get("target_fields", [])
    )
    # Even an explicit limit such as “前1000条” requires deterministic API
    # pagination until that limit is reached or the endpoint reports its end.
    return bool(comment_target)


# _json_dumps 已移至 common.json_dumps


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _record_capability_progress(
    state: CrawlerState,
    capability: str,
    before: Dict[str, Any],
) -> CrawlerState:
    after = progress_snapshot(state)
    delta = progress_delta(before, after)
    history = list(state.get("progress_history") or [])
    entry = {
        "timestamp": _now_iso(),
        "capability": capability,
        "before": before,
        "after": after,
        **delta,
    }
    history.append(entry)
    streaks = dict(state.get("no_progress_streaks") or {})
    streaks[capability] = int(streaks.get(capability, 0) or 0) + 1 if delta.get("stalled") else 0
    level = "WARNING" if delta.get("stalled") else "INFO"
    log_event(
        _pipeline_log, "capability.progress", level=level,
        status="stalled" if delta.get("stalled") else "success",
        action=capability, progress_score=delta.get("score", 0),
        no_progress_streak=streaks[capability], changes=delta.get("changes", {}),
    )
    return {
        **state,
        "progress_history": history[-30:],
        "no_progress_streaks": streaks,
    }


# _json_extract 已移至 common.safe_json_parse












def _validate_parser_result(parser: Dict[str, Any], state: CrawlerState) -> Dict[str, Any]:
    """Normalize Browser AI facts and expose readiness without imposing a fixed parser template."""
    if not isinstance(parser, dict) or not parser:
        return {
            "valid": False,
            "can_enter_code": bool(state.get("target_url")),
            "full_code_ready": False,
            "code_entry_mode": "probe",
            "need_manual_login": False,
            "issues": ["parser_result 为空"],
            "warnings": ["browser_did_not_submit_a_parser"],
            "decision_authority": "pi-agent-core-ai",
            "validation_mode": "runtime_facts",
        }

    agent_meta = parser.get("_agent") if isinstance(parser.get("_agent"), dict) else {}
    confidence = _safe_float(parser.get("confidence"), 0.5)
    parser_error = str(parser.get("error") or "").strip()
    auth_facts = normalize_auth_facts(
        parser,
        state.get("browser_pipeline_info") if isinstance(state.get("browser_pipeline_info"), dict) else {},
        state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {},
    )
    endpoints = endpoint_provenance(parser)
    parser["api_endpoints"] = endpoints
    mode = code_entry_mode(parser, auth_facts)
    has_api = bool(endpoints)
    verified_api = any(item.get("verified") for item in endpoints)
    has_fields = bool(parser.get("fields") or parser.get("selectors"))
    has_strategy = bool(
        verified_api or has_fields or parser.get("interaction_plan")
        or str(parser.get("data_source") or "").lower() not in {"", "unknown", "none"}
    )
    warnings: List[str] = []
    if not agent_meta.get("submitted"):
        warnings.append("browser_agent_did_not_use_submit_parser")
    runtime = str(agent_meta.get("runtime") or "").strip().lower().replace("_", "-")
    if runtime and runtime != "pi-agent-core":
        warnings.append("unexpected_browser_runtime")
    if parser_error:
        warnings.append(f"browser_agent_reported:{parser_error[:300]}")
    warnings.extend(str(value) for value in (parser.get("evidence_warnings") or []))
    if not has_strategy:
        warnings.append("parser_has_no_verified_strategy")
    if auth_facts.get("state") in {"required", "challenge", "provisional"}:
        warnings.append(f"authentication_{auth_facts.get('state')}")
    if mode == "probe":
        warnings.append("code_probe_required")

    return {
        "valid": bool(has_strategy and auth_facts.get("state") not in {"required", "challenge"}),
        "can_enter_code": bool(state.get("target_url")),
        "full_code_ready": mode == "full",
        "code_entry_mode": mode,
        "issues": list(dict.fromkeys(warnings)),
        "warnings": list(dict.fromkeys(warnings)),
        "confidence": confidence,
        "has_api": has_api,
        "verified_api": verified_api,
        "api_candidates": len(endpoints),
        "endpoint_provenance": [
            {"url": item.get("url"), "source": item.get("source"), "verified": item.get("verified")}
            for item in endpoints[:8]
        ],
        "has_fields": has_fields,
        "need_manual_login": auth_facts.get("state") in {"required", "challenge", "provisional"},
        "auth_facts": auth_facts,
        "runtime_pagination_validation": bool(
            ((parser.get("pagination_contract") or {}).get("runtime_validation_required"))
            if isinstance(parser.get("pagination_contract"), dict) else False
        ),
        "decision_authority": "pi-agent-core-ai",
        "validation_mode": "runtime_facts",
    }


def _normalize_root_error(error_type: str) -> str:
    value = str(error_type or "").strip()
    return LEGACY_ROOT_ALIASES.get(value, value)


def _classify_error(error_type: str) -> str:
    root = _normalize_root_error(error_type)
    return ERROR_CATEGORY_BY_ROOT.get(root, "unknown")


# =============================================================================
# RAG 工具
# =============================================================================

def _search_rag(target_url: str, target_fields: List[str]) -> List[Dict[str, Any]]:
    _ensure_dirs()
    rag_file = RAG_DIR / "crawler_rag.jsonl"
    if not rag_file.exists():
        return []

    target_domain = _domain(target_url)
    target_set = {str(f).lower() for f in (target_fields or [])}
    records: List[Dict[str, Any]] = []

    for line in rag_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue

    scored = []
    for r in records[-200:]:
        score = 0.0
        r_url = r.get("url", "")
        r_domain = r.get("domain", _domain(r_url))
        if target_domain and target_domain == r_domain:
            score += 0.45
        if target_url and r_url and (target_url in r_url or r_url in target_url):
            score += 0.25
        r_fields = set(str(f).lower() for f in (r.get("target_fields") or r.get("fields") or []))
        if target_set and r_fields:
            overlap = len(target_set & r_fields)
            score += 0.20 * (overlap / max(len(target_set | r_fields), 1))
        if r.get("last_success_at") or r.get("success_count"):
            score += 0.10
        if score > 0:
            r["rag_score"] = round(score, 4)
            scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:5]]


def _save_rag(state: CrawlerState) -> None:
    parser = state.get("parser_result") or {}
    execution = state.get("execution_result") or {}
    record = {
        "url": state.get("target_url", ""),
        "domain": _domain(state.get("target_url", "")),
        "page_type": parser.get("page_type") or state.get("page_type"),
        "data_source": parser.get("data_source") or state.get("data_source"),
        "target_fields": state.get("target_fields", []),
        "selectors": parser.get("selectors", {}),
        "api_endpoints": parser.get("api_endpoints", []),
        "pagination": parser.get("pagination", {}),
        "interaction_plan": parser.get("interaction_plan", []),
        "code_framework": state.get("code_framework", ""),
        "success": bool(execution.get("success") and _safe_int(execution.get("items_count"), 0) > 0),
        "success_count": _safe_int(execution.get("items_count"), 0),
        "last_success_at": _now_iso() if execution.get("success") else None,
        "confidence": _safe_float(parser.get("confidence"), 0.0),
        "created_at": _now_iso(),
    }
    _ensure_dirs()
    rag_file = RAG_DIR / "crawler_rag.jsonl"
    with rag_file.open("a", encoding="utf-8") as f:
        f.write(json_dumps(record, indent=None) + "\n")


# =============================================================================
# 能力实现：任务规范
# =============================================================================

def _validate_url(url: Any) -> Optional[str]:
    """校验并清洗 URL，返回合法 URL 或 None。"""
    if not url or not isinstance(url, str):
        return None
    # 去除首尾空白、反引号、引号、逗号等
    url = url.strip().strip("`'\"")
    # 去除末尾的中文和英文标点
    url = re.sub(r'[，。、；：！？,.;:!?]+$', '', url)
    # 去除 URL 内部可能残留的反引号
    url = url.replace('`', '')
    # 必须以 http:// 或 https:// 开头
    if not re.match(r'^https?://', url, re.I):
        return None
    # netloc 必须像域名（含点，且只含 ASCII 域名字符）
    parsed = urlparse(url)
    netloc = parsed.netloc
    if not netloc or "." not in netloc:
        return None
    # netloc 不能包含中文
    if re.search(r'[\u4e00-\u9fff]', netloc):
        return None
    # 不能出现两个 "://"（说明被错误拼接过）
    if url.count("://") > 1:
        return None
    return url


def parse_request(
    state: CrawlerState,
    parsed_request: Optional[Dict[str, Any]] = None,
) -> CrawlerState:
    user_request = state.get("user_request", "")
    _ensure_dirs()
    _append_log(state, "INFO", "parse_request", "开始解析用户需求", status="started")

    parsed = parsed_request if isinstance(parsed_request, dict) else state.get("_normalized_request")
    if not isinstance(parsed, dict):
        raise ValueError("parse_request requires normalized request from pi-agent-core")
    url = _validate_url(parsed.get("target_url"))
    if not url:
        _append_log(state, "WARNING", "parse_request", "URL 校验失败或为空", status="advisory", target_url=parsed.get("target_url"))
    target_fields = parsed.get("target_fields", []) or []
    if not isinstance(target_fields, list):
        target_fields = []
    target_fields = [str(field).strip() for field in target_fields if str(field).strip()][:30]
    if re.search(r"评论|comment", user_request, re.I) and not re.search(
        r"评论内容|评论用户|用户名|评论时间|点赞数|回复数|用户等级|content|user|time|like|reply|level",
        user_request,
        re.I,
    ):
        target_fields = ["评论内容", "评论用户", "评论时间", "点赞数"]
    max_items = parsed.get("max_items")
    output_format = parsed.get("output_format", "csv") or "csv"
    output_format = str(output_format).lower()
    if output_format not in {"csv", "json", "xlsx"}:
        output_format = DEFAULT_OUTPUT_FORMAT
    max_items = _safe_int(max_items)
    if max_items is not None and max_items <= 0:
        max_items = None
    need_login = parsed.get("need_login", "unknown") or "unknown"
    need_code_return = bool(parsed.get("need_code_return", False))

    has_url = url is not None

    result: CrawlerState = {
        **state,
        "target_url": url or "",
        "target_fields": target_fields,
        "output_format": output_format,
        "max_items": max_items,
        "need_code_return": need_code_return,
        "need_login": need_login,
        "retry_count": state.get("retry_count", 0),
        "web_retry_count": state.get("web_retry_count", 0),
        "max_retries": state.get("max_retries", DEFAULT_MAX_RETRIES),
        "parser_attempts": state.get("parser_attempts", 0),
        "login_attempts": state.get("login_attempts", 0),
        "code_version": state.get("code_version", 0),
        "parser_result": state.get("parser_result", {}),
        "execution_result": state.get("execution_result", {}),
        "error_info": state.get("error_info", {}),
        "rag_hits": state.get("rag_hits", []),
        "interaction_plan": state.get("interaction_plan", []),
        "browser_feedback": state.get("browser_feedback", {}),
        "auth_status": state.get("auth_status", "unknown"),
        "session_name": state.get("session_name"),
    }

    _append_log(result, "INFO", "parse_request", "需求解析完成", status="success", target_url=url, fields=target_fields, output_format=output_format, max_items=max_items, has_url=has_url)

    return result




# =============================================================================
# 能力实现：策略检索
# =============================================================================

def rag_check(state: CrawlerState) -> CrawlerState:
    target_url = state.get("target_url", "")
    target_domain = _domain(target_url)
    target_fields = state.get("target_fields", [])
    _append_log(state, "INFO", "rag_check", "开始查询历史策略", status="started", domain=target_domain)

    hits = _search_rag(target_url, target_fields)

    same_domain_hits = [h for h in hits if h.get("domain") == target_domain] if target_domain else []
    cross_domain_hits = [h for h in hits if h.get("domain") != target_domain]

    if same_domain_hits:
        best_score = same_domain_hits[0]["rag_score"]
        _append_log(state, "INFO", "rag_check", "命中同域历史策略", status="success", hits=len(same_domain_hits), best_score=best_score, domain=target_domain)
        hits = same_domain_hits
    elif cross_domain_hits and cross_domain_hits[0]["rag_score"] >= 0.55:
        _append_log(state, "INFO", "rag_check", "仅命中跨域历史策略", status="advisory", hits=len(cross_domain_hits), domain=target_domain)
        hits = cross_domain_hits[:1]
    else:
        hits = []

    best_score = hits[0]["rag_score"] if hits else 0.0

    if best_score >= 0.85 and hits and hits[0].get("page_type"):
        _append_log(state, "INFO", "rag_check", "历史策略可供 Browser 参考", status="success", best_score=best_score, confidence="high")
    elif hits:
        _append_log(state, "INFO", "rag_check", "历史策略可选参考", status="success", hits=len(hits), best_score=best_score)
    else:
        _append_log(state, "INFO", "rag_check", "未命中历史策略", status="skipped", hits=0)

    return {
        **state,
        "rag_hits": hits,
        "use_cached_strategy": best_score >= 0.85,
        "rag_checked": True,
    }




# =============================================================================
# 能力实现：Browser Agent
# =============================================================================

def run_browser_capability(state: CrawlerState) -> CrawlerState:
    from browser_pipeline import run_browser_pipeline

    before_progress = progress_snapshot(state)
    _append_log(state, "INFO", "web_parser", "启动 Browser Agent", status="started", invocation=int(state.get("parser_attempts", 0) or 0) + 1)

    target_url = state.get("target_url", "")
    target_fields = state.get("target_fields", [])
    stale_auth_state = state.get("auth_status") == "stale"
    # A quarantined session must not be reintroduced through the explicit
    # session_name argument on the next Supervisor retry.
    session_name = None if stale_auth_state else state.get("session_name")
    parser_attempts = state.get("parser_attempts", 0)
    previous_error = state.get("error_info") if isinstance(state.get("error_info"), dict) else {}
    previous_parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    failure_feedback = dict(state.get("browser_feedback") or {})
    browser_checkpoint = (
        state.get("browser_checkpoint")
        or previous_parser.get("_checkpoint")
        or {}
    )
    if parser_attempts and isinstance(browser_checkpoint, dict) and browser_checkpoint:
        material = browser_checkpoint.get("material") if isinstance(browser_checkpoint.get("material"), dict) else {}
        evidence = browser_checkpoint.get("evidence") if isinstance(browser_checkpoint.get("evidence"), dict) else {}
        _append_log(state, "INFO", "web_parser", "恢复 Browser checkpoint", status="resumed", requests=len(material.get("requests") or []), response_bodies=len(material.get("response_bodies") or []), item_ids=len(evidence.get("item_ids") or []))
    if previous_error.get("error_type"):
        failure_feedback.setdefault("error_type", previous_error.get("error_type"))
        failure_feedback.setdefault("error_message", previous_error.get("error_message"))
    previous_contract = previous_parser.get("pagination_contract")
    if isinstance(previous_contract, dict):
        failure_feedback.setdefault("previous_pagination_contract", previous_contract)
    previous_agent = previous_parser.get("_agent") if isinstance(previous_parser.get("_agent"), dict) else {}
    previous_ledger = previous_agent.get("evidence_ledger") if isinstance(previous_agent.get("evidence_ledger"), dict) else {}
    if previous_ledger:
        failure_feedback.setdefault("previous_evidence_ledger", previous_ledger)

    try:
        parser_result = run_browser_pipeline(
            target_url=target_url,
            target_fields=target_fields,
            session_name=session_name,
            session_confirmed=bool(
                session_name and str((state.get("auth_facts") or {}).get("state") or state.get("auth_status") or "") == "verified"
            ),
            rag_hits=state.get("rag_hits", []),
            pipeline_config={
                "task_id": state.get("task_id"),
                "prior_parser_result": state.get("parser_result", {})
                if parser_attempts else {},
                "browser_checkpoint": browser_checkpoint,
                "skip_saved_auth_state": stale_auth_state,
                "failure_feedback": failure_feedback,
                "operation_mode": str(state.get("browser_operation_mode") or "explore"),
                "required_action": state.get("browser_required_action"),
            },
        )

        pipeline_info = parser_result.pop("_pipeline", {})
        auth = parser_result.get("auth") if isinstance(parser_result.get("auth"), dict) else {}
        phases = pipeline_info.get("phases", {})
        agent_phase = phases.get("agent", {}) if isinstance(phases, dict) else {}
        pipeline_error = str(
            (agent_phase.get("error") if isinstance(agent_phase, dict) else None)
            or parser_result.get("error")
            or ""
        ).strip()
        agent_reason = agent_phase.get("reason") if isinstance(agent_phase, dict) else None

        phase_statuses = {
            key: (
                "skipped" if isinstance(value, dict) and value.get("skipped")
                else "completed" if isinstance(value, dict) and value.get("ok")
                else "failed"
            )
            for key, value in phases.items()
        }
        auth_facts = normalize_auth_facts(
            parser_result, pipeline_info,
            state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {},
        )
        _append_log(
            state, "INFO" if not pipeline_error else "WARNING", "web_parser",
            "Browser Agent 执行完成",
            status=("incomplete" if pipeline_error else ("success" if parser_result.get("analysis_status") == "complete" else "success_with_warnings")),
            page_type=parser_result.get("page_type"), data_source=parser_result.get("data_source"),
            confidence=parser_result.get("confidence"), mode=pipeline_info.get("mode", "unknown"),
            phase_statuses=phase_statuses, authentication_state=auth_facts.get("state"),
            authenticated=auth_facts.get("authenticated"), verification_state=auth_facts.get("verification_state"),
            reason=agent_reason or None, error_type=parser_result.get("error") if pipeline_error else None,
        )

        auth = parser_result.get("auth") or {}
        resolved_session = auth.get("session_name") if isinstance(auth, dict) else None
        resolved_session = resolved_session or session_name
        auth_required = auth_facts.get("state") in {"required", "challenge"}
        if auth_required:
            resolved_session = None
        login_phase = phases.get("login", {}) if isinstance(phases, dict) else {}
        login_attempted = bool(isinstance(login_phase, dict) and login_phase.get("attempted"))
        auth_state_quarantined = bool(
            isinstance(login_phase, dict) and login_phase.get("state_quarantined")
        )
        auth_state_rejected = bool(
            isinstance(login_phase, dict) and login_phase.get("state_rejected")
        )
        if auth_state_quarantined:
            resolved_session = None
            recovery = login_phase.get("recovery") if isinstance(login_phase, dict) else {}
            recovery = recovery if isinstance(recovery, dict) else {}
            _append_log(state, "WARNING", "web_parser", "保存的登录态已隔离", status="degraded", reason=recovery.get("reason") or "no_target_evidence", rejected=auth_state_rejected, saved_score=recovery.get("saved_score"), anonymous_score=recovery.get("anonymous_score"))
        error_info = dict(state.get("error_info") or {})
        if pipeline_error:
            pagination_errors = {
                "pagination_trigger_not_found",
                "pagination_evidence_incomplete",
            }
            parser_errors = {
                "browser_agent_no_progress",
                "llm_repeated_empty_response",
                "api_item_evidence_incomplete",
            }
            if pipeline_error in pagination_errors:
                fallback_root = "pagination_error"
            elif pipeline_error in parser_errors:
                fallback_root = "parser_error"
            else:
                fallback_root = "crawler_runtime_error"
            failure = classify_runtime_failure(
                [pipeline_error, agent_reason, parser_result.get("error"), parser_result.get("message")],
                fallback_root=fallback_root,
                terminal=pipeline_error,
            )
            root = str(failure.get("root_error_type") or fallback_root)
            category = str(failure.get("error_category") or _classify_error(root))
            if auth_state_quarantined and category in {"parser", "unknown"}:
                root = "authentication_unverified"
                category = "authentication"
                failure.update({
                    "root_error_type": root,
                    "error_category": category,
                    "retry_strategy": "resolve_authentication",
                })
            error_info = {
                "error_type": root,
                **failure,
                "error_message": pipeline_error,
                "suggested_fix": {
                    "authentication": "先验证登录/风控状态，再决定是否继续 Browser 或运行有界探针。",
                    "access": "停止重复页面探索，保留访问限制事实并评估访问上下文。",
                    "service": "停止密集重试；稍后执行一次有界服务探针。",
                    "network": "执行一次有界网络探针，避免重复打开相同页面。",
                    "parser": "只在能产生新目标页或响应体证据时继续 Browser。",
                    "code": "检查 Browser 运行时异常和 MCP 工具日志。",
                }.get(category, "检查 Browser 运行事实和错误分类。"),
                "auth_state_quarantined": auth_state_quarantined,
                "auth_state_rejected": auth_state_rejected,
            }
        elif _classify_error(str(error_info.get("root_error_type") or error_info.get("error_type") or "")) in {"parser", "authentication"}:
            # A complete Browser result supersedes stale parser/auth failures.
            if parser_result.get("analysis_status") == "complete" and auth_facts.get("state") not in {"required", "challenge", "provisional"}:
                error_info = {}

        next_state: CrawlerState = {
            **state,
            "parser_result": parser_result,
            "browser_checkpoint": parser_result.get("_checkpoint", state.get("browser_checkpoint", {})),
            "browser_pipeline_info": pipeline_info,
            "page_type": parser_result.get("page_type", "unknown"),
            "data_source": parser_result.get("data_source", "unknown"),
            "page_metadata": parser_result.get("page_metadata", {}),
            "parser_confidence": _safe_float(parser_result.get("confidence"), 0.3),
            "interaction_plan": parser_result.get("interaction_plan", []),
            "session_name": resolved_session,
            "auth_facts": auth_facts,
            "auth_status": str(auth_facts.get("state") or "unknown"),
            "need_login": "yes" if auth_facts.get("state") in {"required", "challenge", "provisional"} else state.get("need_login", "unknown"),
            "manual_login_required": auth_facts.get("state") in {"required", "challenge", "provisional"},
            "login_url": auth.get("login_url") if isinstance(auth, dict) else None,
            "login_attempts": state.get("login_attempts", 0) + int(login_attempted),
            "error_info": error_info,
            "parser_attempts": parser_attempts + 1,
            "browser_feedback": {},
        }
        return _record_capability_progress(next_state, "browser", before_progress)

    except Exception as e:
        _append_log(state, "ERROR", "web_parser", "Browser Agent 执行异常", status="failed", error_type=type(e).__name__, reason=str(e))
        failed_state: CrawlerState = {
            **state,
            "parser_result": {"error": str(e), "page_type": "unknown", "confidence": 0.0},
            "browser_checkpoint": state.get("browser_checkpoint", {}),
            "browser_pipeline_info": {
                "mode": "pi-agent-core",
                "phases": {"agent": {"ok": False, "error": str(e)}},
            },
            "parser_confidence": 0.0,
            "parser_attempts": parser_attempts + 1,
            "error_info": {
                "error_type": "crawler_runtime_error",
                **classify_runtime_failure(
                    [type(e).__name__, str(e)],
                    fallback_root="crawler_runtime_error",
                    terminal="browser_pipeline_exception",
                ),
                "error_message": str(e),
                "suggested_fix": "检查 Browser Pipeline traceback 与 MCP 运行状态。",
            },
        }
        return _record_capability_progress(failed_state, "browser", before_progress)


# =============================================================================
# Browser 结果验收
# =============================================================================

def validate_parser(state: CrawlerState) -> CrawlerState:
    parser = state.get("parser_result") or {}
    validation = _validate_parser_result(parser, state)
    warning_codes = list(dict.fromkeys(str(v) for v in (validation.get("warnings") or []) if v))
    _append_log(
        state,
        "INFO" if validation.get("full_code_ready") else "WARNING",
        "validate_parser",
        "Browser AI 事实已记录",
        status="accepted" if validation.get("full_code_ready") else "advisory",
        usable=validation.get("can_enter_code"),
        full_code_ready=validation.get("full_code_ready"),
        code_entry_mode=validation.get("code_entry_mode"),
        authentication_state=(validation.get("auth_facts") or {}).get("state"),
        warning_codes=warning_codes,
        warnings_count=len(warning_codes),
        confidence=validation.get("confidence"),
    )
    parser["_validation"] = validation
    parser["_interaction_check"] = {
        "valid": True,
        "decision_authority": "pi-agent-core-ai",
        "mode": "runtime_facts",
    }
    auth_facts = validation.get("auth_facts") if isinstance(validation.get("auth_facts"), dict) else {}
    error_info = dict(state.get("error_info") or {})
    auth_state = str(auth_facts.get("state") or "unknown")
    if auth_state in {"required", "challenge", "provisional"}:
        root = "challenge_required" if auth_state == "challenge" else (
            "authentication_unverified" if auth_state == "provisional" else "authentication_required"
        )
        error_info = {
            "error_type": root,
            "root_error_type": root,
            "terminal_error_type": "parser_unavailable",
            "error_category": "authentication",
            "error_message": "Browser AI 仍认为认证或风控状态未解决。",
            "suggested_fix": "优先完成认证验证，或仅运行有界访问探针；不要直接启动完整爬虫开发。",
            "retry_strategy": "resolve_authentication",
        }
    elif validation.get("full_code_ready"):
        if _classify_error(str(error_info.get("root_error_type") or error_info.get("error_type") or "")) in {"parser", "authentication"}:
            error_info = {}
    elif not validation.get("can_enter_code"):
        error_info = {
            "error_type": "parser_unavailable",
            "root_error_type": "parser_error",
            "terminal_error_type": "parser_unavailable",
            "error_category": "parser",
            "error_message": "Browser Agent 没有返回完整方案；Code 只能进入有界探针模式。",
            "suggested_fix": "运行访问/接口探针，或让 Browser 收集新的目标页证据。",
            "retry_strategy": "collect_new_evidence",
        }
    return {
        **state,
        "parser_result": parser,
        "auth_facts": auth_facts,
        "auth_status": auth_state,
        "code_entry_mode": str(validation.get("code_entry_mode") or "probe"),
        "error_info": error_info,
        "error_category": str(error_info.get("error_category") or ("none" if not error_info else _classify_error(str(error_info.get("root_error_type") or error_info.get("error_type") or "")))),
        "parser_valid": bool(validation.get("full_code_ready")),
    }


# =============================================================================
# 能力实现：Code Agent
# =============================================================================


def _merge_probe_endpoints(parser: Dict[str, Any], probe_result: Dict[str, Any]) -> Dict[str, Any]:
    """Promote only endpoints observed by a bounded runtime probe."""
    existing = endpoint_provenance(parser)
    merged: List[Dict[str, Any]] = [dict(item) for item in existing]
    seen = {str(item.get("url") or "") for item in merged}
    for raw in probe_result.get("observed_endpoints") or []:
        if isinstance(raw, str):
            item = {"url": raw}
        elif isinstance(raw, dict):
            item = dict(raw)
        else:
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        item.update({"source": "observed", "verified": True, "evidence_source": "bounded_access_probe"})
        merged.append(item)
        seen.add(url)
    result = {**parser, "api_endpoints": merged}
    # Probe evidence changes readiness. Never carry the validation computed
    # before the probe into the next Supervisor decision.
    result.pop("_validation", None)
    return result


def run_code_capability(state: CrawlerState) -> CrawlerState:
    from code_pipeline import run_code_pipeline

    validation = _current_parser_validation(state)
    entry_mode = str(state.get("code_entry_mode") or validation.get("code_entry_mode") or "probe")
    invocation = int(state.get("code_version", 0) or 0) + 1
    _append_log(
        state, "INFO", "code_generator", "启动 Code Agent",
        status="started", invocation=invocation, execution_mode=entry_mode,
    )

    parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    existing_code_checkpoint = state.get("code_checkpoint") if isinstance(state.get("code_checkpoint"), dict) else {}
    if existing_code_checkpoint and entry_mode == "full":
        _append_log(
            state, "INFO", "code_generator", "恢复 Code checkpoint", status="resumed",
            code_sha256=str((existing_code_checkpoint.get("code_file") or {}).get("sha256") or "")[:12],
            root_error_type=(existing_code_checkpoint.get("execution") or {}).get("root_error_type"),
            terminal_error_type=(existing_code_checkpoint.get("execution") or {}).get("terminal_error_type"),
            repair_attempt=state.get("code_version", 0),
        )

    task_state = {**state, "code_entry_mode": entry_mode, "auth_facts": validation.get("auth_facts") or state.get("auth_facts", {})}
    try:
        pipeline_result = run_code_pipeline(parser_result=parser, state=task_state)
        pipeline_info = pipeline_result.pop("_pipeline", {})
        code_checkpoint = (
            pipeline_result.get("_checkpoint")
            if isinstance(pipeline_result.get("_checkpoint"), dict)
            else state.get("code_checkpoint", {})
        )
        phases = pipeline_info.get("phases", {}) if isinstance(pipeline_info, dict) else {}
        fix_info = phases.get("fix", {}) if isinstance(phases.get("fix"), dict) else {}
        fix_exhausted = bool(fix_info.get("exhausted"))
        if "exhausted" not in fix_info:
            fix_exhausted = bool((fix_info.get("attempt") or 0) >= (fix_info.get("max_attempts") or 2))

        probe_completed = bool(pipeline_result.get("probe_completed"))
        root_error = str(
            pipeline_result.get("root_error_type")
            or pipeline_result.get("error_type")
            or ("none" if pipeline_result.get("success") else "crawler_runtime_error")
        )
        terminal_error = str(pipeline_result.get("terminal_error_type") or pipeline_result.get("error_type") or "") or None
        error_category = str(pipeline_result.get("error_category") or ("none" if pipeline_result.get("success") else _classify_error(root_error)))
        retry_strategy = str(pipeline_result.get("retry_strategy") or "") or None
        status = "completed" if probe_completed else ("success" if pipeline_result.get("success") else "failed")
        _append_log(
            state,
            "INFO" if (pipeline_result.get("success") or probe_completed) else "WARNING",
            "code_generator", "Code Agent 执行完成", status=status,
            items=pipeline_result.get("items_count"), root_error_type=root_error,
            terminal_error_type=terminal_error, error_category=error_category,
            retry_strategy=retry_strategy, probe_completed=probe_completed,
            fix_exhausted=fix_exhausted, invocation=invocation,
            repair_attempt=(phases.get("fix") or {}).get("attempt", 0),
            warning_codes=list(dict.fromkeys(str(v) for v in (pipeline_result.get("warning_codes") or []) if v)),
        )

        probe_result = pipeline_result.get("probe_result") if isinstance(pipeline_result.get("probe_result"), dict) else {}
        execution_result = {
            "success": bool(pipeline_result.get("success")),
            "status": status,
            "data_file": pipeline_result.get("data_file"),
            "code_file": pipeline_result.get("code_file"),
            "items_count": pipeline_result.get("items_count", 0),
            "observed_items_count": pipeline_result.get("items_count", 0),
            "fields": pipeline_result.get("fields", []),
            "pagination_complete": pipeline_result.get("pagination_complete"),
            "crawl_meta": pipeline_result.get("crawl_meta", {}),
            "output_quality": pipeline_result.get("output_quality", {}),
            "ai_review": pipeline_result.get("ai_review", {}),
            "validation_mode": pipeline_result.get("validation_mode", "ai_self_review"),
            "advisory_warnings": pipeline_result.get("advisory_warnings", []),
            "warning_codes": pipeline_result.get("warning_codes", []),
            "crawl_progress": pipeline_result.get("crawl_progress", {}),
            "pagination_violations": pipeline_result.get("pagination_violations", []),
            "elapsed_seconds": pipeline_result.get("elapsed_seconds"),
            "timed_out": pipeline_result.get("timed_out", False),
            "stdout_tail": pipeline_result.get("stdout_tail", ""),
            "stderr_tail": pipeline_result.get("stderr_tail", ""),
            "debug_file": pipeline_result.get("debug_file", ""),
            "debug_report": pipeline_result.get("debug_report", {}),
            "internal_error_type": pipeline_result.get("internal_error_type"),
            "repair_status": pipeline_result.get("repair_status"),
            "repair_attempts": int((phases.get("fix") or {}).get("attempt", 0) or 0),
            "invocation": invocation,
            "runtime_status": pipeline_result.get("runtime_status") or ("success" if pipeline_result.get("success") else "failed"),
            "artifact_status": pipeline_result.get("artifact_status") or ("success" if pipeline_result.get("success") else "failed"),
            "review_status": pipeline_result.get("review_status") or ("accepted" if pipeline_result.get("success") else "rejected"),
            "terminal_reason": pipeline_result.get("terminal_reason"),
            "root_error_type": root_error if root_error != "none" else None,
            "terminal_error_type": terminal_error,
            "error_category": error_category,
            "retry_strategy": retry_strategy,
            "probe_completed": probe_completed,
            "probe_result": probe_result,
            "code_checkpoint_snapshot": code_checkpoint,
            "log_file": None,
        }

        next_parser = parser
        next_mode = entry_mode
        next_error: Dict[str, Any] = {}
        if probe_completed:
            target_observed = bool(probe_result.get("target_data_observed"))
            if target_observed:
                next_parser = _merge_probe_endpoints(parser, probe_result)
                next_parser["data_source"] = "api"
                next_mode = "full"
                next_error = {}
            else:
                next_error = {
                    "error_type": root_error,
                    "root_error_type": root_error,
                    "terminal_error_type": terminal_error or "probe_only",
                    "error_category": error_category,
                    "error_message": str(pipeline_result.get("error_message") or probe_result.get("evidence") or "访问探针没有观察到目标数据。")[:1600],
                    "suggested_fix": str(pipeline_result.get("suggested_fix") or retry_strategy or "根据探针事实决定认证、停止或收集新证据。")[:1000],
                    "retry_strategy": retry_strategy,
                    "probe_result": probe_result,
                }
        elif not pipeline_result.get("success"):
            next_error = {
                "error_type": root_error,
                "root_error_type": root_error,
                "terminal_error_type": terminal_error,
                "error_category": error_category,
                "error_message": str(pipeline_result.get("error_message") or "Code Agent 执行未完成。")[:1600],
                "suggested_fix": str(pipeline_result.get("suggested_fix") or retry_strategy or "检查运行事实。")[:1000],
                "retry_strategy": retry_strategy,
                "fix_exhausted": fix_exhausted,
                "internal_error_type": pipeline_result.get("internal_error_type"),
                "debug_file": pipeline_result.get("debug_file", ""),
            }

        return {
            **state,
            "parser_result": next_parser,
            "code_entry_mode": next_mode,
            "parser_valid": bool(next_mode == "full"),
            "generated_code": pipeline_result.get("generated_code", ""),
            "code_framework": pipeline_result.get("framework", "unknown"),
            "code_version": invocation,
            "code_file": pipeline_result.get("code_file") or "",
            "code_checkpoint": code_checkpoint,
            "execution_result": execution_result,
            "error_info": next_error,
            "error_category": str(next_error.get("error_category") or "none"),
            "fix_exhausted": fix_exhausted,
            "code_probe_runs": int(state.get("code_probe_runs", 0) or 0) + int(probe_completed),
        }
    except Exception as exc:
        facts = classify_runtime_failure([exc], fallback_root="crawler_runtime_error", terminal="code_pipeline_exception")
        _append_log(
            state, "ERROR", "code_generator", "Code Agent 执行异常", status="failed",
            root_error_type=facts.get("root_error_type"), terminal_error_type=facts.get("terminal_error_type"),
            error_category=facts.get("error_category"), retry_strategy=facts.get("retry_strategy"),
            reason=str(exc),
        )
        return {
            **state,
            "code_version": invocation,
            "execution_result": {
                "success": False, "items_count": 0, "root_error_type": facts.get("root_error_type"),
                "terminal_error_type": facts.get("terminal_error_type"), "error_category": facts.get("error_category"),
                "retry_strategy": facts.get("retry_strategy"), "runtime_status": "failed", "artifact_status": "failed",
                "review_status": "rejected",
            },
            "error_info": {
                "error_type": facts.get("root_error_type"), "root_error_type": facts.get("root_error_type"),
                "terminal_error_type": facts.get("terminal_error_type"), "error_category": facts.get("error_category"),
                "error_message": str(exc), "retry_strategy": facts.get("retry_strategy"),
            },
            "error_category": str(facts.get("error_category") or "unknown"),
        }

def _successful_execution(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("success")
        and (_safe_int(value.get("items_count"), 0) or 0) > 0
    )


def _effective_execution_result(state: CrawlerState) -> Dict[str, Any]:
    best = state.get("best_execution_result")
    if _successful_execution(best):
        return dict(best)
    current = state.get("execution_result") if isinstance(state.get("execution_result"), dict) else {}
    if _successful_execution(current):
        return current
    saved = state.get("last_successful_execution_result")
    if _successful_execution(saved):
        return dict(saved)
    return current


def _resolve_execution_file(execution: Dict[str, Any], key: str = "data_file") -> Optional[Path]:
    raw = str(execution.get(key) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / path
    try:
        return path.resolve()
    except Exception:
        return path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_execution_artifacts(
    execution: Dict[str, Any],
    thread_id: str,
    invocation: int,
) -> Dict[str, Any]:
    """Archive every successful Code run before any best-result selection."""
    if not _successful_execution(execution):
        return dict(execution)
    safe_tid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", thread_id or "task")[:80]
    root = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "artifacts" / safe_tid / f"run_{max(1, invocation):03d}"
    root.mkdir(parents=True, exist_ok=True)
    archived: Dict[str, Any] = {
        "run_id": f"run_{max(1, invocation):03d}",
        "invocation": max(1, invocation),
        "items": _safe_int(execution.get("items_count"), 0) or 0,
        "pagination_complete": execution.get("pagination_complete"),
        "created_at": _now_iso(),
    }
    for key, label in (("data_file", "data"), ("code_file", "source")):
        source = _resolve_execution_file(execution, key)
        if source is None or not source.is_file():
            continue
        destination = root / source.name
        shutil.copy2(source, destination)
        archived[f"{label}_path"] = str(destination.resolve())
        archived[f"{label}_sha256"] = _sha256_file(destination)
        archived[f"{label}_size_bytes"] = destination.stat().st_size
        log_event(
            _pipeline_log, "artifact.save", status="saved",
            artifact_type="data_file_version" if label == "data" else "source_code_version",
            run_id=archived["run_id"], path=destination.resolve(),
            sha256=archived[f"{label}_sha256"], size_bytes=archived[f"{label}_size_bytes"],
            rows=archived["items"] if label == "data" else None,
        )
    enriched = dict(execution)
    enriched["artifact_version"] = archived
    return enriched


def _backup_successful_artifact(execution: Dict[str, Any], thread_id: str) -> Dict[str, str]:
    """Back up accepted data and source so a forced recheck cannot destroy them."""
    if not _successful_execution(execution):
        return {}
    safe_tid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", thread_id or "task")[:80]
    backup_dir = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "checkpoints" / "accepted_artifacts" / safe_tid
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot: Dict[str, str] = {}
    for key, label in (("data_file", "data"), ("code_file", "source")):
        source = _resolve_execution_file(execution, key)
        if source is None or not source.is_file():
            continue
        backup = backup_dir / f"{label}{source.suffix}.bak"
        shutil.copy2(source, backup)
        snapshot[f"{label}_source"] = str(source)
        snapshot[f"{label}_backup"] = str(backup)
    return snapshot


def _restore_successful_artifact(snapshot: Dict[str, str]) -> bool:
    restored = False
    for label in ("data", "source"):
        source_raw = str(snapshot.get(f"{label}_source") or "")
        backup_raw = str(snapshot.get(f"{label}_backup") or "")
        if not source_raw or not backup_raw:
            continue
        source = Path(source_raw)
        backup = Path(backup_raw)
        if not backup.is_file():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, source)
        restored = True
    return restored


def _warning_codes(execution: Dict[str, Any]) -> List[str]:
    values = execution.get("warning_codes") or execution.get("advisory_warnings") or []
    return list(dict.fromkeys(str(value)[:160] for value in values if value))


def _ai_confidence(execution: Dict[str, Any]) -> float:
    review = execution.get("ai_review") if isinstance(execution.get("ai_review"), dict) else {}
    return _safe_float(review.get("confidence"), 0.0)


def _choose_best_execution(
    best: Dict[str, Any],
    candidate: Dict[str, Any],
    policy: Dict[str, Any],
) -> tuple[Dict[str, Any], str, bool]:
    """Conservative artifact selection; lower coverage needs an explicit reason."""
    if not _successful_execution(candidate):
        return best, "candidate_not_successful", False
    if not _successful_execution(best):
        return candidate, "first_successful_artifact", True

    best_items = _safe_int(best.get("items_count"), 0) or 0
    candidate_items = _safe_int(candidate.get("items_count"), 0) or 0
    replacement_reason = str(policy.get("replacement_reason") or "").strip()
    recheck_reason = str(policy.get("recheck_reason") or "").strip()
    accept_smaller = bool(policy.get("accept_smaller_result"))

    if candidate_items < best_items:
        if not (accept_smaller and replacement_reason):
            return best, f"retained_higher_coverage:{best_items}>{candidate_items}", False
        return candidate, f"explicit_smaller_replacement:{replacement_reason[:500]}", True

    if best.get("pagination_complete") is True and candidate.get("pagination_complete") is not True:
        if not replacement_reason:
            return best, "retained_completed_pagination", False
        return candidate, f"explicit_pagination_tradeoff:{replacement_reason[:500]}", True

    if candidate_items > best_items:
        return candidate, f"higher_coverage:{candidate_items}>{best_items}", True
    if candidate.get("pagination_complete") is True and best.get("pagination_complete") is not True:
        return candidate, "pagination_completion_improved", True
    if _ai_confidence(candidate) > _ai_confidence(best) + 0.05:
        return candidate, "ai_confidence_improved", True
    if recheck_reason and candidate_items == best_items:
        return best, f"equivalent_after_recheck:{recheck_reason[:300]}", False
    return best, "equivalent_result_retained", False



def evaluate_execution(state: CrawlerState) -> CrawlerState:
    """Record probe facts or version a successful artifact without collapsing root causes."""
    execution = state.get("execution_result") if isinstance(state.get("execution_result"), dict) else {}
    error = state.get("error_info") if isinstance(state.get("error_info"), dict) else {}
    retry_count = int(state.get("retry_count", 0) or 0)
    invocation = int(state.get("code_version", 0) or 0)
    policy = state.get("pending_recheck_policy") if isinstance(state.get("pending_recheck_policy"), dict) else {}
    previous_best = _effective_execution_result({**state, "execution_result": {}})

    if execution.get("probe_completed"):
        probe = execution.get("probe_result") if isinstance(execution.get("probe_result"), dict) else {}
        root = str(execution.get("root_error_type") or error.get("root_error_type") or "api_contract_error")
        observed = bool(probe.get("target_data_observed"))
        _append_log(
            state, "INFO" if observed else "WARNING", "evaluate_execution", "访问探针事实已记录",
            status="success" if observed else "advisory", invocation=invocation,
            target_data_observed=observed, root_error_type=None if observed else root,
            terminal_error_type=execution.get("terminal_error_type"),
            error_category="none" if observed else execution.get("error_category"),
            retry_strategy=execution.get("retry_strategy"),
            observed_endpoints=len(probe.get("observed_endpoints") or []),
        )
        return {
            **state,
            "execution_result": execution,
            "execution_attempted": True,
            "last_probe_result": probe,
            "pending_recheck_policy": {},
            "retry_count": retry_count,
            "error_info": {} if observed else error,
            "error_category": "none" if observed else str(execution.get("error_category") or error.get("error_category") or _classify_error(root)),
        }

    success = _successful_execution(execution)
    if success:
        execution = {**execution, "code_checkpoint_snapshot": execution.get("code_checkpoint_snapshot") or state.get("code_checkpoint", {})}
        candidate = _version_execution_artifacts(execution, str(state.get("thread_id") or "task"), invocation)
        history = list(state.get("execution_history") or []) + [candidate]
        versions = list(state.get("artifact_versions") or [])
        if isinstance(candidate.get("artifact_version"), dict):
            versions.append(candidate["artifact_version"])
        selected, selection_reason, replaced = _choose_best_execution(previous_best, candidate, policy)
        warning_codes = _warning_codes(candidate)
        _append_log(
            state, "INFO", "evaluate_execution", "Code Agent 自检完成",
            status="success_with_warnings" if warning_codes else "success",
            items=_safe_int(candidate.get("items_count"), 0) or 0,
            warning_codes=warning_codes, warnings_count=len(warning_codes), invocation=invocation,
            repair_attempt=int(candidate.get("repair_attempts", 0) or 0),
        )
        log_event(
            _pipeline_log, "artifact.select", level="INFO" if replaced else "WARNING",
            status="selected" if replaced else "retained",
            candidate_run=(candidate.get("artifact_version") or {}).get("run_id"),
            candidate_items=_safe_int(candidate.get("items_count"), 0) or 0,
            previous_best_items=_safe_int(previous_best.get("items_count"), 0) or 0,
            selected_items=_safe_int(selected.get("items_count"), 0) or 0,
            replacement_reason=selection_reason, selected_data_file=selected.get("data_file"),
        )
        if not replaced and _successful_execution(previous_best):
            snapshot = state.get("accepted_artifact_snapshot") if isinstance(state.get("accepted_artifact_snapshot"), dict) else {}
            if snapshot:
                _restore_successful_artifact(snapshot)
            selected = dict(previous_best)
        selection_state = {
            "selected_run": ((selected.get("artifact_version") or {}).get("run_id") if isinstance(selected.get("artifact_version"), dict) else None),
            "candidate_run": ((candidate.get("artifact_version") or {}).get("run_id") if isinstance(candidate.get("artifact_version"), dict) else None),
            "candidate_items": _safe_int(candidate.get("items_count"), 0) or 0,
            "selected_items": _safe_int(selected.get("items_count"), 0) or 0,
            "previous_best_items": _safe_int(previous_best.get("items_count"), 0) or 0,
            "replacement_reason": selection_reason, "replaced": bool(replaced),
        }
        completed = {
            **state, "execution_result": selected, "last_successful_execution_result": selected,
            "best_execution_result": selected, "code_checkpoint": selected.get("code_checkpoint_snapshot") or state.get("code_checkpoint", {}),
            "execution_history": history[-20:], "artifact_versions": versions[-20:], "selection_state": selection_state,
            "failed_code_attempt": candidate if not replaced else state.get("failed_code_attempt", {}),
            "pending_recheck_policy": {}, "accepted_artifact_snapshot": {}, "error_info": {},
            "error_category": "none", "execution_attempted": True,
        }
        _save_rag(completed)
        return completed

    root = str(error.get("root_error_type") or execution.get("root_error_type") or error.get("error_type") or execution.get("error_type") or "crawler_runtime_error")
    terminal = str(error.get("terminal_error_type") or execution.get("terminal_error_type") or execution.get("error_type") or "empty_data")
    category = str(error.get("error_category") or execution.get("error_category") or _classify_error(root))
    retry_strategy = str(error.get("retry_strategy") or execution.get("retry_strategy") or "inspect_facts")
    _append_log(
        state, "WARNING", "evaluate_execution", "Code Agent 自检未通过", status="failed",
        root_error_type=root, terminal_error_type=terminal, error_category=category,
        retry_strategy=retry_strategy, repair_attempt=retry_count + 1,
        warning_codes=_warning_codes(execution),
    )
    failed_error = error or {
        "error_type": root, "root_error_type": root, "terminal_error_type": terminal,
        "error_category": category, "error_message": str(execution.get("fix_info") or "Code Agent 自检认为任务未完成。"),
        "suggested_fix": retry_strategy, "retry_strategy": retry_strategy,
    }
    failed_state = {
        **state, "execution_result": execution, "error_info": failed_error,
        "error_category": category, "execution_attempted": True,
        "retry_count": retry_count + 1, "pending_recheck_policy": {},
    }
    if _successful_execution(previous_best):
        snapshot = state.get("accepted_artifact_snapshot") if isinstance(state.get("accepted_artifact_snapshot"), dict) else {}
        if snapshot:
            _restore_successful_artifact(snapshot)
        failed_state.update({
            "failed_code_attempt": execution, "execution_result": previous_best,
            "last_successful_execution_result": previous_best, "best_execution_result": previous_best,
            "code_checkpoint": previous_best.get("code_checkpoint_snapshot") or state.get("code_checkpoint", {}),
            "accepted_artifact_snapshot": {}, "error_info": {}, "error_category": "none",
            "selection_state": {
                "selected_run": ((previous_best.get("artifact_version") or {}).get("run_id") if isinstance(previous_best.get("artifact_version"), dict) else None),
                "candidate_run": None, "candidate_items": _safe_int(execution.get("items_count"), 0) or 0,
                "selected_items": _safe_int(previous_best.get("items_count"), 0) or 0,
                "previous_best_items": _safe_int(previous_best.get("items_count"), 0) or 0,
                "replacement_reason": "candidate_failed", "replaced": False,
            },
        })
        log_event(
            _pipeline_log, "artifact.select", level="WARNING", status="retained",
            candidate_items=_safe_int(execution.get("items_count"), 0) or 0,
            previous_best_items=_safe_int(previous_best.get("items_count"), 0) or 0,
            selected_items=_safe_int(previous_best.get("items_count"), 0) or 0,
            replacement_reason="candidate_failed",
        )
    return failed_state


# =============================================================================
# 能力实现：最终输出
# =============================================================================

def finalize(state: CrawlerState) -> CrawlerState:
    execution = _effective_execution_result(state)
    error = state.get("error_info") or {}
    parser = state.get("parser_result") or {}

    capability_counts = dict(state.get("capability_counts") or {})
    # Actual executions are derived from state mutations, not requested/skipped
    # capability calls. This keeps retry accounting honest.
    browser_runs = int(state.get("parser_attempts", 0) or 0)
    code_runs = int(state.get("code_version", 0) or 0)
    browser_retries = max(0, browser_runs - 1)
    execution_retries = max(0, code_runs - 1)
    pipeline_retries = browser_retries + execution_retries
    code_repairs = max(
        int(execution.get("repair_attempts", 0) or 0),
        int(capability_counts.get("recheck_code", 0) or 0),
    )
    duration_ms = int(max(0.0, time.time() - float(state.get("task_started_at", time.time()) or time.time())) * 1000)
    selected_version = execution.get("artifact_version") if isinstance(execution.get("artifact_version"), dict) else {}
    selection_state = state.get("selection_state") if isinstance(state.get("selection_state"), dict) else {}

    success = bool(execution.get("success") and _safe_int(execution.get("items_count"), 0) > 0)
    root_error = str(error.get("root_error_type") or execution.get("root_error_type") or error.get("error_type") or "")
    terminal_error = str(error.get("terminal_error_type") or execution.get("terminal_error_type") or execution.get("error_type") or "")
    error_category = str(error.get("error_category") or execution.get("error_category") or (_classify_error(root_error) if root_error else "none"))
    retry_strategy = str(error.get("retry_strategy") or execution.get("retry_strategy") or "") or None
    auth_facts = state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {}
    auth_state = str(auth_facts.get("state") or state.get("auth_status") or "unknown")

    if success:
        status = "success"
        summary = f"已成功提取 {execution.get('items_count', 0)} 条数据，保存为 {state.get('output_format', 'csv').upper()} 文件。"
        next_action = None
    elif (error_category == "authentication" or auth_state in {"required", "challenge", "provisional"}) and error_category not in {"access", "service", "network", "dependency"}:
        status = "need_user_action"
        if auth_state == "challenge" or root_error in {"challenge_required", "captcha_required", "mfa_required"}:
            summary = "目标站点仍处于验证码、风控或安全验证状态；人工确认登录后也未验证目标页面可访问。"
        elif auth_state == "provisional" or root_error == "authentication_unverified":
            summary = "用户已确认登录，但系统尚未验证登录态能够访问目标数据；需要重新打开目标页并完成认证复验。"
        else:
            summary = "目标页面或数据接口仍要求认证，当前会话未获得可验证的目标数据。"
        next_action = "manual_login_and_verify"
    elif error_category == "access":
        status = "failed"
        summary = f"目标站点拒绝或限制了当前访问上下文（{root_error or 'access_denied'}）；继续更换接口或重复请求没有产生目标数据。"
        next_action = "change_access_context_or_retry_later"
    elif error_category == "service":
        status = "failed"
        summary = f"目标服务当前不可用或返回系统繁忙（{root_error or 'service_unavailable'}），有界探针未观察到目标数据。"
        next_action = "retry_later"
    elif error_category == "network":
        status = "failed"
        summary = f"网络探针未能稳定访问目标页面或接口（{root_error or 'network_error'}）。"
        next_action = "check_network"
    elif error_category == "dependency":
        status = "failed"
        summary = f"Code Agent 的依赖或导入问题尚未修复（{root_error or 'dependency_error'}），因此未进入有效爬取执行。"
        next_action = "repair_dependency"
    elif error_category == "code":
        status = "failed"
        summary = f"Agent 运行或生成代码时发生错误（{root_error or 'crawler_runtime_error'}）；终止症状为 {terminal_error or 'unknown'}。"
        next_action = retry_strategy or "inspect_runtime_error"
    elif error_category == "budget":
        status = "failed"
        summary = f"Agent 在形成可接受结果前耗尽运行预算（{root_error or 'tool_budget_exhausted'}）。"
        next_action = retry_strategy or "resume_with_focused_context"
    elif error_category == "parser":
        status = "failed"
        summary = f"尚未获得经过观察或探针验证的目标数据结构（{root_error or 'parser_error'}）。"
        next_action = "collect_new_evidence"
    elif state.get("parser_confidence", 0) < LOW_CONFIDENCE_STOP and not root_error:
        status = "failed"
        summary = "Browser AI 没有形成可用方案，且当前没有更明确的认证、访问或接口根因。"
        next_action = "inspect_browser_evidence"
    else:
        status = "failed"
        summary = (
            f"任务未完成（根因={root_error or 'unknown'}，终止症状={terminal_error or 'unknown'}），"
            f"Browser 运行 {browser_runs} 次，Code 运行 {code_runs} 次。"
        )
        next_action = retry_strategy or ("inspect_code_debug" if execution.get("debug_file") else "check_logs")

    final_output = {
        "status": status,
        "data_file": execution.get("data_file") if success else None,
        "total_items": _safe_int(execution.get("items_count"), 0),
        "pagination_complete": execution.get("pagination_complete"),
        "crawl_meta": execution.get("crawl_meta", {}),
        "output_quality": execution.get("output_quality", {}),
        "ai_review": execution.get("ai_review", {}),
        "validation_mode": execution.get("validation_mode", "ai_self_review"),
        "advisory_warnings": execution.get("advisory_warnings", []),
        "warning_codes": _warning_codes(execution),
        "fields": execution.get("fields") or state.get("target_fields", []),
        "execution_time": execution.get("execution_time"),
        "summary": summary,
        "code_file": (execution.get("code_file") or state.get("code_file")) if success else None,
        "log_file": execution.get("log_file"),
        "debug_file": execution.get("debug_file"),
        "debug_report": execution.get("debug_report", {}),
        "pipeline_retries": pipeline_retries,
        "browser_retries": browser_retries,
        "execution_retries": execution_retries,
        "code_repairs": code_repairs,
        "capability_counts": capability_counts,
        "browser_runs": browser_runs,
        "code_runs": code_runs,
        "code_probe_runs": int(state.get("code_probe_runs", 0) or 0),
        "code_full_runs": max(0, code_runs - int(state.get("code_probe_runs", 0) or 0)),
        "duration_ms": duration_ms,
        "selected_run": selected_version.get("run_id"),
        "selected_items": _safe_int(execution.get("items_count"), 0) or 0,
        "previous_best_items": _safe_int(selection_state.get("previous_best_items"), 0) or 0,
        "latest_candidate_items": _safe_int(selection_state.get("candidate_items"), 0) or 0,
        "latest_candidate_run": selection_state.get("candidate_run"),
        "replacement_reason": selection_state.get("replacement_reason"),
        "selection_replaced": bool(selection_state.get("replaced")),
        "artifact_versions": list(state.get("artifact_versions") or []),
        "code_sha256": selected_version.get("source_sha256"),
        "data_sha256": selected_version.get("data_sha256"),
        "next_action": next_action,
        "parser_confidence": state.get("parser_confidence", 0),
        "parser_confidence_breakdown": parser.get("confidence_breakdown", {}),
        "authentication_state": auth_state,
        "authenticated": auth_facts.get("authenticated"),
        "auth_verification_state": auth_facts.get("verification_state"),
        "root_error_type": root_error or None,
        "terminal_error_type": terminal_error or None,
        "error_category": error_category,
        "retry_strategy": retry_strategy,
        "progress_history": list(state.get("progress_history") or [])[-10:],
        "no_progress_streaks": dict(state.get("no_progress_streaks") or {}),
        "error_info": {
            "error_type": root_error if not success else None,
            "root_error_type": root_error if not success else None,
            "terminal_error_type": terminal_error if not success else None,
            "error_category": error_category if not success else None,
            "retry_strategy": retry_strategy if not success else None,
            "error_message": error.get("error_message") if not success else None,
            "internal_error_type": error.get("internal_error_type") if not success else None,
            "debug_file": error.get("debug_file") if not success else None,
        },
        "api_stats": get_all_summaries(),
    }

    if state.get("need_code_return"):
        selected_code_path = _resolve_execution_file(execution, "code_file")
        if selected_code_path is not None and selected_code_path.is_file():
            try:
                final_output["generated_code"] = selected_code_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception:
                final_output["generated_code"] = state.get("generated_code", "")
        elif state.get("generated_code"):
            final_output["generated_code"] = state.get("generated_code", "")

    state_path = None
    try:
        _ensure_dirs()
        tid = state.get("thread_id", "supervisor")
        safe_tid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tid)
        snap = STATE_DIR / f"{safe_tid}_final.json"
        snap.write_text(json_dumps({
            "thread_id": tid,
            "target_url": state.get("target_url"),
            "target_fields": state.get("target_fields"),
            "parser_confidence": state.get("parser_confidence"),
            "retry_count": state.get("retry_count"),
            "web_retry_count": state.get("web_retry_count"),
            "error_info": error,
            "browser_pipeline_info": state.get("browser_pipeline_info", {}),
            "final_output": final_output,
            "created_at": _now_iso(),
        }), encoding="utf-8")
        state_path = str(snap)
    except Exception:
        pass

    try:
        api_file = LOG_DIR / f"{safe_tid}_api_stats.json"
        api_file.write_text(json_dumps(final_output.get("api_stats", {})), encoding="utf-8")
    except Exception:
        pass

    _append_log(
        state, "INFO" if success else "WARNING", "finalize", "任务结束",
        status="success" if success else "failed", items=final_output["total_items"],
        duration_ms=duration_ms, selected_run=final_output.get("selected_run"),
        previous_best_items=final_output.get("previous_best_items"),
        latest_candidate_items=final_output.get("latest_candidate_items"),
        replacement_reason=final_output.get("replacement_reason"),
        capability_counts=capability_counts, warning_codes=final_output.get("warning_codes", []),
        root_error_type=root_error if not success else None, terminal_error_type=terminal_error if not success else None,
        error_category=error_category if not success else None, retry_strategy=retry_strategy if not success else None,
        authentication_state=auth_state,
    )

    return {
        **state,
        "final_output": final_output,
    }


# =============================================================================
# 任务规范化
# =============================================================================



def normalize_task(task: str) -> str:
    if not task:
        return task

    # 纯 URL 判断：必须以 http(s):// 或域名(字母数字开头) 开头，不能以中文开头
    pure_url_like = re.fullmatch(
        r'(?:https?://)?(?:[a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}(?:/[^\s]*)?/?',
        task.strip(),
    )
    if pure_url_like:
        target_url = task.strip()
        if not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url
        return f"使用爬虫代码爬取 {target_url} 页面上的主要数据，并保存为 csv 数据文件"

    if "http://" not in task and "https://" not in task:
        # 用 (?<![a-zA-Z0-9]) 替代 \b，避免中文边界问题
        domain_match = re.search(
            r'(?<![a-zA-Z0-9])(?:www\.)?(?:[a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}(?:/[^\s]*)?',
            task,
        )
        if domain_match:
            domain = domain_match.group(0)
            task = task.replace(domain, "https://" + domain, 1)

    return task


# =============================================================================
# 公共入口
# =============================================================================




SUPERVISOR_NATIVE_PROMPT = """你是通过 pi-agent-core 运行的自主 Supervisor Agent。你负责完成爬虫任务，并依据实时证据自主调度能力。

你拥有一组能力工具，可依据当前证据自由选择、重复或跳过：
- set_task_spec：记录或纠正结构化任务。
- search_strategy：可选的历史策略检索；不是必经步骤。
- run_browser：运行或恢复普通 Browser 探索。Browser Agent 自行判断解析方案。
- resolve_authentication：执行独立认证协议；宿主把 Browser 限制为 manual_login → auth_probe → submit_parser，不是普通探索提示。
- run_code：仅在尚无成功产物时运行或恢复 pi-coding-agent。
- recheck_code：已有成功产物后，仅在发现具体缺陷时显式复验；必须说明 recheck_reason。若要接受更少的数据，还必须设置 accept_smaller_result=true 并解释 replacement_reason。
- inspect_task：读取当前事实、预算和建议，不修改状态。
- finalize_task：在成功或无法安全继续时生成最终结果。

工作原则：
1. 目标 URL 或字段尚未记录时，先调用 set_task_spec。解析规则：第一个完整 http/https URL；只说“全部评论”时字段为 ["评论内容","评论用户","评论时间","点赞数"]；未指定数量时 max_items=null；默认 csv。
2. RAG 是可选能力，不得因为没有检索 RAG 而阻止 Browser。
3. 尚未成功时 Browser 与 Code 可按证据重试。已有成功产物后，默认立即 finalize_task；不得再次调用普通 run_code。只有发现具体缺陷时才使用 recheck_code。
4. 可以在同一模型回复中调用多个工具；有副作用的工具会顺序执行，因此 run_browser 后可直接 run_code，但不得伪造工具结果。
5. Python 只负责权限、安全边界、预算、checkpoint 和最低事实记录，不再用固定源码模式或证据模板决定业务成败。你应结合 Agent 自检、真实 stdout、数据文件与样本自行判断。
6. 不要停留在文字解释。任务成功或确实没有安全且有价值的行动后，调用 finalize_task。
7. 对不确定状态先调用 inspect_task；不要盲目重复已经无收益的能力。成功产物受保护，新结果更少时默认保留历史最佳。
8. 认证是事实而不是普通 warning：required/challenge/provisional 时调用 resolve_authentication，不得用 run_browser 的 focus 代替认证指令。认证协议固定为 manual_login → auth_probe → submit_parser；用户确认登录不等于验证成功。登录后禁止建议切换 User-Agent、viewport、locale、时区、浏览器引擎、代理或其他指纹。
9. API 候选必须区分 observed、historical、hypothesized。未验证的猜测只能进入有界访问探针，不得直接启动完整爬虫。
10. Code 探针只回答可达性、内容类型、目标数据是否出现和根因。authentication/access/rate_limit/service 等根因连续无进展时应停止，不要反复运行 Browser/Code。
11. 始终优先使用 root_error_type、terminal_error_type、error_category、retry_strategy 和 capability.progress；不得把 access denied、限流、认证或依赖错误降级成 empty_data/parser。
"""


def _pipeline_checkpoint_path(thread_id: str) -> Path:
    safe_tid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", thread_id or "supervisor")
    return STATE_DIR / f"{safe_tid}_checkpoint.json"


def _safe_transcript(value: Any, *, max_messages: int = 160) -> List[Dict[str, Any]]:
    return sanitize_agent_transcript(value, max_messages=max_messages)


def _save_pipeline_checkpoint(
    state: CrawlerState,
    last_capability: str,
    *,
    agent_transcript: Optional[List[Dict[str, Any]]] = None,
    recommended_actions: Optional[List[str]] = None,
) -> None:
    try:
        _ensure_dirs()
        transcript = _safe_transcript(
            agent_transcript if agent_transcript is not None else state.get("agent_transcript", [])
        )
        payload = {
            "version": 2,
            "thread_id": state.get("thread_id"),
            "user_request": state.get("user_request"),
            "last_capability": last_capability,
            "recommended_actions": list(recommended_actions or _recommended_actions(state)),
            "agent_transcript": transcript,
            "state": json.loads(json.dumps({**state, "agent_transcript": transcript}, ensure_ascii=False, default=str)),
            "updated_at": _now_iso(),
        }
        _pipeline_checkpoint_path(str(state.get("thread_id") or "supervisor")).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log_event(_pipeline_log, "checkpoint.save", level="WARNING", status="failed", scope="pipeline", error_type="checkpoint_write_failed", reason=str(exc))


def _load_pipeline_checkpoint(
    thread_id: str,
    user_request: str,
) -> Optional[Dict[str, Any]]:
    if str(os.getenv("PIPELINE_RESUME_CHECKPOINT", "true")).strip().lower() in {
        "0", "false", "no", "off"
    }:
        return None
    path = _pipeline_checkpoint_path(thread_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("user_request") != user_request:
            return None
        state = payload.get("state")
        if not isinstance(state, dict):
            return None
        final_output = state.get("final_output") if isinstance(state.get("final_output"), dict) else {}
        if state.get("terminal_finalized") or final_output.get("status") == "success":
            return None
        if state.get("resumable"):
            state = {**state, "final_output": {}, "resumable": False}
            payload["state"] = state
        # Older checkpoints are accepted for factual state recovery only.
        # Native transcripts are used when available.
        if not isinstance(payload.get("agent_transcript"), list):
            payload["agent_transcript"] = state.get("agent_transcript", [])
        return payload
    except Exception:
        return None


def _current_parser_validation(state: CrawlerState) -> Dict[str, Any]:
    parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    cached = parser.get("_validation") if isinstance(parser.get("_validation"), dict) else None
    return cached or _validate_parser_result(parser, state)



def _state_summary(state: CrawlerState) -> Dict[str, Any]:
    parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    execution = _effective_execution_result(state)
    current_execution = state.get("execution_result") if isinstance(state.get("execution_result"), dict) else {}
    error = state.get("error_info") if isinstance(state.get("error_info"), dict) else {}
    validation = _current_parser_validation(state) if parser else {}
    auth = validation.get("auth_facts") if isinstance(validation.get("auth_facts"), dict) else (state.get("auth_facts") or {})
    items_count = _safe_int(execution.get("items_count"), 0) or 0
    execution_success = bool(execution.get("success") and items_count > 0)
    root_error = str(error.get("root_error_type") or current_execution.get("root_error_type") or error.get("error_type") or "")
    terminal_error = str(error.get("terminal_error_type") or current_execution.get("terminal_error_type") or "")
    category = str(error.get("error_category") or current_execution.get("error_category") or (_classify_error(root_error) if root_error else "none"))
    retry_strategy = str(error.get("retry_strategy") or current_execution.get("retry_strategy") or "") or None
    selection = state.get("selection_state") if isinstance(state.get("selection_state"), dict) else {}
    return {
        "thread_id": state.get("thread_id"),
        "spec_ready": bool(state.get("target_url") and state.get("target_fields")),
        "target_url": sanitize_url(str(state.get("target_url") or "")),
        "target_fields": state.get("target_fields", []), "output_format": state.get("output_format", "csv"),
        "max_items": state.get("max_items"), "rag_checked": bool(state.get("rag_checked")),
        "rag_hits": len(state.get("rag_hits") or []), "parser_attempts": int(state.get("parser_attempts", 0) or 0),
        "parser_valid": bool(validation.get("full_code_ready")), "parser_usable_for_probe": bool(validation.get("can_enter_code")),
        "parser_confidence": state.get("parser_confidence", parser.get("confidence", 0)),
        "data_source": state.get("data_source", parser.get("data_source", "unknown")),
        "code_entry_mode": str(state.get("code_entry_mode") or validation.get("code_entry_mode") or "probe"),
        "authentication_state": auth.get("state") or "unknown", "authenticated": auth.get("authenticated"),
        "auth_verification_state": auth.get("verification_state") or "unverified",
        "code_version": int(state.get("code_version", 0) or 0), "code_probe_runs": int(state.get("code_probe_runs", 0) or 0),
        "auth_resolution_attempts": int(state.get("auth_resolution_attempts", 0) or 0),
        "execution_attempted": bool(state.get("execution_attempted") or state.get("code_version", 0)),
        "execution_success": execution_success, "items_count": items_count,
        "best_run_id": ((execution.get("artifact_version") or {}).get("run_id") if isinstance(execution.get("artifact_version"), dict) else None),
        "execution_runs": len(state.get("execution_history") or []),
        "latest_candidate_items": _safe_int(selection.get("candidate_items"), 0) or 0,
        "selection_reason": selection.get("replacement_reason"), "pagination_complete": execution.get("pagination_complete"),
        "probe_result": state.get("last_probe_result") or current_execution.get("probe_result") or {},
        "retry_count": int(state.get("retry_count", 0) or 0), "web_retry_count": int(state.get("web_retry_count", 0) or 0),
        "max_retries": int(state.get("max_retries", DEFAULT_MAX_RETRIES) or DEFAULT_MAX_RETRIES),
        "fix_exhausted": bool(state.get("fix_exhausted") or error.get("fix_exhausted")),
        "root_error_type": root_error or None, "terminal_error_type": terminal_error or None,
        "error_category": category, "retry_strategy": retry_strategy,
        "error_message": str(error.get("error_message") or "")[:1200] or None,
        "no_progress_streaks": dict(state.get("no_progress_streaks") or {}),
        "latest_progress": (list(state.get("progress_history") or [])[-1] if state.get("progress_history") else None),
        "final_status": (state.get("final_output") or {}).get("status"),
        "capability_history": list(state.get("capability_history") or [])[-20:],
        "capability_counts": dict(state.get("capability_counts") or {}),
    }


def _recommended_actions(state: CrawlerState) -> List[str]:
    summary = _state_summary(state)
    if summary.get("final_status"):
        return []
    if not summary["spec_ready"]:
        return ["set_task_spec", "inspect_task", "finalize_task"]
    if summary["execution_success"]:
        return ["finalize_task", "inspect_task"]

    actions: List[str] = []
    if not summary["rag_checked"]:
        actions.append("search_strategy")
    streaks = summary.get("no_progress_streaks") or {}
    browser_stalled = int(streaks.get("browser", 0) or 0) >= 2
    code_stalled = int(streaks.get("code", 0) or 0) >= 2
    auth_state = str(summary.get("authentication_state") or "unknown")
    category = str(summary.get("error_category") or "none")
    mode = str(summary.get("code_entry_mode") or "probe")

    if auth_state in {"required", "challenge", "provisional"}:
        auth_attempts = int(summary.get("auth_resolution_attempts", 0) or 0)
        if auth_attempts < 2:
            actions.append("resolve_authentication")
        if auth_attempts >= 1 and mode == "probe" and not code_stalled and int(summary.get("code_probe_runs", 0) or 0) < 1:
            actions.append("run_code")
        if auth_attempts >= 2 or browser_stalled or category in {"access", "service"}:
            actions.append("finalize_task")
    elif mode == "probe":
        if not code_stalled:
            actions.append("run_code")
        if not browser_stalled:
            actions.append("run_browser")
        if code_stalled and browser_stalled:
            actions.append("finalize_task")
    elif not summary["execution_attempted"]:
        actions.append("run_code")
    elif category in {"dependency", "code"} and not code_stalled and not summary["fix_exhausted"]:
        actions.append("run_code")
    elif category == "parser" and not browser_stalled:
        actions.append("run_browser")
    elif category in {"authentication", "access", "service", "network", "budget"}:
        if summary.get("retry_strategy") == "resolve_authentication" and int(summary.get("auth_resolution_attempts", 0) or 0) < 2:
            actions.append("resolve_authentication")
        elif summary.get("retry_strategy") == "bounded_network_probe" and not code_stalled:
            actions.append("run_code")
        else:
            actions.append("finalize_task")
    else:
        if not code_stalled and int(summary.get("retry_count", 0)) < int(summary.get("max_retries", 0)):
            actions.append("run_code")
        else:
            actions.append("finalize_task")
    actions.append("inspect_task")
    return list(dict.fromkeys(actions))

def _initial_supervisor_state(
    user_request: str,
    thread_id: str,
) -> CrawlerState:
    return {
        "user_request": user_request,
        "thread_id": thread_id,
        "task_id": thread_id,
        "retry_count": 0,
        "web_retry_count": 0,
        "max_retries": DEFAULT_MAX_RETRIES,
        "parser_attempts": 0,
        "login_attempts": 0,
        "code_version": 0,
        "parser_result": {},
        "browser_checkpoint": {},
        "code_checkpoint": {},
        "browser_pipeline_info": {},
        "execution_result": {},
        "last_successful_execution_result": {},
        "best_execution_result": {},
        "execution_history": [],
        "artifact_versions": [],
        "pending_recheck_policy": {},
        "accepted_artifact_snapshot": {},
        "selection_state": {},
        "failed_code_attempt": {},
        "browser_attempt_result": {},
        "error_info": {},
        "error_category": "none",
        "rag_hits": [],
        "rag_checked": False,
        "interaction_plan": [],
        "browser_feedback": {},
        "log_entries": [],
        "final_output": {},
        "output_format": "csv",
        "page_type": "unknown",
        "data_source": "unknown",
        "parser_confidence": 0.0,
        "parser_valid": False,
        "execution_attempted": False,
        "auth_status": "unknown",
        "auth_facts": {"state": "unknown", "authenticated": None, "verification_state": "unverified"},
        "auth_resolution_attempts": 0,
        "browser_operation_mode": "explore",
        "browser_required_action": None,
        "need_login": "unknown",
        "target_fields": [],
        "target_url": "",
        "capability_history": [],
        "agent_transcript": [],
        "terminal_finalized": False,
        "resumable": False,
        "task_started_at": time.time(),
        "capability_counts": {},
        "progress_history": [],
        "no_progress_streaks": {},
        "code_entry_mode": "probe",
        "code_probe_runs": 0,
        "last_probe_result": {},
    }


def run_supervisor(
    user_request: str,
    thread_id: Optional[str] = None,
) -> CrawlerState:
    """Run an autonomous pi-agent-core Supervisor capability loop.

    The model chooses capabilities based on evidence rather than following a
    predetermined graph. Python capability implementations remain idempotent,
    persist evidence, and enforce only safety/minimum artifact facts. Semantic
    correctness is reviewed by the Agents.
    """
    if thread_id is None:
        thread_id = f"crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    set_log_context(task_id=thread_id)
    reset_all_trackers()
    _ensure_dirs()

    restored = _load_pipeline_checkpoint(thread_id, user_request)
    if restored:
        active_state: CrawlerState = dict(restored.get("state") or {})
        initial_transcript = _safe_transcript(restored.get("agent_transcript") or active_state.get("agent_transcript"))
        log_event(
            _pipeline_log, "checkpoint.restore", status="resumed", scope="pipeline",
            action=restored.get("last_capability"), transcript_messages=len(initial_transcript),
        )
    else:
        active_state = _initial_supervisor_state(user_request, thread_id)
        initial_transcript = []

    active_state.setdefault("task_started_at", time.time())
    active_state.setdefault("capability_counts", {})
    active_state.setdefault("best_execution_result", active_state.get("last_successful_execution_result", {}))
    active_state.setdefault("execution_history", [])
    active_state.setdefault("artifact_versions", [])
    active_state.setdefault("pending_recheck_policy", {})
    active_state.setdefault("accepted_artifact_snapshot", {})
    active_state.setdefault("selection_state", {})
    active_state.setdefault("auth_facts", {"state": "unknown", "authenticated": None, "verification_state": "unverified"})
    active_state.setdefault("auth_resolution_attempts", 0)
    active_state.setdefault("browser_operation_mode", "explore")
    active_state.setdefault("browser_required_action", None)
    active_state.setdefault("progress_history", [])
    active_state.setdefault("no_progress_streaks", {})
    active_state.setdefault("code_entry_mode", "probe")
    active_state.setdefault("code_probe_runs", 0)
    active_state.setdefault("last_probe_result", {})
    capability_history: List[str] = list(active_state.get("capability_history") or [])

    def capability_result(
        capability: str,
        *,
        ok: bool = True,
        error: Optional[str] = None,
        validation: Optional[Dict[str, Any]] = None,
        include_evidence: bool = False,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": ok,
            "capability": capability,
            "error": error,
            "state": _state_summary(active_state),
            "recommended_actions": _recommended_actions(active_state),
        }
        if validation is not None:
            result["validation"] = validation
        if include_evidence:
            parser = active_state.get("parser_result") if isinstance(active_state.get("parser_result"), dict) else {}
            execution = _effective_execution_result(active_state)
            result["evidence"] = {
                "parser_validation": parser.get("_validation", {}),
                "browser_checkpoint": {
                    "requests": len((((active_state.get("browser_checkpoint") or {}).get("material") or {}).get("requests") or [])),
                    "response_bodies": len((((active_state.get("browser_checkpoint") or {}).get("material") or {}).get("response_bodies") or [])),
                    "item_ids": len((((active_state.get("browser_checkpoint") or {}).get("evidence") or {}).get("item_ids") or [])),
                },
                "output_quality": execution.get("output_quality", {}),
                "ai_review": execution.get("ai_review", {}),
                "validation_mode": execution.get("validation_mode", "ai_self_review"),
                "advisory_warnings": execution.get("advisory_warnings", []),
                "warning_codes": _warning_codes(execution),
                "pagination_violations": execution.get("pagination_violations", []),
            }
        if active_state.get("final_output"):
            result["final_output"] = active_state.get("final_output")
        return result

    def handle_capability_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal active_state, capability_history
        capability = str(name or "")
        arguments = arguments if isinstance(arguments, dict) else {}
        counts = dict(active_state.get("capability_counts") or {})
        counts[capability] = int(counts.get(capability, 0) or 0) + 1
        active_state = {**active_state, "capability_counts": counts}
        decision_reason = str(
            arguments.get("reason")
            or arguments.get("recheck_reason")
            or arguments.get("repair_focus")
            or arguments.get("focus")
            or ""
        ).strip()
        best_now = _effective_execution_result(active_state)
        log_event(
            _supervisor_log,
            "agent.decision",
            status="received",
            agent="supervisor",
            phase="pipeline",
            action=capability,
            invocation=counts[capability],
            decision_reason=decision_reason[:1000] or None,
            current_best_items=_safe_int(best_now.get("items_count"), 0) or 0,
            current_best_run=((best_now.get("artifact_version") or {}).get("run_id") if isinstance(best_now.get("artifact_version"), dict) else None),
            force_recheck=(capability == "recheck_code"),
        )

        if capability == "inspect_task":
            return capability_result(
                capability,
                include_evidence=bool(arguments.get("include_evidence")),
            )

        if capability == "set_task_spec":
            request = arguments.get("request")
            if not isinstance(request, dict):
                return capability_result(capability, ok=False, error="set_task_spec_missing_request")
            previous_url = active_state.get("target_url")
            active_state = parse_request(active_state, request)
            if previous_url and active_state.get("target_url") != previous_url:
                active_state = {
                    **active_state,
                    "rag_hits": [],
                    "rag_checked": False,
                    "parser_result": {},
                    "browser_checkpoint": {},
                    "execution_result": {},
                    "last_successful_execution_result": {},
                    "best_execution_result": {},
                    "execution_history": [],
                    "artifact_versions": [],
                    "selection_state": {},
                    "pending_recheck_policy": {},
                    "accepted_artifact_snapshot": {},
                    "code_checkpoint": {},
                    "parser_valid": False,
                    "execution_attempted": False,
                    "auth_status": "unknown",
                    "auth_facts": {"state": "unknown", "authenticated": None, "verification_state": "unverified"},
                    "auth_resolution_attempts": 0,
                    "browser_operation_mode": "explore",
                    "browser_required_action": None,
                    "progress_history": [],
                    "no_progress_streaks": {},
                    "code_entry_mode": "probe",
                    "code_probe_runs": 0,
                    "last_probe_result": {},
                }

        elif capability == "search_strategy":
            if not active_state.get("target_url"):
                return capability_result(capability, ok=False, error="task_spec_required")
            active_state = rag_check(active_state)

        elif capability in {"run_browser", "resolve_authentication"}:
            if not active_state.get("target_url"):
                return capability_result(capability, ok=False, error="task_spec_required")
            auth_resolution = capability == "resolve_authentication"
            if auth_resolution:
                reason = str(arguments.get("reason") or "").strip()
                if len(reason) < 8:
                    return capability_result(capability, ok=False, error="authentication_reason_required")
                current_auth = str((active_state.get("auth_facts") or {}).get("state") or "unknown")
                if current_auth == "verified":
                    return capability_result(capability, ok=True, skipped=True, error=None)
                auth_attempts = int(active_state.get("auth_resolution_attempts", 0) or 0)
                if auth_attempts >= 2:
                    return capability_result(capability, ok=False, error="authentication_resolution_budget_exhausted")
                active_state = {
                    **active_state,
                    "auth_resolution_attempts": auth_attempts + 1,
                    "browser_operation_mode": "resolve_authentication",
                    "browser_required_action": "manual_login_and_verify",
                }
            else:
                active_state = {
                    **active_state,
                    "browser_operation_mode": "explore",
                    "browser_required_action": None,
                }
            browser_streak = int((active_state.get("no_progress_streaks") or {}).get("browser", 0) or 0)
            if browser_streak >= 2 and not arguments.get("force_refresh") and not auth_resolution:
                return capability_result(capability, ok=False, error="browser_no_progress_limit_reached")
            prior_attempts = int(active_state.get("parser_attempts", 0) or 0)
            max_retries = int(active_state.get("max_retries", DEFAULT_MAX_RETRIES) or DEFAULT_MAX_RETRIES)
            if prior_attempts >= max_retries + 1 and not auth_resolution:
                return capability_result(capability, ok=False, error="browser_retry_budget_exhausted")
            if prior_attempts > 0:
                active_state = {
                    **active_state,
                    "web_retry_count": int(active_state.get("web_retry_count", 0) or 0) + 1,
                }
                _append_log(
                    active_state, "INFO", capability,
                    "执行认证专用 Browser 协议" if auth_resolution else "再次执行 Browser 能力",
                    status="started", invocation=active_state["web_retry_count"] + 1,
                    operation_mode=active_state.get("browser_operation_mode"),
                    required_action=active_state.get("browser_required_action"),
                )
            feedback = dict(active_state.get("browser_feedback") or {})
            focus = str(arguments.get("focus") or "").strip()
            if focus and not auth_resolution:
                feedback["supervisor_focus"] = focus[:2000]
            if auth_resolution:
                feedback.pop("supervisor_focus", None)
                feedback["authentication_reason"] = str(arguments.get("reason") or "")[:2000]
                feedback["required_action"] = "manual_login_and_verify"
            if arguments.get("force_refresh") and not auth_resolution:
                active_state = {
                    **active_state,
                    "browser_checkpoint": {},
                    "parser_result": {},
                    "session_name": None,
                    "auth_status": "unknown",
                    "auth_facts": {"state": "unknown", "authenticated": None, "verification_state": "unverified"},
                    "auth_resolution_attempts": 0,
                    "browser_operation_mode": "explore",
                    "browser_required_action": None,
                    "no_progress_streaks": {**dict(active_state.get("no_progress_streaks") or {}), "browser": 0},
                }
                feedback["force_refresh"] = True
            active_state = {**active_state, "browser_feedback": feedback}
            previous_parser = active_state.get("parser_result") if isinstance(active_state.get("parser_result"), dict) else {}
            previous_validation = _current_parser_validation(active_state) if previous_parser else {}
            previous_error = active_state.get("error_info", {})
            previous_parser_state = {
                "page_type": active_state.get("page_type"),
                "data_source": active_state.get("data_source"),
                "page_metadata": active_state.get("page_metadata", {}),
                "parser_confidence": active_state.get("parser_confidence"),
                "interaction_plan": active_state.get("interaction_plan", []),
                "session_name": active_state.get("session_name"),
                "auth_status": active_state.get("auth_status"),
                "auth_facts": active_state.get("auth_facts", {}),
                "code_entry_mode": active_state.get("code_entry_mode", "probe"),
            }
            active_state = run_browser_capability(active_state)
            if auth_resolution and previous_parser:
                auth_candidate = active_state.get("parser_result") if isinstance(active_state.get("parser_result"), dict) else {}
                merged_parser = dict(previous_parser)
                merged_parser["auth"] = dict(auth_candidate.get("auth") or {})
                resolved_auth_state = str((active_state.get("auth_facts") or {}).get("state") or "unknown")
                if resolved_auth_state == "verified" and str(merged_parser.get("page_type") or "") == "auth_required":
                    merged_parser["page_type"] = str(auth_candidate.get("page_type") or "dynamic")
                if auth_candidate.get("_checkpoint"):
                    merged_parser["_checkpoint"] = auth_candidate.get("_checkpoint")
                merged_parser["_auth_resolution"] = {
                    "analysis_summary": auth_candidate.get("analysis_summary"),
                    "confidence": auth_candidate.get("confidence"),
                    "page_type": auth_candidate.get("page_type"),
                }
                active_state = {
                    **active_state,
                    "parser_result": merged_parser,
                    "page_type": merged_parser.get("page_type") or previous_parser_state.get("page_type"),
                    "data_source": previous_parser_state.get("data_source"),
                    "page_metadata": previous_parser_state.get("page_metadata", {}),
                    "parser_confidence": previous_parser_state.get("parser_confidence"),
                    "interaction_plan": previous_parser_state.get("interaction_plan", []),
                    "code_entry_mode": previous_parser_state.get("code_entry_mode", "probe"),
                }
            active_state = validate_parser(active_state)
            current_validation = _current_parser_validation(active_state)
            active_state = {
                **active_state,
                "browser_operation_mode": "explore",
                "browser_required_action": None,
            }
            if previous_validation.get("can_enter_code") and not current_validation.get("can_enter_code") and not auth_resolution:
                failed_attempt = active_state.get("parser_result", {})
                active_state = {
                    **active_state,
                    "browser_attempt_result": failed_attempt,
                    "parser_result": previous_parser,
                    "parser_valid": True,
                    "error_info": previous_error,
                    **previous_parser_state,
                }
                _append_log(active_state, "WARNING", "run_browser", "本轮 Browser 结果不可用，保留上一轮 Parser", status="degraded")

        elif capability in {"run_code", "recheck_code"}:
            validation = _current_parser_validation(active_state)
            if not active_state.get("target_url"):
                return capability_result(capability, ok=False, error="task_spec_required")
            code_streak = int((active_state.get("no_progress_streaks") or {}).get("code", 0) or 0)
            if code_streak >= 2 and capability != "recheck_code":
                return capability_result(capability, ok=False, error="code_no_progress_limit_reached")
            active_state = {**active_state, "code_entry_mode": str(validation.get("code_entry_mode") or active_state.get("code_entry_mode") or "probe"), "auth_facts": validation.get("auth_facts") or active_state.get("auth_facts", {})}
            before_code_progress = progress_snapshot(active_state)
            existing_success = _effective_execution_result(active_state)
            force_recheck = capability == "recheck_code"
            recheck_reason = str(arguments.get("recheck_reason") or "").strip()
            if force_recheck and not recheck_reason:
                return capability_result(capability, ok=False, error="recheck_reason_required")
            if force_recheck and not _successful_execution(existing_success):
                return capability_result(capability, ok=False, error="successful_artifact_required")
            if (
                force_recheck
                and bool(arguments.get("accept_smaller_result"))
                and not str(arguments.get("replacement_reason") or "").strip()
            ):
                return capability_result(capability, ok=False, error="replacement_reason_required")
            if _successful_execution(existing_success) and not force_recheck:
                _append_log(
                    active_state,
                    "INFO",
                    "run_code",
                    "已有成功产物，普通 Code 调用已跳过",
                    status="skipped",
                    items=existing_success.get("items_count"),
                    selected_run=((existing_success.get("artifact_version") or {}).get("run_id") if isinstance(existing_success.get("artifact_version"), dict) else None),
                )
                return capability_result(capability, validation=validation, include_evidence=True)
            if str(active_state.get("code_entry_mode") or "probe") == "probe":
                _append_log(
                    active_state, "INFO", "run_code", "进入有界访问探针，不生成完整爬虫",
                    status="advisory", execution_mode="probe",
                    authentication_state=(active_state.get("auth_facts") or {}).get("state"),
                    warning_codes=list(dict.fromkeys(str(v) for v in (validation.get("warnings") or []) if v)),
                )
            if int(active_state.get("retry_count", 0) or 0) >= int(active_state.get("max_retries", DEFAULT_MAX_RETRIES) or DEFAULT_MAX_RETRIES) and active_state.get("execution_attempted"):
                return capability_result(capability, ok=False, error="execution_retry_budget_exhausted")
            repair_focus = str(arguments.get("repair_focus") or "").strip()
            if repair_focus:
                error_info = dict(active_state.get("error_info") or {})
                error_info["supervisor_repair_focus"] = repair_focus[:2000]
                active_state = {**active_state, "error_info": error_info}
            previous_success = _effective_execution_result(active_state)
            artifact_snapshot = (
                _backup_successful_artifact(previous_success, str(active_state.get("thread_id") or "task"))
                if _successful_execution(previous_success) and force_recheck else {}
            )
            active_state = {
                **active_state,
                "pending_recheck_policy": {
                    "force_recheck": force_recheck,
                    "recheck_reason": recheck_reason,
                    "accept_smaller_result": bool(arguments.get("accept_smaller_result")),
                    "replacement_reason": str(arguments.get("replacement_reason") or "").strip(),
                },
                "accepted_artifact_snapshot": artifact_snapshot,
            }
            active_state = run_code_capability(active_state)
            active_state = evaluate_execution(active_state)
            active_state = _record_capability_progress(active_state, "code", before_code_progress)

        elif capability == "finalize_task":
            reason = str(arguments.get("reason") or "").strip()
            effective_execution = _effective_execution_result(active_state)
            already_successful = bool(
                effective_execution.get("success")
                and _safe_int(effective_execution.get("items_count"), 0) > 0
            )
            if reason and not already_successful and not (active_state.get("error_info") or {}).get("error_message"):
                active_state = {
                    **active_state,
                    "error_info": {
                        "error_type": "supervisor_finalized",
                        "error_message": reason[:2000],
                        "suggested_fix": "查看 Agent 能力调用和运行日志。",
                    },
                }
            active_state = {
                **finalize(active_state),
                "terminal_finalized": True,
                "resumable": False,
            }

        else:
            return capability_result(capability, ok=False, error=f"unknown_supervisor_capability:{capability}")

        capability_history.append(capability)
        active_state = {**active_state, "capability_history": capability_history[-80:]}
        actions = _recommended_actions(active_state)
        _save_pipeline_checkpoint(active_state, capability, recommended_actions=actions)
        return capability_result(
            capability,
            validation=_current_parser_validation(active_state) if capability in {"run_browser", "resolve_authentication"} else None,
        )

    from pi_browser_runtime import run_pi_supervisor_agent

    log_event(
        _supervisor_log, "agent.start", status="started", agent="supervisor",
        phase="pipeline", runtime="pi-agent-core", build=SUPERVISOR_AGENT_BUILD,
        resumed=bool(restored), mode="native_capabilities",
    )
    try:
        timeout_seconds = max(
            300,
            min(int(os.getenv("PI_SUPERVISOR_PIPELINE_TIMEOUT_SECONDS", "14400")), 14400),
        )
    except Exception:
        timeout_seconds = 14400
    try:
        max_tools = max(12, min(int(os.getenv("PI_SUPERVISOR_MAX_TOOLS", "32")), 80))
    except Exception:
        max_tools = 32
    try:
        max_turns = max(max_tools + 4, min(int(os.getenv("PI_SUPERVISOR_MAX_TURNS", "48")), 80))
    except Exception:
        max_turns = 48

    summary = _state_summary(active_state)
    actions = _recommended_actions(active_state)
    if restored:
        native_user_prompt = (
            "这是一个从持久化 transcript 和任务 checkpoint 恢复的爬取任务。"
            "请基于已有推理和证据继续，只执行当前最有价值的能力。\n\n"
            f"原始用户需求：\n{user_request}\n\n"
            f"当前状态：{json_dumps(summary, indent=None)}\n"
            f"建议能力（仅供参考，不是固定路由）：{actions}"
        )
    else:
        native_user_prompt = (
            "自主完成下面的爬取任务。先确认任务规格，然后根据证据选择能力；"
            "RAG 可跳过，Browser/Code 可按需重试。\n\n"
            f"用户需求：\n{user_request}"
        )

    def persist_native_transcript(event: Dict[str, Any]) -> None:
        nonlocal active_state
        transcript = _safe_transcript(event.get("messages"))
        if not transcript:
            return
        active_state = {**active_state, "agent_transcript": transcript}
        _save_pipeline_checkpoint(
            active_state,
            capability_history[-1] if capability_history else "agent_turn",
            agent_transcript=transcript,
            recommended_actions=(
                event.get("recommended_actions")
                if isinstance(event.get("recommended_actions"), list)
                else _recommended_actions(active_state)
            ),
        )

    result = run_pi_supervisor_agent(
        system_prompt=SUPERVISOR_NATIVE_PROMPT,
        user_prompt=native_user_prompt,
        tool_handler=handle_capability_tool,
        max_turns=max_turns,
        max_tools=max_tools,
        timeout_seconds=timeout_seconds,
        initial_messages=initial_transcript,
        state_summary=summary,
        recommended_actions=actions,
        thinking_level=os.getenv("PI_SUPERVISOR_THINKING_LEVEL", "low"),
        checkpoint_handler=persist_native_transcript,
    )

    transcript = _safe_transcript(result.get("transcript"))
    active_state = {**active_state, "agent_transcript": transcript}
    _save_pipeline_checkpoint(
        active_state,
        capability_history[-1] if capability_history else "agent_session",
        agent_transcript=transcript,
        recommended_actions=_recommended_actions(active_state),
    )

    if not active_state.get("final_output"):
        if not (active_state.get("error_info") or {}).get("error_type"):
            active_state = {
                **active_state,
                "error_info": {
                    "error_type": "supervisor_native_loop_incomplete",
                    "error_message": str(result.get("error") or result.get("stop_reason") or "Agent 未完成 finalize_task"),
                    "suggested_fix": "恢复同一 thread_id；pi-agent-core 将从 transcript 和任务 checkpoint 继续。",
                },
            }
        active_state = finalize(active_state)
        fallback_success = (active_state.get("final_output") or {}).get("status") == "success"
        active_state = {
            **active_state,
            "terminal_finalized": bool(fallback_success),
            "resumable": not fallback_success,
        }
        _save_pipeline_checkpoint(
            active_state,
            "native_loop_interrupted" if not fallback_success else "verified_success_finalize",
            agent_transcript=transcript,
            recommended_actions=([] if fallback_success else _recommended_actions({**active_state, "final_output": {}})),
        )

    active_state["supervisor_runtime"] = {
        "runtime": "pi-agent-core",
        "build": SUPERVISOR_AGENT_BUILD,
        "mode": "native_capabilities",
        "capability_history": capability_history,
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", []),
        "stop_reason": result.get("stop_reason"),
        "error": result.get("error"),
        "checkpoint_resumed": bool(restored),
        "transcript_messages": len(transcript),
    }
    final_success = (active_state.get("final_output") or {}).get("status") == "success"
    runtime_status = "terminated" if result.get("error") else "success"
    supervisor_status = (
        "success_with_warnings" if final_success and runtime_status != "success"
        else "success" if final_success else "failed"
    )
    log_event(
        _supervisor_log, "agent.finish",
        level="INFO" if final_success else "WARNING",
        status=supervisor_status, agent="supervisor", phase="pipeline", runtime="pi-agent-core",
        runtime_status=runtime_status, artifact_status="success" if final_success else "failed",
        review_status="accepted" if final_success else "rejected",
        terminal_reason=result.get("error") or result.get("stop_reason"),
        mode="native_capabilities", turns=result.get("turns", 0),
        capability_calls=len(result.get("tool_calls", [])), capability_counts=active_state.get("capability_counts", {}),
        error_type="supervisor_runtime_error" if (result.get("error") and not final_success) else None,
        reason=result.get("error") if (result.get("error") and not final_success) else None,
    )
    return active_state
