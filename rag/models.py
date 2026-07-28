from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryQuery:
    target_url: str
    domain: str
    route_template: str
    route_hash: bytes
    task_type: str
    entity_type: str
    collection_type: str
    canonical_fields: List[str]
    scope_type: str = "all"
    max_items: Optional[int] = None
    preferred_source: str = "unknown"
    authentication_state: str = "unknown"
    verification_state: str = "unverified"
    current_root_error: str = ""
    query_text: str = ""
    environment_fingerprint: Optional[bytes] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["route_hash"] = self.route_hash.hex()
        if self.environment_fingerprint:
            value["environment_fingerprint"] = self.environment_fingerprint.hex()
        return value


@dataclass
class MemoryCandidate:
    id: int
    memory_key: str
    memory_type: str
    status: str
    source_kind: str
    domain: str
    route_template: str
    task_type: str
    entity_type: str
    collection_type: str
    data_source: str
    summary: str
    facts: Dict[str, Any]
    metrics: Dict[str, Any]
    reliability_score: float
    confidence_score: float
    successful_runs: int = 0
    failed_runs: int = 0
    complete_runs: int = 0
    partial_runs: int = 0
    retrieval_count: int = 0
    selected_count: int = 0
    validation_success_count: int = 0
    validation_failure_count: int = 0
    contribution_count: int = 0
    last_verified_at: Optional[str] = None
    last_failed_at: Optional[str] = None
    fresh_until: Optional[str] = None
    lexical_score: float = 0.0
    structural_score: float = 0.0
    freshness_score: float = 0.0
    completion_score: float = 0.0
    environment_score: float = 0.0
    penalty_score: float = 0.0
    final_score: float = 0.0
    match_reasons: List[str] = field(default_factory=list)
    requires_validation: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FailureCandidate:
    id: int
    failure_key: str
    domain: str
    route_template: str
    task_type: str
    endpoint_family: str
    root_error_type: str
    terminal_error_type: str
    error_category: str
    retry_strategy: str
    authentication_state: str
    http_status: Optional[int]
    evidence_summary: str
    facts: Dict[str, Any]
    occurrence_count: int
    last_observed_at: str
    block_until: Optional[str]
    expires_at: Optional[str]
    status: str
    environment_fingerprint: str = ""
    environment_match: bool = False
    match_score: float = 0.0
    block_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    query: MemoryQuery
    supervisor_cards: List[Dict[str, Any]]
    browser_cards: List[Dict[str, Any]]
    code_cards: List[Dict[str, Any]]
    failure_cards: List[Dict[str, Any]]
    backend: str
    candidate_count: int
    latency_ms: int
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "supervisor": self.supervisor_cards,
            "browser": self.browser_cards,
            "code": self.code_cards,
            "failures": self.failure_cards,
            "backend": self.backend,
            "candidate_count": self.candidate_count,
            "latency_ms": self.latency_ms,
            "warnings": self.warnings,
        }
