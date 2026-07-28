from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Dict, Optional

from crawler_agent.core.logger import get_logger, log_event

from .config import RagConfig
from .memory_cards import build_cards
from .normalizer import build_memory_query
from .ranker import rank_failures, rank_memories
from .repository import JsonlFallbackRepository, MySQLRagRepository, RagRepository
from .writer import build_commit_payload

logger = get_logger("rag")


class RagService:
    def __init__(self, config: RagConfig, repository: Optional[RagRepository] = None):
        self.config = config
        self._repository = repository
        self._fallback = JsonlFallbackRepository(config)

    @property
    def repository(self) -> RagRepository:
        if self._repository is None:
            if self.config.backend == "mysql":
                self._repository = MySQLRagRepository(self.config)
            elif self.config.backend == "jsonl":
                self._repository = self._fallback
            else:
                raise RuntimeError("RAG is disabled")
        return self._repository

    def health(self) -> Dict[str, Any]:
        if self.config.backend == "disabled":
            return {"ok": True, "backend": "disabled"}
        started = time.monotonic()
        try:
            result = self.repository.health()
            log_event(logger, "rag.health", status="success", backend=result.get("backend"), latency_ms=int((time.monotonic()-started)*1000))
            return result
        except Exception as exc:
            log_event(logger, "rag.health", level="WARNING", status="degraded", backend=self.config.backend, error_type="rag_backend_unavailable", reason=str(exc), latency_ms=int((time.monotonic()-started)*1000))
            return {"ok": False, "backend": self.config.backend, "error": str(exc)}

    def search_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        base_query = build_memory_query(state)
        aliases: Dict[str, str] = {}
        repository_error: Exception | None = None
        if self.config.backend != "disabled":
            try:
                aliases = self.repository.load_field_aliases(base_query.domain)
            except Exception as exc:
                repository_error = exc
        query = build_memory_query(state, aliases)
        task_id = str(state.get("thread_id") or state.get("task_id") or "-")
        if self.config.backend == "disabled":
            return {"query": query.to_dict(), "supervisor": [], "browser": [], "code": [], "failures": [], "backend": "disabled", "candidate_count": 0, "latency_ms": 0, "warnings": []}
        started = time.monotonic()
        warnings = []
        backend = self.config.backend
        try:
            if repository_error is not None:
                raise repository_error
            candidates = self.repository.search_memories(query, self.config.candidate_limit)
            failures = self.repository.search_failures(query, self.config.top_k_failure * 4)
        except Exception as exc:
            if not self.config.fail_open:
                raise
            warnings.append("mysql_unavailable_jsonl_fallback")
            backend = "jsonl_fallback"
            log_event(logger, "rag.query", level="WARNING", status="degraded", backend=self.config.backend, error_type="rag_backend_unavailable", reason=str(exc), domain=query.domain, task_type=query.task_type)
            candidates = self._fallback.search_memories(query, self.config.candidate_limit)
            failures = []
        ranked = rank_memories(query, candidates)
        ranked_failures = rank_failures(query, failures)
        cards = build_cards(ranked, ranked_failures, top_k=self.config.top_k_strategy)
        selected_ids = [item.id for item in ranked[: self.config.top_k_strategy] if item.id > 0]
        if selected_ids and backend == "mysql":
            try:
                self.repository.record_retrieval(task_id, selected_ids, {"domain": query.domain, "task_type": query.task_type})
            except Exception as exc:
                warnings.append("retrieval_feedback_write_failed")
                log_event(logger, "rag.feedback", level="WARNING", status="degraded", error_type="rag_feedback_write_failed", reason=str(exc))
        latency = int((time.monotonic() - started) * 1000)
        result = {
            "query": query.to_dict(), **cards, "backend": backend,
            "candidate_count": len(candidates), "latency_ms": latency,
            "warnings": warnings,
        }
        log_event(logger, "rag.query", status="success_with_warnings" if warnings else "success", backend=backend, domain=query.domain, route_template=query.route_template, task_type=query.task_type, candidates=len(candidates), selected=len(cards["supervisor"]), failures=len(cards["failures"]), latency_ms=latency, warning_codes=warnings)
        for rank, card in enumerate(cards["supervisor"], start=1):
            log_event(
                logger, "rag.select", status="selected",
                memory_id=card.get("memory_id"), rank=rank,
                score=card.get("match_score"), memory_type=card.get("memory_type"),
                memory_domain=card.get("domain"), memory_route=card.get("route_template"),
                match_reasons=card.get("match_reason"),
                requires_validation=card.get("requires_validation"),
            )
        return result

    def commit_state(self, state: Dict[str, Any], builds: Dict[str, str]) -> Dict[str, Any]:
        if self.config.backend == "disabled":
            return {"ok": True, "backend": "disabled", "skipped": True}
        payload = build_commit_payload(state, self.config, builds)
        quality = payload.get("quality")
        usage_rows = list(payload.get("usage") or [])
        failure_rows = list(payload.get("failures") or [])
        feedback_summary = {
            "usage_status": dict(Counter(str(item.get("usage_status") or "unknown") for item in usage_rows)),
            "validation_result": dict(Counter(str(item.get("validation_result") or "unknown") for item in usage_rows)),
            "contribution_result": dict(Counter(str(item.get("contribution_result") or "unknown") for item in usage_rows)),
        }
        started = time.monotonic()
        try:
            result = self.repository.commit_state(payload)
            if self.config.dual_write_jsonl and self.repository.backend_name != "jsonl":
                self._fallback.commit_state(payload)
            for item in usage_rows:
                event = "rag.apply" if item.get("usage_status") == "selected" else "rag.feedback"
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                log_event(
                    logger, event, status=str(item.get("usage_status") or "provided"),
                    memory_id=item.get("memory_id"), agent=item.get("agent_name"),
                    agents=list(details.get("explicit_agents") or []),
                    receipt_source=details.get("receipt_source"),
                    rank=item.get("rank_position"), quality=quality,
                    validation_result=item.get("validation_result"),
                    contribution_result=item.get("contribution_result"),
                    reason=item.get("reason"),
                )
            log_event(
                logger, "rag.upsert", status="success", backend=result.get("backend"),
                quality=quality, memories=len(payload.get("memories") or []),
                endpoints=len(payload.get("endpoints") or []),
                failures=len(failure_rows), usage=len(usage_rows),
                latency_ms=int((time.monotonic()-started)*1000),
            )
            return {
                **result,
                "quality": quality,
                "failures_written": len(failure_rows),
                "feedback_summary": feedback_summary,
            }
        except Exception as exc:
            if not self.config.fail_open:
                raise
            fallback_result = self._fallback.commit_state(payload)
            log_event(
                logger, "rag.upsert", level="WARNING", status="degraded",
                backend="jsonl_fallback", quality=quality,
                failures=len(failure_rows), usage=len(usage_rows),
                error_type="rag_backend_unavailable", reason=str(exc),
                latency_ms=int((time.monotonic()-started)*1000),
            )
            return {
                **fallback_result,
                "warning": str(exc),
                "quality": quality,
                "failures_written": len(failure_rows),
                "feedback_summary": feedback_summary,
            }


_service_lock = threading.Lock()
_service: Optional[RagService] = None


def get_rag_service(*, reset: bool = False, repository: Optional[RagRepository] = None) -> RagService:
    global _service
    with _service_lock:
        if reset or _service is None or repository is not None:
            _service = RagService(RagConfig.from_env(), repository=repository)
        return _service
