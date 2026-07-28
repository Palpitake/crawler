from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from crawler_agent.core.runtime_facts import endpoint_provenance

from .config import RagConfig
from .feedback import (
    browser_memory_receipts,
    code_memory_receipts,
    merge_memory_receipts,
)
from .normalizer import (
    build_memory_query, memory_key,
    normalize_endpoint_template, normalize_url, site_family,
)


def build_commit_payload(state: Dict[str, Any], config: RagConfig, builds: Dict[str, str]) -> Dict[str, Any]:
    query = build_memory_query(state)
    parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    execution = _effective_execution(state)
    error = state.get("error_info") if isinstance(state.get("error_info"), dict) else {}
    final_output = state.get("final_output") if isinstance(state.get("final_output"), dict) else {}
    auth = state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {}
    success = bool(execution.get("success") and int(execution.get("items_count", 0) or 0) > 0)
    items = int(execution.get("items_count", 0) or 0)
    complete = bool(execution.get("pagination_complete") or _reached_limit(state, items))
    root_for_quality = str(
        error.get("root_error_type") or execution.get("root_error_type")
        or final_output.get("root_error_type") or ""
    )
    if not root_for_quality and not success and str(auth.get("state") or "") in {"required", "challenge", "provisional"}:
        root_for_quality = "challenge_required" if auth.get("state") == "challenge" else "authentication_required"
    quality = _quality(success, complete, parser, auth, execution, root_for_quality)
    now = datetime.now(timezone.utc)
    failure_contexts = _failure_contexts(
        state, success=success, complete=complete, execution=execution,
        error=error, final_output=final_output,
    )
    known_root_errors = list(dict.fromkeys(
        str((context.get("error") or {}).get("root_error_type") or context.get("derived_root") or "")
        for context in failure_contexts
        if str((context.get("error") or {}).get("root_error_type") or context.get("derived_root") or "")
    ))

    endpoints = endpoint_provenance(parser)
    verified_endpoints = [item for item in endpoints if item.get("verified")]
    source_kind = "observed" if verified_endpoints else "historical" if endpoints else "hypothesized"
    data_source = str(parser.get("data_source") or state.get("data_source") or "unknown")
    pagination = parser.get("pagination_contract") if isinstance(parser.get("pagination_contract"), dict) else parser.get("pagination") or {}
    pagination_type = str((pagination or {}).get("type") or (pagination or {}).get("pagination_type") or "unknown")
    common_facts = {
        "target_url": normalize_url(str(state.get("target_url") or "")),
        "canonical_fields": query.canonical_fields,
        "requested_fields": list(state.get("target_fields") or []),
        "scope_type": query.scope_type,
        "max_items": query.max_items,
        "authentication_state": query.authentication_state,
        "authentication_facts": _safe_auth_facts(auth),
        "data_source": data_source,
        "pagination_type": pagination_type,
        "pagination_facts": pagination,
        "selectors": parser.get("selectors") or {},
        "interaction_plan": parser.get("interaction_plan") or [],
        "endpoint_hints": [_endpoint_card(item) for item in endpoints[:12]],
        "field_mapping": _field_mapping(parser, query.canonical_fields),
        "completion_condition": {
            "pagination_complete": complete,
            "max_items": query.max_items,
            "items": items,
        },
        "runtime_pagination": _runtime_pagination_facts(execution),
        "known_root_errors": known_root_errors,
        "known_failures": [
            {
                "root_error_type": str((context.get("error") or {}).get("root_error_type") or context.get("derived_root") or ""),
                "terminal_error_type": str((context.get("error") or {}).get("terminal_error_type") or ""),
                "error_category": str((context.get("error") or {}).get("error_category") or ""),
                "retry_strategy": str((context.get("error") or {}).get("retry_strategy") or ""),
            }
            for context in failure_contexts
        ],
        "quality": quality,
    }

    memories: List[Dict[str, Any]] = []
    endpoint_rows: List[Dict[str, Any]] = []

    # Site memory is useful even when the run ended in an environment failure.
    site_key = memory_key("site", query.domain, query.route_template)
    memories.append(_memory_row(
        key=site_key, memory_type="site", source_kind="observed" if parser else "historical",
        query=query, data_source=data_source,
        summary=f"{query.domain} {query.route_template} 的 {query.task_type} 页面事实",
        facts={
            **common_facts,
            "known_risks": _known_risks(error, auth),
            "page_type": parser.get("page_type") or state.get("page_type") or "unknown",
        },
        quality=quality, success=success, complete=complete,
        confidence=float(parser.get("confidence") or 0.5),
        fresh_until=now + timedelta(days=7 if query.authentication_state in {"required", "challenge"} else 30),
        config=config, builds=builds,
    ))

    if query.authentication_state not in {"", "unknown"}:
        auth_key = memory_key("authentication", query.domain, query.route_template, query.authentication_state)
        memories.append(_memory_row(
            key=auth_key, memory_type="authentication", source_kind="observed",
            query=query, data_source=data_source,
            summary=f"{query.domain} 认证状态：{query.authentication_state}",
            facts={
                "authentication_state": query.authentication_state,
                "authentication_facts": _safe_auth_facts(auth),
                "route_template": query.route_template,
                "task_type": query.task_type,
                "known_risks": _known_risks(error, auth),
                "stable_browser_fingerprint_required": bool(query.authentication_state in {"required", "challenge", "provisional", "verified"}),
            },
            quality=quality, success=success, complete=complete,
            confidence=float(parser.get("confidence") or 0.5),
            reliability=0.78 if query.authentication_state == "verified" else 0.62,
            fresh_until=now + timedelta(days=7),
            config=config, builds=builds,
        ))

    if success:
        strategy_key = memory_key(
            "strategy", query.domain, query.route_template, query.task_type,
            data_source, pagination_type, ",".join(sorted(query.canonical_fields)),
        )
        reliability = 0.86 if quality == "verified_success" else 0.58
        memories.append(_memory_row(
            key=strategy_key, memory_type="strategy", source_kind=source_kind,
            query=query, data_source=data_source,
            summary=f"{query.domain} {query.task_type}：{data_source}/{pagination_type}，最近获得 {items} 条",
            facts=common_facts, quality=quality, success=True, complete=complete,
            confidence=float(parser.get("confidence") or 0.5), reliability=reliability,
            fresh_until=now + timedelta(days=30 if source_kind == "observed" else 7),
            config=config, builds=builds,
        ))
        for endpoint in endpoints:
            endpoint_url = str(endpoint.get("url") or endpoint.get("endpoint") or "")
            if not endpoint_url:
                continue
            template, family = normalize_endpoint_template(endpoint_url)
            endpoint_key = memory_key("endpoint", family, endpoint.get("method") or "GET", pagination_type)
            endpoint_memory = _memory_row(
                key=endpoint_key, memory_type="endpoint", source_kind=str(endpoint.get("source") or "historical"),
                query=query, data_source="api",
                summary=f"{query.task_type} endpoint {family}",
                facts={
                    **common_facts,
                    "endpoint_hints": [_endpoint_card(endpoint)],
                    "request_template": endpoint.get("request_template") or {},
                    "response_signature": endpoint.get("response_signature") or {},
                },
                quality=quality, success=True, complete=complete,
                confidence=float(parser.get("confidence") or 0.5),
                reliability=0.88 if endpoint.get("verified") else 0.55,
                fresh_until=now + timedelta(days=30 if endpoint.get("verified") else 7),
                config=config, builds=builds,
            )
            memories.append(endpoint_memory)
            endpoint_rows.append({
                "parent_memory_key_hex": endpoint_memory["memory_key_hex"],
                "endpoint_key_hex": endpoint_key.hex(),
                "endpoint_template": template,
                "endpoint_family": family,
                "http_method": str(endpoint.get("method") or "GET").upper(),
                "source_kind": str(endpoint.get("source") or "historical"),
                "verified": bool(endpoint.get("verified")),
                "request_template": _sanitize_fact(endpoint.get("request_template") or {}),
                "response_signature": _sanitize_fact(endpoint.get("response_signature") or {}),
                "field_mapping": _sanitize_fact(common_facts["field_mapping"]),
                "pagination_facts": _sanitize_fact(pagination),
                "authentication_facts": _safe_auth_facts(auth),
                "last_http_status": endpoint.get("http_status"),
                "successful_probes": 1 if endpoint.get("verified") else 0,
                "failed_probes": 0,
                "last_verified_at": now if endpoint.get("verified") else None,
                "fresh_until": now + timedelta(days=30 if endpoint.get("verified") else 7),
            })

    failures = [
        row
        for row in (
            _failure_row(
                state, query,
                context.get("error") if isinstance(context.get("error"), dict) else {},
                context.get("execution") if isinstance(context.get("execution"), dict) else {},
                auth, final_output, str(context.get("derived_root") or ""), now,
            )
            for context in failure_contexts
        )
        if row
    ]
    execution_row = {
        "task_id": str(state.get("thread_id") or state.get("task_id") or ""),
        "domain": query.domain,
        "route_template": query.route_template,
        "task_type": query.task_type,
        "requested_fields": list(state.get("target_fields") or []),
        "canonical_fields": query.canonical_fields,
        "authentication_state": query.authentication_state,
        "final_status": str(final_output.get("status") or ("success" if success else "failed")),
        "root_error_type": str(error.get("root_error_type") or execution.get("root_error_type") or final_output.get("root_error_type") or root_for_quality or ""),
        "terminal_error_type": str(error.get("terminal_error_type") or execution.get("terminal_error_type") or final_output.get("terminal_error_type") or ("parser_unavailable" if not success else "")),
        "retry_strategy": str(error.get("retry_strategy") or execution.get("retry_strategy") or final_output.get("retry_strategy") or ("resolve_authentication" if root_for_quality in {"authentication_required", "challenge_required"} else "")),
        "items": items,
        "pagination_complete": complete,
        "selected_run": str((state.get("selection_state") or {}).get("selected_run") or ""),
        "selected_strategy_id": _selected_strategy_id(state, parser),
        "runtime_facts": _runtime_fact_snapshot(state, parser, execution, quality),
        "metrics": {
            "duration_ms": int((time_value(state, "task_finished_at", now.timestamp()) - time_value(state, "task_started_at", now.timestamp())) * 1000),
            "browser_runs": int(state.get("parser_attempts", 0) or 0),
            "code_runs": int(state.get("code_version", 0) or 0),
        },
        "supervisor_build": builds.get("supervisor", ""),
        "browser_build": builds.get("browser", ""),
        "code_build": builds.get("code", ""),
        "started_at": datetime.fromtimestamp(time_value(state, "task_started_at", now.timestamp()), timezone.utc),
        "finished_at": now,
    }

    usage = _usage_rows(state, quality)
    legacy_record = {
        "url": normalize_url(str(state.get("target_url") or "")),
        "domain": query.domain,
        "route_template": query.route_template,
        "task_type": query.task_type,
        "target_fields": list(state.get("target_fields") or []),
        "canonical_fields": query.canonical_fields,
        "data_source": data_source,
        "success": success,
        "items": items,
        "pagination_complete": complete,
        "quality": quality,
        "root_error_type": execution_row["root_error_type"],
        "created_at": now.isoformat(),
    }
    return {
        "memories": memories,
        "endpoints": endpoint_rows,
        # ``failure`` is retained for repository compatibility.  New
        # repositories consume the complete list so a retained best artifact
        # does not erase a later challenge or pagination failure.
        "failure": failures[0] if failures else None,
        "failures": failures,
        "execution": execution_row,
        "usage": usage,
        "legacy_record": legacy_record,
        "quality": quality,
    }


