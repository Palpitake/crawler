from __future__ import annotations

from typing import Any, Dict

from logger import get_logger, log_event

from .config import RagConfig
from .pool import MySQLPool

logger = get_logger("rag")


def run_maintenance(config: RagConfig | None = None) -> Dict[str, Any]:
    config = config or RagConfig.from_env()
    if config.backend != "mysql":
        return {"ok": True, "backend": config.backend, "skipped": True}
    pool = MySQLPool(config)
    stats = {"staled": 0, "quarantined": 0, "failures_expired": 0, "reliability_updated": 0}
    with pool.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE rag_memory SET status='stale', updated_at=UTC_TIMESTAMP(6)
                   WHERE status='active' AND fresh_until IS NOT NULL AND fresh_until < UTC_TIMESTAMP(6)"""
            )
            stats["staled"] = int(cursor.rowcount or 0)
            cursor.execute(
                """UPDATE rag_memory SET status='quarantined', updated_at=UTC_TIMESTAMP(6)
                   WHERE status IN ('active','stale')
                     AND validation_failure_count >= 3
                     AND validation_failure_count > validation_success_count * 2"""
            )
            stats["quarantined"] = int(cursor.rowcount or 0)
            cursor.execute(
                """UPDATE rag_failure_memory SET status='expired', updated_at=UTC_TIMESTAMP(6)
                   WHERE status='active' AND expires_at IS NOT NULL AND expires_at < UTC_TIMESTAMP(6)"""
            )
            stats["failures_expired"] = int(cursor.rowcount or 0)
            cursor.execute(
                """UPDATE rag_memory
                   SET reliability_score = LEAST(0.99000, GREATEST(0.05000,
                       0.35 * ((validation_success_count + 2.0) /
                               (validation_success_count + validation_failure_count + 4.0))
                     + 0.25 * ((successful_runs + 2.0) /
                               (successful_runs + failed_runs + 4.0))
                     + 0.20 * ((complete_runs + 1.0) /
                               (complete_runs + partial_runs + 2.0))
                     + 0.10 * ((contribution_count + 1.0) /
                               (selected_count + 2.0))
                     + 0.10 * IF(fresh_until IS NULL OR fresh_until >= UTC_TIMESTAMP(6), 1.0, 0.25)
                   )), updated_at=UTC_TIMESTAMP(6)
                   WHERE status IN ('active','stale','quarantined')"""
            )
            stats["reliability_updated"] = int(cursor.rowcount or 0)
    log_event(logger, "rag.expire", status="success", **stats)
    if stats["quarantined"]:
        log_event(logger, "rag.quarantine", level="WARNING", status="success", memories=stats["quarantined"])
    return {"ok": True, "backend": "mysql", **stats}
