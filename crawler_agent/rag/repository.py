from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .config import RagConfig
from .models import FailureCandidate, MemoryCandidate, MemoryQuery
from .pool import MySQLPool


class RagRepository(Protocol):
    backend_name: str

    def health(self) -> Dict[str, Any]: ...
    def load_field_aliases(self, domain: str) -> Dict[str, str]: ...
    def search_memories(self, query: MemoryQuery, limit: int) -> List[MemoryCandidate]: ...
    def search_failures(self, query: MemoryQuery, limit: int) -> List[FailureCandidate]: ...
    def record_retrieval(self, task_id: str, memory_ids: List[int], details: Dict[str, Any]) -> None: ...
    def commit_state(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...


class MySQLRagRepository:
    backend_name = "mysql"

    def __init__(self, config: RagConfig):
        self.config = config
        self.pool = MySQLPool(config)

    def health(self) -> Dict[str, Any]:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone() or {}
        return {"ok": bool(row.get("ok")), "backend": self.backend_name}

    def load_field_aliases(self, domain: str) -> Dict[str, str]:
        sql = """SELECT alias, canonical_field FROM rag_field_alias
                 WHERE domain IN ('', %s)
                 ORDER BY (domain = %s) DESC, priority ASC"""
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [domain, domain])
                rows = list(cursor.fetchall() or [])
        aliases: Dict[str, str] = {}
        for row in rows:
            alias = str(row.get("alias") or "").strip().lower()
            canonical = str(row.get("canonical_field") or "").strip()
            if alias and canonical and alias not in aliases:
                aliases[alias] = canonical
        return aliases

    def search_memories(self, query: MemoryQuery, limit: int) -> List[MemoryCandidate]:
        values = self._search_memory_rows(query, limit, fulltext=self.config.enable_fulltext)
        return [self._memory_candidate(row) for row in values]

    def _search_memory_rows(self, query: MemoryQuery, limit: int, *, fulltext: bool) -> List[Dict[str, Any]]:
        score_sql = "MATCH(summary, searchable_text) AGAINST (%s IN NATURAL LANGUAGE MODE)" if fulltext else "0"
        params: List[Any] = []
        if fulltext:
            params.append(query.query_text)
        sql = f"""
            SELECT id, HEX(memory_key) AS memory_key, memory_type, status, source_kind,
                   domain, route_template, task_type, entity_type, collection_type,
                   data_source, summary, facts, metrics, reliability_score,
                   confidence_score, successful_runs, failed_runs, complete_runs,
                   partial_runs, retrieval_count, selected_count,
                   validation_success_count, validation_failure_count,
                   contribution_count, last_verified_at, last_failed_at,
                   fresh_until, {score_sql} AS lexical_score
            FROM rag_memory
            WHERE status IN ('active', 'stale', 'quarantined')
              AND memory_type IN ('site', 'strategy', 'endpoint', 'authentication')
              AND (domain = %s OR task_type = %s OR collection_type = %s)
            ORDER BY (domain = %s) DESC, (route_hash = %s) DESC,
                     lexical_score DESC, reliability_score DESC, updated_at DESC
            LIMIT %s
        """
        params.extend([query.domain, query.task_type, query.collection_type, query.domain, query.route_hash, limit])
        try:
            with self.pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return list(cursor.fetchall() or [])
        except Exception:
            if fulltext:
                return self._search_memory_rows(query, limit, fulltext=False)
            raise

    def search_failures(self, query: MemoryQuery, limit: int) -> List[FailureCandidate]:
        sql = """
            SELECT id, HEX(failure_key) AS failure_key, domain, route_template,
                   task_type, endpoint_family, root_error_type, terminal_error_type,
                   error_category, retry_strategy, authentication_state, http_status,
                   evidence_summary, facts, occurrence_count, last_observed_at,
                   block_until, expires_at, status
            FROM rag_failure_memory
            WHERE status = 'active'
              AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP(6))
              AND (domain = %s OR task_type = %s)
            ORDER BY (domain = %s) DESC, (route_hash = %s) DESC,
                     (block_until IS NOT NULL AND block_until > UTC_TIMESTAMP(6)) DESC,
                     occurrence_count DESC, last_observed_at DESC
            LIMIT %s
        """
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [query.domain, query.task_type, query.domain, query.route_hash, limit])
                rows = list(cursor.fetchall() or [])
        return [self._failure_candidate(row) for row in rows]

    def record_retrieval(self, task_id: str, memory_ids: List[int], details: Dict[str, Any]) -> None:
        if not memory_ids:
            return
        placeholders = ",".join(["%s"] * len(memory_ids))
        with self.pool.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE rag_memory SET retrieval_count = retrieval_count + 1 WHERE id IN ({placeholders})",
                    memory_ids,
                )
                for rank, memory_id in enumerate(memory_ids, start=1):
                    cursor.execute(
                        """INSERT INTO rag_memory_event
                           (task_id, memory_id, event_type, agent_name, stage, details, created_at)
                           VALUES (%s, %s, 'retrieved', 'supervisor', 'search', %s, UTC_TIMESTAMP(6))""",
                        [task_id, memory_id, _json({**details, "rank": rank})],
                    )

    def commit_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        memories = list(payload.get("memories") or [])
        endpoints = list(payload.get("endpoints") or [])
        failures = [
            item for item in (payload.get("failures") or [])
            if isinstance(item, dict)
        ]
        legacy_failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else None
        if not failures and legacy_failure:
            failures = [legacy_failure]
        execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else None
        usage = list(payload.get("usage") or [])
        memory_ids: Dict[str, int] = {}
        with self.pool.transaction() as connection:
            with connection.cursor() as cursor:
                for memory in memories:
                    memory_id = self._upsert_memory(cursor, memory)
                    memory_ids[str(memory.get("memory_key_hex"))] = memory_id
                for endpoint in endpoints:
                    key = str(endpoint.get("parent_memory_key_hex") or "")
                    memory_id = memory_ids.get(key)
                    if memory_id:
                        self._upsert_endpoint(cursor, memory_id, endpoint)
                for failure in failures:
                    self._upsert_failure(cursor, failure)
                execution_id = self._upsert_execution(cursor, execution) if execution else None
                if execution_id:
                    for item in usage:
                        memory_id = item.get("memory_id") or memory_ids.get(str(item.get("memory_key_hex") or ""))
                        if memory_id:
                            self._insert_usage(cursor, execution_id, int(memory_id), item)
        return {
            "ok": True,
            "backend": self.backend_name,
            "memory_ids": memory_ids,
            "failure_count": len(failures),
            "usage_count": len(usage),
        }

    def _upsert_memory(self, cursor: Any, value: Dict[str, Any]) -> int:
        sql = """
            INSERT INTO rag_memory (
                memory_key, memory_type, status, source_kind, domain, route_template,
                route_hash, task_type, entity_type, collection_type, data_source,
                summary, searchable_text, facts, metrics, reliability_score,
                confidence_score, successful_runs, failed_runs, complete_runs,
                partial_runs, last_verified_at, last_failed_at, fresh_until,
                schema_version, agent_build, created_at, updated_at
            ) VALUES (
                UNHEX(%s), %s, %s, %s, %s, %s, UNHEX(%s), %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            )
            ON DUPLICATE KEY UPDATE
                status = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN status ELSE VALUES(status) END,
                source_kind = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN source_kind ELSE VALUES(source_kind) END,
                summary = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN summary ELSE VALUES(summary) END,
                searchable_text = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN searchable_text ELSE VALUES(searchable_text) END,
                facts = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN facts ELSE VALUES(facts) END,
                metrics = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN metrics ELSE VALUES(metrics) END,
                reliability_score = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN reliability_score ELSE VALUES(reliability_score) END,
                confidence_score = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN confidence_score ELSE VALUES(confidence_score) END,
                successful_runs = successful_runs + VALUES(successful_runs),
                failed_runs = failed_runs + VALUES(failed_runs),
                complete_runs = complete_runs + VALUES(complete_runs),
                partial_runs = partial_runs + VALUES(partial_runs),
                last_verified_at = COALESCE(VALUES(last_verified_at), last_verified_at),
                last_failed_at = COALESCE(VALUES(last_failed_at), last_failed_at),
                fresh_until = CASE
                    WHEN last_verified_at IS NOT NULL AND VALUES(status) = 'stale'
                    THEN fresh_until ELSE VALUES(fresh_until) END,
                schema_version = VALUES(schema_version),
                agent_build = VALUES(agent_build), updated_at = UTC_TIMESTAMP(6),
                id = LAST_INSERT_ID(id)
        """
        params = [
            value["memory_key_hex"], value["memory_type"], value.get("status", "active"),
            value.get("source_kind", "observed"), value.get("domain", ""),
            value.get("route_template", ""), value["route_hash_hex"], value.get("task_type", ""),
            value.get("entity_type", ""), value.get("collection_type", ""), value.get("data_source", ""),
            value.get("summary", ""), value.get("searchable_text", ""), _json(value.get("facts") or {}),
            _json(value.get("metrics") or {}), float(value.get("reliability_score", 0.5)),
            float(value.get("confidence_score", 0.5)), int(value.get("successful_runs", 0)),
            int(value.get("failed_runs", 0)), int(value.get("complete_runs", 0)),
            int(value.get("partial_runs", 0)), value.get("last_verified_at"), value.get("last_failed_at"),
            value.get("fresh_until"), int(value.get("schema_version", 1)), value.get("agent_build", ""),
        ]
        cursor.execute(sql, params)
        return int(cursor.lastrowid)

    def _upsert_endpoint(self, cursor: Any, memory_id: int, value: Dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO rag_strategy_endpoint (
                memory_id, endpoint_key, endpoint_template, endpoint_family,
                http_method, source_kind, verified, request_template,
                response_signature, field_mapping, pagination_facts,
                authentication_facts, last_http_status, successful_probes,
                failed_probes, last_verified_at, fresh_until, created_at, updated_at
            ) VALUES (
                %s, UNHEX(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            ) ON DUPLICATE KEY UPDATE
                endpoint_template=VALUES(endpoint_template), endpoint_family=VALUES(endpoint_family),
                source_kind=VALUES(source_kind), verified=VALUES(verified),
                request_template=VALUES(request_template), response_signature=VALUES(response_signature),
                field_mapping=VALUES(field_mapping), pagination_facts=VALUES(pagination_facts),
                authentication_facts=VALUES(authentication_facts), last_http_status=VALUES(last_http_status),
                successful_probes=successful_probes+VALUES(successful_probes),
                failed_probes=failed_probes+VALUES(failed_probes),
                last_verified_at=COALESCE(VALUES(last_verified_at),last_verified_at),
                fresh_until=VALUES(fresh_until), updated_at=UTC_TIMESTAMP(6)
            """,
            [memory_id, value["endpoint_key_hex"], value.get("endpoint_template", ""),
             value.get("endpoint_family", ""), value.get("http_method", "GET"),
             value.get("source_kind", "observed"), int(bool(value.get("verified"))),
             _json(value.get("request_template") or {}), _json(value.get("response_signature") or {}),
             _json(value.get("field_mapping") or {}), _json(value.get("pagination_facts") or {}),
             _json(value.get("authentication_facts") or {}), value.get("last_http_status"),
             int(value.get("successful_probes", 0)), int(value.get("failed_probes", 0)),
             value.get("last_verified_at"), value.get("fresh_until")],
        )

    def _upsert_failure(self, cursor: Any, value: Dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO rag_failure_memory (
                failure_key, domain, route_template, route_hash, task_type,
                endpoint_family, root_error_type, terminal_error_type,
                error_category, retry_strategy, authentication_state, http_status,
                environment_fingerprint, evidence_summary, facts, occurrence_count,
                last_observed_at, block_until, expires_at, status, created_at, updated_at
            ) VALUES (
                UNHEX(%s), %s, %s, UNHEX(%s), %s, %s, %s, %s, %s, %s,
                %s, %s, UNHEX(%s), %s, %s, %s, %s, %s, %s, %s,
                UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
            ) ON DUPLICATE KEY UPDATE
                terminal_error_type=VALUES(terminal_error_type),
                error_category=VALUES(error_category), retry_strategy=VALUES(retry_strategy),
                authentication_state=VALUES(authentication_state), http_status=VALUES(http_status),
                evidence_summary=VALUES(evidence_summary), facts=VALUES(facts),
                occurrence_count=occurrence_count+1, last_observed_at=VALUES(last_observed_at),
                block_until=VALUES(block_until), expires_at=VALUES(expires_at),
                status=VALUES(status), updated_at=UTC_TIMESTAMP(6)
            """,
            [value["failure_key_hex"], value.get("domain", ""), value.get("route_template", ""),
             value["route_hash_hex"], value.get("task_type", ""), value.get("endpoint_family", ""),
             value.get("root_error_type", "unknown"), value.get("terminal_error_type", "unknown"),
             value.get("error_category", "unknown"), value.get("retry_strategy", "inspect_facts"),
             value.get("authentication_state", "unknown"), value.get("http_status"),
             value.get("environment_fingerprint_hex") or ("00" * 32), value.get("evidence_summary", ""),
             _json(value.get("facts") or {}), int(value.get("occurrence_count", 1)),
             value.get("last_observed_at"), value.get("block_until"), value.get("expires_at"),
             value.get("status", "active")],
        )

    def _upsert_execution(self, cursor: Any, value: Dict[str, Any]) -> int:
        cursor.execute(
            """
            INSERT INTO rag_execution (
                task_id, domain, route_template, task_type, requested_fields,
                canonical_fields, authentication_state, final_status,
                root_error_type, terminal_error_type, retry_strategy, items,
                pagination_complete, selected_run, selected_strategy_id,
                runtime_facts, metrics, supervisor_build, browser_build, code_build,
                started_at, finished_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            ) ON DUPLICATE KEY UPDATE
                authentication_state=VALUES(authentication_state), final_status=VALUES(final_status),
                root_error_type=VALUES(root_error_type), terminal_error_type=VALUES(terminal_error_type),
                retry_strategy=VALUES(retry_strategy), items=VALUES(items),
                pagination_complete=VALUES(pagination_complete), selected_run=VALUES(selected_run),
                selected_strategy_id=VALUES(selected_strategy_id), runtime_facts=VALUES(runtime_facts),
                metrics=VALUES(metrics), finished_at=VALUES(finished_at), id=LAST_INSERT_ID(id)
            """,
            [value.get("task_id"), value.get("domain", ""), value.get("route_template", ""),
             value.get("task_type", ""), _json(value.get("requested_fields") or []),
             _json(value.get("canonical_fields") or []), value.get("authentication_state", "unknown"),
             value.get("final_status", "failed"), value.get("root_error_type", ""),
             value.get("terminal_error_type", ""), value.get("retry_strategy", ""),
             int(value.get("items", 0)), int(bool(value.get("pagination_complete"))),
             value.get("selected_run", ""), value.get("selected_strategy_id"),
             _json(value.get("runtime_facts") or {}), _json(value.get("metrics") or {}),
             value.get("supervisor_build", ""), value.get("browser_build", ""), value.get("code_build", ""),
             value.get("started_at"), value.get("finished_at")],
        )
        return int(cursor.lastrowid)

    def _insert_usage(self, cursor: Any, execution_id: int, memory_id: int, value: Dict[str, Any]) -> None:
        cursor.execute(
            """INSERT INTO rag_memory_usage
               (execution_id, memory_id, agent_name, stage, usage_status,
                rank_position, retrieval_score, validation_result,
                contribution_result, reason, details, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))""",
            [execution_id, memory_id, value.get("agent_name", "supervisor"), value.get("stage", "finalize"),
             value.get("usage_status", "not_used"), value.get("rank_position"), value.get("retrieval_score"),
             value.get("validation_result", ""), value.get("contribution_result", ""),
             value.get("reason", ""), _json(value.get("details") or {})],
        )
        usage_status = str(value.get("usage_status") or "")
        validation = str(value.get("validation_result") or "")
        contribution = str(value.get("contribution_result") or "")
        delta = (0.03 if validation == "validated" else -0.05 if validation == "rejected" else 0.0) + (0.04 if contribution == "contributed" else 0.0)
        cursor.execute(
            """UPDATE rag_memory
               SET selected_count = selected_count + %s,
                   validation_success_count = validation_success_count + %s,
                   validation_failure_count = validation_failure_count + %s,
                   contribution_count = contribution_count + %s,
                   reliability_score = LEAST(0.99000, GREATEST(0.05000, reliability_score + %s)),
                   updated_at = UTC_TIMESTAMP(6)
               WHERE id = %s""",
            [1 if usage_status == "selected" else 0,
             1 if validation == "validated" else 0, 1 if validation == "rejected" else 0,
             1 if contribution == "contributed" else 0, delta, memory_id],
        )

    @staticmethod
    def _memory_candidate(row: Dict[str, Any]) -> MemoryCandidate:
        return MemoryCandidate(
            id=int(row.get("id", 0)), memory_key=str(row.get("memory_key") or ""),
            memory_type=str(row.get("memory_type") or "strategy"), status=str(row.get("status") or "active"),
            source_kind=str(row.get("source_kind") or "historical"), domain=str(row.get("domain") or ""),
            route_template=str(row.get("route_template") or ""), task_type=str(row.get("task_type") or ""),
            entity_type=str(row.get("entity_type") or ""), collection_type=str(row.get("collection_type") or ""),
            data_source=str(row.get("data_source") or "unknown"), summary=str(row.get("summary") or ""),
            facts=_json_obj(row.get("facts")), metrics=_json_obj(row.get("metrics")),
            reliability_score=float(row.get("reliability_score") or 0.5),
            confidence_score=float(row.get("confidence_score") or 0.5),
            successful_runs=int(row.get("successful_runs") or 0), failed_runs=int(row.get("failed_runs") or 0),
            complete_runs=int(row.get("complete_runs") or 0), partial_runs=int(row.get("partial_runs") or 0),
            retrieval_count=int(row.get("retrieval_count") or 0), selected_count=int(row.get("selected_count") or 0),
            validation_success_count=int(row.get("validation_success_count") or 0),
            validation_failure_count=int(row.get("validation_failure_count") or 0),
            contribution_count=int(row.get("contribution_count") or 0),
            last_verified_at=_iso(row.get("last_verified_at")), last_failed_at=_iso(row.get("last_failed_at")),
            fresh_until=_iso(row.get("fresh_until")), lexical_score=_normalize_lexical(row.get("lexical_score")),
        )

    @staticmethod
    def _failure_candidate(row: Dict[str, Any]) -> FailureCandidate:
        return FailureCandidate(
            id=int(row.get("id", 0)), failure_key=str(row.get("failure_key") or ""),
            domain=str(row.get("domain") or ""), route_template=str(row.get("route_template") or ""),
            task_type=str(row.get("task_type") or ""), endpoint_family=str(row.get("endpoint_family") or ""),
            root_error_type=str(row.get("root_error_type") or "unknown"),
            terminal_error_type=str(row.get("terminal_error_type") or "unknown"),
            error_category=str(row.get("error_category") or "unknown"),
            retry_strategy=str(row.get("retry_strategy") or "inspect_facts"),
            authentication_state=str(row.get("authentication_state") or "unknown"),
            http_status=int(row["http_status"]) if row.get("http_status") is not None else None,
            evidence_summary=str(row.get("evidence_summary") or ""), facts=_json_obj(row.get("facts")),
            occurrence_count=int(row.get("occurrence_count") or 1),
            last_observed_at=_iso(row.get("last_observed_at")) or "", block_until=_iso(row.get("block_until")),
            expires_at=_iso(row.get("expires_at")), status=str(row.get("status") or "active"),
            environment_fingerprint=str(row.get("environment_fingerprint") or ""),
        )


class JsonlFallbackRepository:
    """Compatibility reader/writer for migration and database outages.

    It intentionally does not pretend to offer the full MySQL feedback model.
    Records returned from JSONL are historical and always require validation.
    """

    backend_name = "jsonl"

    def __init__(self, config: RagConfig):
        self.config = config
        self.path = Path(config.jsonl_path)

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "backend": self.backend_name, "path": str(self.path)}

    def load_field_aliases(self, domain: str) -> Dict[str, str]:
        return {}

    def search_memories(self, query: MemoryQuery, limit: int) -> List[MemoryCandidate]:
        if not self.path.is_file():
            return []
        rows: List[MemoryCandidate] = []
        for index, line in enumerate(self.path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            try:
                value = json.loads(line)
            except Exception:
                continue
            domain = str(value.get("domain") or "")
            task_type = str(value.get("task_type") or ("comments" if any("评论" in str(v) for v in (value.get("target_fields") or [])) else "generic_collection"))
            if domain != query.domain and task_type != query.task_type:
                continue
            facts = {
                "canonical_fields": value.get("target_fields") or value.get("fields") or [],
                "selectors": value.get("selectors") or {},
                "api_endpoints": value.get("api_endpoints") or [],
                "pagination": value.get("pagination") or {},
                "interaction_plan": value.get("interaction_plan") or [],
                "scope_type": "all",
            }
            rows.append(MemoryCandidate(
                id=-index, memory_key=f"legacy-{index}", memory_type="strategy", status="stale",
                source_kind="historical", domain=domain, route_template=str(value.get("route_template") or ""),
                task_type=task_type, entity_type="record", collection_type="records",
                data_source=str(value.get("data_source") or "unknown"),
                summary=f"Legacy strategy for {domain}: {value.get('data_source') or 'unknown'}",
                facts=facts, metrics={}, reliability_score=min(float(value.get("confidence") or 0.5), 0.6),
                confidence_score=min(float(value.get("confidence") or 0.5), 0.6),
                successful_runs=1 if value.get("success") else 0,
                complete_runs=0, partial_runs=1 if value.get("success") else 0,
                last_verified_at=value.get("last_success_at"), fresh_until=None,
                lexical_score=0.2,
            ))
        return rows[-limit:]

    def search_failures(self, query: MemoryQuery, limit: int) -> List[FailureCandidate]:
        return []

    def record_retrieval(self, task_id: str, memory_ids: List[int], details: Dict[str, Any]) -> None:
        return None

    def commit_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload.get("legacy_record") or payload, ensure_ascii=False, default=str) + "\n")
        return {"ok": True, "backend": self.backend_name}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _normalize_lexical(value: Any) -> float:
    try:
        raw = float(value or 0.0)
    except Exception:
        return 0.0
    # MATCH scores are unbounded and query dependent.  This monotonic mapping
    # keeps the application ranker stable without pretending it is a cosine.
    return raw / (1.0 + raw) if raw > 0 else 0.0