def _memory_row(*, key: bytes, memory_type: str, source_kind: str, query: Any,
                data_source: str, summary: str, facts: Dict[str, Any], quality: str,
                success: bool, complete: bool, confidence: float, fresh_until: datetime,
                config: RagConfig, builds: Dict[str, str], reliability: Optional[float] = None) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "memory_key_hex": key.hex(), "memory_type": memory_type,
        "status": "active" if quality == "verified_success" or memory_type in {"site", "authentication"} else "stale",
        "source_kind": source_kind, "domain": query.domain, "route_template": query.route_template,
        "route_hash_hex": query.route_hash.hex(), "task_type": query.task_type,
        "entity_type": query.entity_type, "collection_type": query.collection_type,
        "data_source": data_source, "summary": summary,
        "searchable_text": " ".join([summary, query.task_type, query.collection_type, " ".join(query.canonical_fields), query.domain]),
        "facts": _sanitize_fact(facts), "metrics": {"quality": quality, "last_items": facts.get("completion_condition", {}).get("items", 0)},
        "reliability_score": reliability if reliability is not None else (0.82 if success else 0.45),
        "confidence_score": max(0.0, min(confidence, 1.0)),
        "successful_runs": 1 if success else 0, "failed_runs": 0 if success else 1,
        "complete_runs": 1 if success and complete else 0,
        "partial_runs": 1 if success and not complete else 0,
        "last_verified_at": now if success and quality == "verified_success" else None,
        "last_failed_at": None if success else now, "fresh_until": fresh_until,
        "schema_version": config.schema_version, "agent_build": builds.get("supervisor", ""),
    }


