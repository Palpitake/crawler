from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .models import FailureCandidate, MemoryCandidate
from .normalizer import site_family


def build_cards(candidates: Iterable[MemoryCandidate], failures: Iterable[FailureCandidate], *, top_k: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    selected = [item for item in candidates if item.final_score >= 0.30][:top_k]
    failure_values = [item for item in failures if item.match_score >= 0.35][:5]
    supervisor = [_supervisor_card(item) for item in selected[:5]]
    browser = [
        _browser_card(item)
        for item in selected
        if item.memory_type in {"site", "strategy", "endpoint", "authentication"}
    ][:5]
    code = [_code_card(item) for item in selected if item.memory_type in {"strategy", "endpoint"}][:6]
    failure_cards = [_failure_card(item) for item in failure_values]
    return {"supervisor": supervisor, "browser": browser, "code": code, "failures": failure_cards}


def _base(item: MemoryCandidate) -> Dict[str, Any]:
    return {
        "memory_id": item.id,
        "memory_key": item.memory_key,
        "memory_type": item.memory_type,
        "domain": item.domain,
        "site_family": site_family(item.domain),
        "route_template": item.route_template,
        "match_score": item.final_score,
        "match_reason": item.match_reasons,
        "reliability": item.reliability_score,
        "confidence": item.confidence_score,
        "status": item.status,
        "source": item.source_kind,
        "fresh_until": item.fresh_until,
        "requires_validation": item.requires_validation,
        "summary": item.summary[:500],
    }


def _supervisor_card(item: MemoryCandidate) -> Dict[str, Any]:
    facts = item.facts or {}
    card = _base(item)
    card.update({
        "recommended_capability": _recommended_capability(item),
        "known_risks": facts.get("known_risks") or facts.get("known_root_errors") or [],
        "authentication_state": facts.get("authentication_state") or "unknown",
        "data_source": item.data_source,
        "endpoint_hints": facts.get("endpoint_hints") or facts.get("api_endpoints") or [],
    })
    return card


def _browser_card(item: MemoryCandidate) -> Dict[str, Any]:
    facts = item.facts or {}
    card = _base(item)
    card.update({
        "route_template": item.route_template,
        "authentication_facts": facts.get("authentication_facts") or {"state": facts.get("authentication_state")},
        "interaction_plan": facts.get("interaction_plan") or [],
        "selectors": facts.get("selectors") or {},
        "endpoint_hints": facts.get("endpoint_hints") or facts.get("api_endpoints") or [],
        "validation_required": item.requires_validation,
        "do_not_repeat": facts.get("do_not_repeat") or [],
    })
    return card


def _code_card(item: MemoryCandidate) -> Dict[str, Any]:
    facts = item.facts or {}
    card = _base(item)
    card.update({
        "endpoint_hints": facts.get("endpoint_hints") or facts.get("api_endpoints") or [],
        "request_template": facts.get("request_template") or {},
        "response_signature": facts.get("response_signature") or {},
        "field_mapping": facts.get("field_mapping") or {},
        "pagination_facts": facts.get("pagination_facts") or facts.get("pagination") or {},
        "completion_condition": facts.get("completion_condition") or {},
        "known_failures": facts.get("known_failures") or [],
        "validation_required": item.requires_validation,
    })
    return card


def _failure_card(item: FailureCandidate) -> Dict[str, Any]:
    return {
        "failure_id": item.id,
        "root_error_type": item.root_error_type,
        "terminal_error_type": item.terminal_error_type,
        "error_category": item.error_category,
        "retry_strategy": item.retry_strategy,
        "authentication_state": item.authentication_state,
        "endpoint_family": item.endpoint_family,
        "http_status": item.http_status,
        "block_active": item.block_active,
        "environment_match": item.environment_match,
        "block_until": item.block_until,
        "expires_at": item.expires_at,
        "match_score": item.match_score,
        "summary": item.evidence_summary[:500],
        "required_change_before_retry": (item.facts or {}).get("required_change_before_retry") or [],
    }


def _recommended_capability(item: MemoryCandidate) -> str:
    facts = item.facts or {}
    auth_state = str(facts.get("authentication_state") or "unknown")
    if auth_state in {"required", "challenge", "provisional"}:
        return "resolve_authentication"
    if item.requires_validation:
        return "run_code" if item.memory_type == "endpoint" else "run_browser"
    return "run_code" if item.memory_type == "endpoint" else "run_browser"
