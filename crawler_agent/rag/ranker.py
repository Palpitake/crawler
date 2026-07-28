from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List

from .models import FailureCandidate, MemoryCandidate, MemoryQuery
from .normalizer import canonicalize_fields, site_family


def rank_memories(query: MemoryQuery, candidates: Iterable[MemoryCandidate]) -> List[MemoryCandidate]:
    ranked: List[MemoryCandidate] = []
    for candidate in candidates:
        reasons: List[str] = []
        structural = 0.0
        same_domain = bool(query.domain and query.domain == candidate.domain)
        same_site = bool(
            query.domain and candidate.domain
            and site_family(query.domain) == site_family(candidate.domain)
        )
        same_route = bool(query.route_template and query.route_template == candidate.route_template)
        same_task = bool(query.task_type and query.task_type == candidate.task_type)
        same_collection = bool(query.collection_type and query.collection_type == candidate.collection_type)

        # Site, authentication and concrete endpoint facts are site-bound.
        # Only generic strategy memories may cross a site boundary.
        if candidate.memory_type in {"site", "endpoint", "authentication"} and not same_site:
            continue
        if same_domain:
            structural += 0.25
            reasons.append("same_domain")
        elif same_site:
            structural += 0.18
            reasons.append("same_site_family")
        if same_route:
            structural += 0.25
            reasons.append("same_route_template")
        if same_task:
            structural += 0.25
            reasons.append("same_task_type")
        if same_collection:
            structural += 0.10
            reasons.append("same_collection_type")
        facts_fields = canonicalize_fields((candidate.facts or {}).get("canonical_fields") or [])
        if query.canonical_fields and facts_fields:
            overlap = len(set(query.canonical_fields) & set(facts_fields)) / max(len(set(query.canonical_fields) | set(facts_fields)), 1)
            structural += 0.10 * overlap
            if overlap:
                reasons.append("field_overlap")
        if query.scope_type == str((candidate.facts or {}).get("scope_type") or ""):
            structural += 0.05
            reasons.append("same_scope")

        # Cross-domain strategy matching must be reachable.  Generic pagination
        # and collection strategies are compared by task/data/pagination facts,
        # not by a domain bonus they can never receive.
        if not same_site and candidate.memory_type == "strategy":
            cross = 0.0
            if same_task:
                cross += 0.35
            if same_collection:
                cross += 0.20
            if query.preferred_source not in {"", "unknown"} and query.preferred_source == candidate.data_source:
                cross += 0.20
                reasons.append("same_data_source")
            q_pagination = str((candidate.facts or {}).get("pagination_type") or "")
            if q_pagination not in {"", "unknown", "none"}:
                cross += 0.10
                reasons.append("reusable_pagination_pattern")
            if cross >= structural:
                structural = min(1.0, cross)
                reasons.append("cross_domain_strategy")

        freshness = _freshness(candidate.fresh_until)
        completion = _completion(candidate)
        environment = _environment_match(query, candidate)
        penalties = _penalties(query, candidate)
        lexical = max(0.0, min(float(candidate.lexical_score or 0.0), 1.0))
        final = (
            0.36 * min(structural, 1.0)
            + 0.24 * lexical
            + 0.18 * max(0.0, min(candidate.reliability_score, 1.0))
            + 0.10 * freshness
            + 0.08 * completion
            + 0.04 * environment
            - penalties
        )
        candidate.structural_score = round(min(structural, 1.0), 6)
        candidate.freshness_score = round(freshness, 6)
        candidate.completion_score = round(completion, 6)
        candidate.environment_score = round(environment, 6)
        candidate.penalty_score = round(penalties, 6)
        candidate.final_score = round(max(0.0, min(final, 1.0)), 6)
        candidate.match_reasons = list(dict.fromkeys(reasons))
        candidate.requires_validation = bool(
            candidate.source_kind != "observed"
            or freshness < 0.95
            or candidate.confidence_score < 0.85
            or candidate.validation_failure_count > candidate.validation_success_count
            or not same_domain
            or not same_route
        )
        ranked.append(candidate)
    ranked.sort(key=lambda item: (item.final_score, item.reliability_score, item.last_verified_at or ""), reverse=True)
    return _diversify(ranked)