def _failure_row(state: Dict[str, Any], query: Any, error: Dict[str, Any], execution: Dict[str, Any], auth: Dict[str, Any], final_output: Dict[str, Any], derived_root: str, now: datetime) -> Optional[Dict[str, Any]]:
    root = str(error.get("root_error_type") or execution.get("root_error_type") or final_output.get("root_error_type") or error.get("error_type") or execution.get("error_type") or derived_root or "")
    if not root:
        return None
    terminal = str(error.get("terminal_error_type") or execution.get("terminal_error_type") or final_output.get("terminal_error_type") or root)
    category = str(error.get("error_category") or execution.get("error_category") or final_output.get("error_category") or "unknown")
    retry = str(error.get("retry_strategy") or execution.get("retry_strategy") or final_output.get("retry_strategy") or ("resolve_authentication" if root in {"authentication_required", "challenge_required"} else "inspect_facts"))
    endpoint_family = ""
    probe = execution.get("probe_result") if isinstance(execution.get("probe_result"), dict) else {}
    observed = probe.get("observed_endpoints") if isinstance(probe.get("observed_endpoints"), list) else []
    if observed:
        _, endpoint_family = normalize_endpoint_template(str(observed[0]))
    if not endpoint_family:
        parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
        parser_endpoints = endpoint_provenance(parser)
        if parser_endpoints:
            _, endpoint_family = normalize_endpoint_template(str(parser_endpoints[0].get("url") or ""))
    ttl_hours = 6 if root in {"rate_limited", "service_unavailable"} else 24 if root in {"access_denied", "authentication_required", "challenge_required"} else 72
    block_until = now + timedelta(hours=ttl_hours) if root in {"rate_limited", "access_denied", "authentication_required", "challenge_required", "service_unavailable"} else None
    required_change = []
    if root in {"authentication_required", "challenge_required"}:
        required_change.append("authentication_state_or_auth_epoch")
    if root in {"rate_limited", "access_denied"}:
        required_change.extend(["block_ttl_expired", "access_context_changed"])
    key = memory_key("failure", query.domain, query.route_template, query.task_type, endpoint_family, root, query.authentication_state, query.environment_fingerprint.hex() if query.environment_fingerprint else "")
    return {
        "failure_key_hex": key.hex(), "domain": query.domain, "route_template": query.route_template,
        "route_hash_hex": query.route_hash.hex(), "task_type": query.task_type,
        "endpoint_family": endpoint_family, "root_error_type": root,
        "terminal_error_type": terminal, "error_category": category,
        "retry_strategy": retry, "authentication_state": query.authentication_state,
        "http_status": _first_http_status(probe),
        "environment_fingerprint_hex": query.environment_fingerprint.hex() if query.environment_fingerprint else ("00" * 32),
        "evidence_summary": _sanitize_text(str(
            error.get("error_message")
            or execution.get("fix_info")
            or execution.get("error_message")
            or root
        ))[:1000],
        "facts": {
            "probe_result": _sanitize_fact(_small_probe(probe)),
            "required_change_before_retry": list(dict.fromkeys(required_change)),
            "authentication_facts": _safe_auth_facts(auth),
            "runtime_pagination": _sanitize_fact(_runtime_pagination_facts(execution)),
            "warning_codes": list(execution.get("warning_codes") or [])[:30],
            "invocation": execution.get("invocation"),
            "selected_run": str((state.get("selection_state") or {}).get("selected_run") or ""),
        },
        "occurrence_count": 1, "last_observed_at": now,
        "block_until": block_until, "expires_at": now + timedelta(hours=max(ttl_hours * 4, 24)),
        "status": "active",
    }


