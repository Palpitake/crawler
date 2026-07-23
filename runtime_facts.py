"""Shared runtime facts for autonomous crawler agents.

This module deliberately separates observable facts from agent decisions:
- authentication facts describe what was observed, not whether a crawl should continue;
- endpoint provenance distinguishes observed evidence from hypotheses;
- runtime failures preserve root cause and terminal symptom independently;
- progress scoring prevents repeated capabilities that do not change task facts.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

AUTH_STATES = {
    "unknown", "anonymous", "not_required", "required", "challenge",
    "provisional", "verified", "rejected", "stale",
}

ERROR_CATEGORY_BY_ROOT = {
    "dependency_error": "dependency",
    "import_error": "dependency",
    "syntax_error": "code",
    "authentication_required": "authentication",
    "authentication_unverified": "authentication",
    "challenge_required": "authentication",
    "captcha_required": "authentication",
    "mfa_required": "authentication",
    "rate_limited": "access",
    "access_denied": "access",
    "blocked_by_site_policy": "access",
    "service_unavailable": "service",
    "network_error": "network",
    "timeout": "network",
    "api_contract_error": "parser",
    "parser_error": "parser",
    "pagination_error": "parser",
    "crawler_runtime_error": "code",
    "tool_budget_exhausted": "budget",
    "empty_data": "data",
}

SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "authorization", "key", "apikey",
    "api_key", "secret", "signature", "sign", "xsec_token", "pcdk",
    "spmtag", "spm", "session", "sid", "ticket", "code",
}


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "verified", "authenticated"}:
        return True
    if text in {"0", "false", "no", "n", "rejected", "anonymous"}:
        return False
    return None


def sanitize_url(url: str) -> str:
    """Redact sensitive/long tracking query values while preserving routing facts."""
    try:
        parsed = urlparse(str(url or ""))
        if not parsed.scheme or not parsed.netloc:
            return str(url or "")
        pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            redact = (
                lowered in SENSITIVE_QUERY_KEYS
                or any(token in lowered for token in ("token", "secret", "signature", "ticket", "session"))
                or len(value) > 64
            )
            pairs.append((key, "***" if redact else value))
        return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))
    except Exception:
        return str(url or "")


def page_partition(current_url: str, target_url: str, *, after_login: bool = False) -> str:
    """Classify evidence by page context so login/challenge traffic cannot pollute target evidence."""
    current = str(current_url or "")
    target = str(target_url or "")
    try:
        cp = urlparse(current)
        tp = urlparse(target)
        path = (cp.path or "").lower()
        query = (cp.query or "").lower()
        auth_like = bool(re.search(r"(?:^|[/._-])(login|signin|sign-in|auth|passport)(?:[/._?&#-]|$)", path))
        challenge_like = bool(re.search(r"captcha|verify|challenge|risk|safe|security|滑块|验证", f"{path}?{query}", re.I))
        if challenge_like:
            return "challenge"
        if auth_like:
            return "login"
        same_host = bool(cp.hostname and tp.hostname and cp.hostname.lower() == tp.hostname.lower())
        same_path = (cp.path or "").rstrip("/") == (tp.path or "").rstrip("/")
        if same_host and same_path:
            return "target_post_login" if after_login else "target_pre_login"
        if same_host:
            return "same_site_post_login" if after_login else "same_site_pre_login"
        return "external_or_redirect"
    except Exception:
        return "unknown"


def normalize_auth_facts(
    parser: Mapping[str, Any],
    pipeline_info: Mapping[str, Any] | None = None,
    previous: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Normalize Browser AI auth claims and raw phase facts without inventing success."""
    pipeline_info = pipeline_info or {}
    previous = previous or {}
    auth = parser.get("auth") if isinstance(parser.get("auth"), Mapping) else {}
    phases = pipeline_info.get("phases") if isinstance(pipeline_info.get("phases"), Mapping) else {}
    login_phase = phases.get("login") if isinstance(phases.get("login"), Mapping) else {}

    claimed_state = str(
        auth.get("authentication_state")
        or auth.get("state")
        or ""
    ).strip().lower()
    authenticated = _bool_or_none(auth.get("authenticated"))
    page_type = str(parser.get("page_type") or "unknown").lower()
    challenge = bool(
        auth.get("challenge_detected")
        or auth.get("captcha_detected")
        or auth.get("mfa_required")
        or login_phase.get("challenge_detected")
    )
    manual_attempted = bool(login_phase.get("attempted") or auth.get("manual_login_attempted"))
    verification_state = str(
        auth.get("verification_state")
        or login_phase.get("verification_state")
        or ""
    ).strip().lower()

    if claimed_state not in AUTH_STATES:
        claimed_state = ""
    if challenge:
        state = "challenge"
    elif claimed_state == "verified" or authenticated is True or verification_state in {"verified", "success"}:
        # A post-login probe explicitly verified by the Browser AI supersedes
        # a stale pre-login page_type=auth_required carried by the Parser.
        state = "verified"
    elif page_type == "auth_required" or auth.get("auth_required"):
        state = "required"
    elif claimed_state:
        state = claimed_state
    elif manual_attempted and verification_state not in {"verified", "success"}:
        state = "provisional"
    elif authenticated is True or verification_state in {"verified", "success"}:
        state = "verified"
    elif authenticated is False:
        state = "anonymous"
    elif previous.get("state") in AUTH_STATES:
        state = str(previous.get("state"))
    else:
        state = "unknown"

    if state == "verified":
        authenticated = True
    elif state in {"required", "challenge", "anonymous", "rejected", "stale"}:
        authenticated = False

    return {
        "state": state,
        "authenticated": authenticated,
        "auth_check_status": "completed" if parser else "not_run",
        "verification_state": verification_state or ("verified" if state == "verified" else "unverified"),
        "manual_login_attempted": manual_attempted,
        "challenge_detected": challenge,
        "session_name": auth.get("session_name") or login_phase.get("session_name") or previous.get("session_name"),
        "login_url": auth.get("login_url"),
        "probe": auth.get("probe") or login_phase.get("post_login_probe") or login_phase.get("resume_probe") or {},
        "source": "browser_ai",
    }