def rank_failures(query: MemoryQuery, candidates: Iterable[FailureCandidate]) -> List[FailureCandidate]:
    now = datetime.now(timezone.utc)
    values: List[FailureCandidate] = []
    for candidate in candidates:
        same_site = bool(
            query.domain and candidate.domain
            and site_family(query.domain) == site_family(candidate.domain)
        )
        if query.domain and candidate.domain and not same_site:
            continue
        score = 0.0
        if candidate.domain == query.domain:
            score += 0.35
        elif same_site:
            score += 0.25
        if candidate.route_template == query.route_template:
            score += 0.20
        if candidate.task_type == query.task_type:
            score += 0.20
        if candidate.authentication_state == query.authentication_state:
            score += 0.10
        if query.current_root_error and candidate.root_error_type == query.current_root_error:
            score += 0.15
        query_env = query.environment_fingerprint.hex().upper() if query.environment_fingerprint else ""
        candidate_env = str(candidate.environment_fingerprint or "").upper()
        candidate.environment_match = bool(not candidate_env or not query_env or candidate_env == query_env)
        candidate.block_active = bool(_future(candidate.block_until, now) and candidate.environment_match)
        if candidate.block_active:
            score += 0.10
        elif candidate_env and query_env and candidate_env != query_env:
            score -= 0.10
        candidate.match_score = round(min(score, 1.0), 6)
        values.append(candidate)
    values.sort(key=lambda item: (item.block_active, item.match_score, item.last_observed_at), reverse=True)
    return values


def _completion(candidate: MemoryCandidate) -> float:
    total = candidate.complete_runs + candidate.partial_runs
    if total <= 0:
        return 0.5
    return candidate.complete_runs / total


def _freshness(fresh_until: str | None) -> float:
    if not fresh_until:
        return 0.55
    try:
        value = datetime.fromisoformat(str(fresh_until).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return 1.0 if value >= datetime.now(timezone.utc) else 0.25
    except Exception:
        return 0.4


def _environment_match(query: MemoryQuery, candidate: MemoryCandidate) -> float:
    facts = candidate.facts or {}
    required_auth = str(facts.get("authentication_state") or facts.get("auth_state") or "unknown")
    if required_auth in {"", "unknown"}:
        return 0.6
    return 1.0 if required_auth == query.authentication_state else 0.0


def _penalties(query: MemoryQuery, candidate: MemoryCandidate) -> float:
    penalty = 0.0
    if candidate.status == "stale":
        penalty += 0.10
    if candidate.status == "quarantined":
        penalty += 0.40
    if candidate.source_kind == "hypothesized":
        penalty += 0.20
    if candidate.validation_failure_count > candidate.validation_success_count:
        penalty += 0.15
    facts = candidate.facts or {}
    required_auth = str(facts.get("authentication_state") or "unknown")
    if required_auth not in {"", "unknown", query.authentication_state}:
        penalty += 0.25
    known_failures = set(str(v) for v in (facts.get("known_root_errors") or []))
    if query.current_root_error and query.current_root_error in known_failures:
        penalty += 0.30
    return min(penalty, 0.75)


def _future(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > now
    except Exception:
        return False


def _diversify(candidates: List[MemoryCandidate]) -> List[MemoryCandidate]:
    seen = set()
    output: List[MemoryCandidate] = []
    for candidate in candidates:
        fingerprint = (
            candidate.memory_type,
            candidate.domain,
            candidate.route_template,
            candidate.task_type,
            candidate.data_source,
            str((candidate.facts or {}).get("pagination_type") or ""),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        output.append(candidate)
    return output