def _quality(success: bool, complete: bool, parser: Dict[str, Any], auth: Dict[str, Any], execution: Dict[str, Any], derived_root: str = "") -> str:
    if not success:
        root = str(execution.get("root_error_type") or derived_root or "")
        return "environment_failure" if root in {"authentication_required", "challenge_required", "rate_limited", "access_denied", "service_unavailable", "network_error"} else "strategy_failure"
    endpoints = endpoint_provenance(parser)
    observed = any(item.get("source") == "observed" and item.get("verified") for item in endpoints)
    review = execution.get("ai_review") if isinstance(execution.get("ai_review"), dict) else {}
    review_ok = review.get("success") is not False
    auth_consistent = str(auth.get("state") or "unknown") not in {"required", "challenge", "provisional"}
    if complete and review_ok and auth_consistent and (observed or str(parser.get("data_source") or "") == "dom"):
        return "verified_success"
    return "partial_success"


def _failure_contexts(
    state: Dict[str, Any],
    *,
    success: bool,
    complete: bool,
    execution: Dict[str, Any],
    error: Dict[str, Any],
    final_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Keep unresolved attempt failures separate from the retained best result."""
    contexts: List[Dict[str, Any]] = []
    failed_attempt = state.get("failed_code_attempt") if isinstance(state.get("failed_code_attempt"), dict) else {}
    if failed_attempt and not bool(failed_attempt.get("success")):
        root = str(
            failed_attempt.get("root_error_type")
            or failed_attempt.get("error_type")
            or "crawler_runtime_error"
        )
        contexts.append({
            "derived_root": root,
            "execution": failed_attempt,
            "error": {
                "root_error_type": root,
                "terminal_error_type": str(failed_attempt.get("terminal_error_type") or root),
                "error_category": str(failed_attempt.get("error_category") or "unknown"),
                "retry_strategy": str(failed_attempt.get("retry_strategy") or "inspect_facts"),
                "error_message": str(
                    failed_attempt.get("fix_info")
                    or failed_attempt.get("runtime_error_message")
                    or root
                ),
            },
        })
    elif not success:
        root = str(
            error.get("root_error_type")
            or execution.get("root_error_type")
            or final_output.get("root_error_type")
            or "crawler_runtime_error"
        )
        contexts.append({
            "derived_root": root,
            "execution": execution,
            "error": {
                **error,
                "root_error_type": root,
                "terminal_error_type": str(
                    error.get("terminal_error_type")
                    or execution.get("terminal_error_type")
                    or root
                ),
                "error_category": str(
                    error.get("error_category")
                    or execution.get("error_category")
                    or "unknown"
                ),
                "retry_strategy": str(
                    error.get("retry_strategy")
                    or execution.get("retry_strategy")
                    or "inspect_facts"
                ),
            },
        })

    if success and not complete:
        pagination = _runtime_pagination_facts(execution)
        stop_reason = str(
            pagination.get("stop_reason")
            or pagination.get("terminal_reason")
            or "pagination_incomplete"
        )
        contexts.append({
            "derived_root": "pagination_incomplete",
            "execution": {
                **execution,
                "root_error_type": "pagination_incomplete",
                "terminal_error_type": stop_reason,
                "error_category": "strategy",
                "retry_strategy": "repair_pagination",
            },
            "error": {
                "root_error_type": "pagination_incomplete",
                "terminal_error_type": stop_reason,
                "error_category": "strategy",
                "retry_strategy": "repair_pagination",
                "error_message": (
                    "Non-empty output retained, but pagination did not reach the "
                    f"requested limit or a verified terminal state: {pagination}"
                ),
            },
        })

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for context in contexts:
        value = context.get("error") if isinstance(context.get("error"), dict) else {}
        key = (
            str(value.get("root_error_type") or context.get("derived_root") or ""),
            str(value.get("terminal_error_type") or ""),
            (context.get("execution") or {}).get("invocation")
            if isinstance(context.get("execution"), dict) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(context)
    return deduped


def _runtime_pagination_facts(execution: Dict[str, Any]) -> Dict[str, Any]:
    """Extract bounded runtime pagination evidence for memory and diagnostics."""
    sources = [
        execution.get("crawl_meta"),
        execution.get("crawl_progress"),
        execution.get("ai_review"),
        execution,
    ]
    keys = (
        "complete", "pagination_complete", "pages", "response_count", "items",
        "unique_ids", "stall_count", "has_more", "last_has_more", "last_cursor",
        "cursor", "cursor_path", "next_cursor", "terminal_path", "terminal_raw",
        "stop_reason", "terminal_reason",
    )
    result: Dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and key not in result:
                result[key] = source.get(key)
    violations = list(execution.get("pagination_violations") or [])
    if violations:
        result["violations"] = [str(value)[:300] for value in violations[:20]]
    warnings = list(execution.get("warning_codes") or [])
    if warnings:
        result["warning_codes"] = [str(value)[:200] for value in warnings[:30]]
    return result


def _memory_usage_signals(parser: Dict[str, Any], execution: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Collect explicit Browser/Code memory adoption declarations."""
    review = execution.get("ai_review") if isinstance(execution.get("ai_review"), dict) else {}
    return merge_memory_receipts([
        browser_memory_receipts(parser),
        code_memory_receipts(review),
    ])


def _usage_rows(state: Dict[str, Any], quality: str) -> List[Dict[str, Any]]:
    views = state.get("rag_memory_views") if isinstance(state.get("rag_memory_views"), dict) else {}
    parser = state.get("parser_result") if isinstance(state.get("parser_result"), dict) else {}
    execution = _effective_execution(state)
    query = build_memory_query(state)
    current_source = str(parser.get("data_source") or state.get("data_source") or "unknown")
    current_auth = str(
        ((state.get("auth_facts") or {}).get("state") if isinstance(state.get("auth_facts"), dict) else "")
        or state.get("auth_status") or "unknown"
    )
    current_endpoints = {
        normalize_endpoint_template(str(item.get("url") or ""))[1]
        for item in endpoint_provenance(parser)
        if item.get("url")
    }
    signals = _memory_usage_signals(parser, execution)
    failed_attempt = state.get("failed_code_attempt") if isinstance(state.get("failed_code_attempt"), dict) else {}
    failed_signals = _memory_usage_signals({}, failed_attempt)
    for memory_id, signal in failed_signals.items():
        existing = signals.get(memory_id)
        if not existing:
            signals[memory_id] = signal
            continue
        if signal.get("status") == "rejected":
            existing["status"] = "rejected"
        for key in ("agents", "reasons", "receipt_sources"):
            values = list(existing.get(key) or [])
            for value in signal.get(key) or []:
                if value not in values:
                    values.append(value)
            existing[key] = values
    rows = []
    seen = set()
    rank = 0
    for agent in ("supervisor", "browser", "code"):
        for card in views.get(agent) or []:
            if not isinstance(card, dict):
                continue
            try:
                memory_id = int(card.get("memory_id") or 0)
            except Exception:
                memory_id = 0
            if memory_id <= 0 or memory_id in seen:
                continue
            seen.add(memory_id)
            rank += 1
            signal = signals.get(memory_id) or {}
            explicit_status = str(signal.get("status") or "")
            explicit_agents = list(signal.get("agents") or [])
            receipt_sources = list(signal.get("receipt_sources") or [])
            attributed_agent = (
                "multi_agent" if len(explicit_agents) > 1
                else explicit_agents[0] if explicit_agents
                else agent
            )
            matched = _card_matches_execution(
                card, current_source, current_endpoints,
                query.domain, query.route_template, current_auth,
            )
            if explicit_status == "rejected":
                usage_status = "selected"
                validation_result = "rejected"
                contribution_result = "not_contributed"
                reason = "agent explicitly rejected this memory after current-task validation"
            elif explicit_status == "used":
                usage_status = "selected"
                if quality == "verified_success":
                    validation_result = "validated"
                    contribution_result = "contributed"
                    reason = "agent explicitly used this memory and the final result was verified"
                elif quality == "strategy_failure":
                    validation_result = "rejected"
                    contribution_result = "not_contributed"
                    reason = "agent explicitly used this memory but the strategy failed"
                else:
                    validation_result = "inconclusive"
                    contribution_result = "provisional" if quality == "partial_success" else "not_contributed"
                    reason = f"agent explicitly used this memory but final quality was {quality}"
            elif matched:
                usage_status = "matched"
                validation_result = "not_used"
                contribution_result = "not_used"
                reason = "runtime facts matched the card, but no agent reported using it"
            else:
                usage_status = "provided"
                validation_result = "not_used"
                contribution_result = "not_used"
                reason = "memory card was available but no matching runtime fact or explicit use was recorded"
            rows.append({
                "memory_id": memory_id,
                "agent_name": attributed_agent,
                "stage": "execution",
                "usage_status": usage_status,
                "rank_position": rank,
                "retrieval_score": card.get("match_score"),
                "validation_result": validation_result,
                "contribution_result": contribution_result,
                "reason": reason,
                "details": {
                    "requires_validation": card.get("requires_validation"),
                    "final_quality": quality,
                    "explicit_usage": signal,
                    "explicit_agents": explicit_agents,
                    "receipt_source": (
                        receipt_sources[0] if len(receipt_sources) == 1
                        else "multiple" if receipt_sources
                        else None
                    ),
                    "receipt_sources": receipt_sources,
                    "runtime_fact_match": matched,
                    "current_data_source": current_source,
                    "current_endpoint_families": sorted(current_endpoints),
                },
            })
    return rows


def _card_matches_execution(
    card: Dict[str, Any],
    current_source: str,
    current_endpoints: set[str],
    current_domain: str,
    current_route: str,
    current_auth: str,
) -> bool:
    memory_type = str(card.get("memory_type") or "")
    card_source = str(card.get("data_source") or "")
    card_domain = str(card.get("domain") or "")
    card_route = str(card.get("route_template") or "")
    same_site = bool(
        card_domain and current_domain
        and site_family(card_domain) == site_family(current_domain)
    )
    if memory_type == "site":
        return bool(same_site and (not card_route or card_route == current_route))
    if memory_type == "authentication":
        auth_facts = card.get("authentication_facts") if isinstance(card.get("authentication_facts"), dict) else {}
        card_auth = str(
            card.get("authentication_state")
            or auth_facts.get("state")
            or "unknown"
        )
        return bool(same_site and card_auth in {"unknown", current_auth})
    endpoint_hints = card.get("endpoint_hints") if isinstance(card.get("endpoint_hints"), list) else []
    hint_families = set()
    for hint in endpoint_hints:
        if not isinstance(hint, dict):
            continue
        raw = str(hint.get("url") or hint.get("endpoint") or "")
        if raw:
            hint_families.add(normalize_endpoint_template(raw)[1])
    if hint_families and current_endpoints:
        return bool(hint_families & current_endpoints)
    if memory_type == "strategy" and card_source and card_source != "unknown":
        return card_source == current_source
    return False


def _effective_execution(state: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("best_execution_result", "execution_result", "last_successful_execution_result"):
        value = state.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _reached_limit(state: Dict[str, Any], items: int) -> bool:
    try:
        limit = int(state.get("max_items") or 0)
    except Exception:
        limit = 0
    return bool(limit and items >= limit)


def _endpoint_card(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "url": normalize_url(str(value.get("url") or value.get("endpoint") or ""), keep_semantic_query=True),
        "source": str(value.get("source") or "historical"),
        "verified": bool(value.get("verified")),
        "method": str(value.get("method") or "GET"),
    }


def _field_mapping(parser: Dict[str, Any], canonical_fields: List[str]) -> Dict[str, Any]:
    fields = parser.get("fields") if isinstance(parser.get("fields"), dict) else {}
    return {"canonical_fields": canonical_fields, "parser_fields": fields}


def _safe_auth_facts(auth: Dict[str, Any]) -> Dict[str, Any]:
    return {key: auth.get(key) for key in ("state", "authenticated", "verification_state", "auth_epoch", "challenge_detected") if key in auth}


def _known_risks(error: Dict[str, Any], auth: Dict[str, Any]) -> List[str]:
    values = []
    if auth.get("state") in {"required", "challenge", "provisional"}:
        values.append(f"authentication_{auth.get('state')}")
    if error.get("root_error_type"):
        values.append(str(error.get("root_error_type")))
    return list(dict.fromkeys(values))


def _runtime_fact_snapshot(state: Dict[str, Any], parser: Dict[str, Any], execution: Dict[str, Any], quality: str) -> Dict[str, Any]:
    return {
        "quality": quality,
        "auth_facts": _safe_auth_facts(state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {}),
        "parser": {
            "page_type": parser.get("page_type"), "data_source": parser.get("data_source"),
            "confidence": parser.get("confidence"), "api_endpoints": [_endpoint_card(item) for item in endpoint_provenance(parser)[:12]],
            "pagination": parser.get("pagination_contract") or parser.get("pagination") or {},
        },
        "execution": {
            "success": execution.get("success"), "items_count": execution.get("items_count"),
            "pagination_complete": execution.get("pagination_complete"),
            "root_error_type": execution.get("root_error_type"),
            "terminal_error_type": execution.get("terminal_error_type"),
        },
        "progress_history": list(state.get("progress_history") or [])[-10:],
    }


def _small_probe(value: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: value.get(key) for key in ("completed", "reachable", "target_data_observed", "root_error_type", "http_statuses", "content_types", "observed_endpoints", "recommended_next") if key in value}
    if isinstance(result.get("observed_endpoints"), list):
        result["observed_endpoints"] = [normalize_url(str(item), keep_semantic_query=True) for item in result["observed_endpoints"][:12]]
    return result


def _first_http_status(probe: Dict[str, Any]) -> Optional[int]:
    values = probe.get("http_statuses") if isinstance(probe.get("http_statuses"), list) else []
    for value in values:
        try:
            return int(value)
        except Exception:
            continue
    return None




_SENSITIVE_FACT_KEYS = {
    "cookie", "cookies", "set-cookie", "authorization", "proxy-authorization",
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "api_key", "apikey", "storage_state", "storage_state_path", "auth_state_path",
}


def _sanitize_fact(value: Any, key: str = "") -> Any:
    lowered = str(key or "").lower().replace("-", "_")
    if lowered in {item.replace("-", "_") for item in _SENSITIVE_FACT_KEYS} or any(
        token in lowered for token in ("password", "secret", "authorization", "cookie", "access_token", "refresh_token")
    ):
        return "***"
    if isinstance(value, dict):
        return {str(k): _sanitize_fact(v, str(k)) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_sanitize_fact(item, key) for item in list(value)[:200]]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def _sanitize_text(value: str) -> str:
    import re
    text = str(value or "")
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-+/=]+", "Bearer ***", text, flags=re.I)
    text = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)[=:]\s*[^\s,;]+", r"\1=***", text)
    if "http://" in text or "https://" in text:
        text = re.sub(r"https?://[^\s\"'<>]+", lambda match: normalize_url(match.group(0), keep_semantic_query=True), text)
    return text

def _selected_strategy_id(state: Dict[str, Any], parser: Dict[str, Any]) -> Optional[int]:
    views = state.get("rag_memory_views") if isinstance(state.get("rag_memory_views"), dict) else {}
    signals = _memory_usage_signals(parser, _effective_execution(state))
    for card in views.get("supervisor") or []:
        if not isinstance(card, dict) or card.get("memory_type") not in {"strategy", "endpoint"}:
            continue
        try:
            memory_id = int(card.get("memory_id") or 0)
        except Exception:
            memory_id = 0
        if memory_id > 0 and (signals.get(memory_id) or {}).get("status") == "used":
            return memory_id
    return None

def time_value(state: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(state.get(key, default) or default)
    except Exception:
        return default