def endpoint_provenance(parser: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return endpoint candidates with explicit provenance and verification facts."""
    result: List[Dict[str, Any]] = []
    agent = parser.get("_agent") if isinstance(parser.get("_agent"), Mapping) else {}
    contract = agent.get("evidence_contract") if isinstance(agent.get("evidence_contract"), Mapping) else {}
    host_item_evidence = bool(contract.get("item_evidence_complete"))
    for endpoint in parser.get("api_endpoints") or []:
        if not isinstance(endpoint, Mapping) or not endpoint.get("url"):
            continue
        source = str(endpoint.get("source") or endpoint.get("provenance") or "").strip().lower()
        if source not in {"observed", "historical", "hypothesized"}:
            source = "observed" if (
                endpoint.get("observed_url")
                or endpoint.get("evidence_id")
                or endpoint.get("response_index") is not None
                or host_item_evidence
            ) else "hypothesized"
        verified = _bool_or_none(endpoint.get("verified"))
        if verified is None:
            verified = bool(source == "observed" and (endpoint.get("data_path") or host_item_evidence))
        item = dict(endpoint)
        item["source"] = source
        item["verified"] = verified
        result.append(item)
    return result


def code_entry_mode(parser: Mapping[str, Any], auth_facts: Mapping[str, Any]) -> str:
    """Choose full implementation vs bounded discovery probe from objective readiness facts."""
    endpoints = endpoint_provenance(parser)
    verified_endpoint = any(item.get("verified") and item.get("source") in {"observed", "historical"} for item in endpoints)
    explicit_dom = bool(parser.get("selectors") or parser.get("fields")) and str(parser.get("data_source") or "") in {"dom", "mixed", "iframe"}
    unresolved_auth = str(auth_facts.get("state") or "unknown") in {"required", "challenge", "provisional"}
    if unresolved_auth:
        return "probe"
    if verified_endpoint or explicit_dom:
        return "full"
    return "probe"


_ERROR_RULES = [
    ("dependency_error", re.compile(r"modulenotfounderror|importerror|no module named|cannot import name", re.I)),
    ("challenge_required", re.compile(r"captcha|滑块|验证码|安全验证|risk.?control|challenge", re.I)),
    ("authentication_required", re.compile(r"login required|sign.?in required|需要登录|登录后|未登录|authentication required", re.I)),
    ("rate_limited", re.compile(r"rate.?limit|too many requests|http\s*429|ip.?限流|频繁|访问过于频繁", re.I)),
    ("access_denied", re.compile(r"no access|access denied|forbidden|http\s*403|拒绝访问|无权访问|权限不足", re.I)),
    ("service_unavailable", re.compile(r"system busy|系统繁忙|service unavailable|http\s*503|temporarily unavailable", re.I)),
    ("timeout", re.compile(r"timed?\s*out|timeout|超时", re.I)),
    ("network_error", re.compile(r"connection reset|connection refused|dns|network is unreachable|网络错误|连接失败", re.I)),
    ("syntax_error", re.compile(r"syntaxerror", re.I)),
    ("api_contract_error", re.compile(r"jsondecodeerror|unexpected content.?type|schema|data_path|接口结构|返回结构", re.I)),
    ("tool_budget_exhausted", re.compile(r"tool_budget_exhausted|tool budget exhausted|turn_budget_exhausted", re.I)),
    ("crawler_runtime_error", re.compile(r"traceback \(most recent call last\)|\b[a-z_][\w.]*error:\s", re.I)),
]


def classify_runtime_failure(
    texts: Iterable[Any],
    *,
    fallback_root: str = "empty_data",
    terminal: str = "empty_data",
) -> Dict[str, Any]:
    combined = "\n".join(str(value or "") for value in texts)
    root = ""
    for candidate, pattern in _ERROR_RULES:
        if pattern.search(combined):
            root = candidate
            break
    root = root or fallback_root or terminal
    category = ERROR_CATEGORY_BY_ROOT.get(root, "unknown")
    return {
        "root_error_type": root,
        "terminal_error_type": terminal,
        "error_category": category,
        "retry_strategy": {
            "dependency": "repair_code",
            "code": "repair_code",
            "authentication": "resolve_authentication",
            "access": "stop_or_change_access_context",
            "service": "bounded_retry_later",
            "network": "bounded_network_probe",
            "parser": "collect_new_evidence",
            "budget": "resume_from_checkpoint_with_focus",
            "data": "inspect_upstream_facts",
        }.get(category, "inspect_facts"),
        "diagnostic_excerpt": combined[-1600:],
    }


def progress_snapshot(state: Mapping[str, Any]) -> Dict[str, Any]:
    parser = state.get("parser_result") if isinstance(state.get("parser_result"), Mapping) else {}
    checkpoint = state.get("browser_checkpoint") if isinstance(state.get("browser_checkpoint"), Mapping) else {}
    evidence = checkpoint.get("evidence") if isinstance(checkpoint.get("evidence"), Mapping) else {}
    material = checkpoint.get("material") if isinstance(checkpoint.get("material"), Mapping) else {}
    execution = state.get("execution_result") if isinstance(state.get("execution_result"), Mapping) else {}
    auth = state.get("auth_facts") if isinstance(state.get("auth_facts"), Mapping) else {}
    endpoints = endpoint_provenance(parser)
    return {
        "item_ids": len(evidence.get("item_ids") or []),
        "response_bodies": len(material.get("response_bodies") or []),
        "verified_endpoints": sum(1 for item in endpoints if item.get("verified")),
        "observed_endpoints": sum(1 for item in endpoints if item.get("source") == "observed"),
        "auth_state": auth.get("state") or state.get("auth_status") or "unknown",
        "output_items": int(execution.get("observed_items_count") or execution.get("items_count") or 0),
        "root_error_type": execution.get("root_error_type") or (state.get("error_info") or {}).get("root_error_type") or (state.get("error_info") or {}).get("error_type"),
    }


def progress_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    score = 0
    changes: Dict[str, Any] = {}
    weights = {
        "item_ids": 4,
        "response_bodies": 1,
        "verified_endpoints": 5,
        "observed_endpoints": 2,
        "output_items": 8,
    }
    for key, weight in weights.items():
        old = int(before.get(key) or 0)
        new = int(after.get(key) or 0)
        delta = new - old
        if delta:
            changes[key] = delta
            if delta > 0:
                score += weight
    if before.get("auth_state") != after.get("auth_state"):
        changes["auth_state"] = [before.get("auth_state"), after.get("auth_state")]
        if after.get("auth_state") == "verified":
            score += 8
        elif after.get("auth_state") in {"required", "challenge"}:
            score += 2  # new root-cause fact is still useful progress
    if before.get("root_error_type") != after.get("root_error_type"):
        changes["root_error_type"] = [before.get("root_error_type"), after.get("root_error_type")]
        score += 1
    return {"score": score, "changes": changes, "stalled": score <= 0}


def stable_code(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
