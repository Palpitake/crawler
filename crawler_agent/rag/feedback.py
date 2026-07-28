"""Normalize and report explicit Browser/Code memory feedback receipts."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from crawler_agent.core.logger import log_event


_VALID_DECISIONS = {"used", "rejected"}


def browser_memory_receipts(parser: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return validated receipts declared by ``submit_parser``."""
    usage = parser.get("memory_usage")
    return normalize_memory_receipts(
        usage if isinstance(usage, list) else [],
        agent_name="browser",
        receipt_source="browser.submit_parser",
    )


def code_memory_receipts(review: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return validated receipts declared in ``AI_REVIEW_JSON``."""
    values: List[Dict[str, Any]] = []
    used_reason = str(review.get("memory_usage_reason") or "")
    rejected_reason = str(review.get("memory_rejection_reason") or "")
    used_ids = review.get("memory_ids_used")
    rejected_ids = review.get("memory_ids_rejected")
    for memory_id in used_ids if isinstance(used_ids, list) else []:
        values.append({"memory_id": memory_id, "status": "used", "reason": used_reason})
    for memory_id in rejected_ids if isinstance(rejected_ids, list) else []:
        values.append({"memory_id": memory_id, "status": "rejected", "reason": rejected_reason})
    usage = review.get("memory_usage")
    if isinstance(usage, list):
        values.extend(item for item in usage if isinstance(item, dict))
    return normalize_memory_receipts(
        values,
        agent_name="code",
        receipt_source="code.ai_review",
    )


def normalize_memory_receipts(
    values: Iterable[Mapping[str, Any]],
    *,
    agent_name: str,
    receipt_source: str,
) -> List[Dict[str, Any]]:
    """Validate and deduplicate one Agent's receipts; rejection wins."""
    receipts: Dict[int, Dict[str, Any]] = {}
    for value in values:
        try:
            memory_id = int(value.get("memory_id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        decision = str(value.get("status") or "").strip().lower()
        if memory_id <= 0 or decision not in _VALID_DECISIONS:
            continue
        reason = str(value.get("reason") or "").strip()[:500]
        existing = receipts.get(memory_id)
        if existing is None:
            receipts[memory_id] = {
                "memory_id": memory_id,
                "status": decision,
                "reason": reason,
                "agent_name": agent_name,
                "receipt_source": receipt_source,
            }
        elif decision == "rejected":
            existing["status"] = "rejected"
            if reason:
                existing["reason"] = reason
        elif reason and not existing.get("reason"):
            existing["reason"] = reason
    return list(receipts.values())


def merge_memory_receipts(
    groups: Sequence[Iterable[Mapping[str, Any]]],
) -> Dict[int, Dict[str, Any]]:
    """Merge receipts across Agents while retaining complete attribution."""
    signals: Dict[int, Dict[str, Any]] = {}
    for group in groups:
        for receipt in group:
            try:
                memory_id = int(receipt.get("memory_id") or 0)
            except (TypeError, ValueError):
                memory_id = 0
            decision = str(receipt.get("status") or "").strip().lower()
            if memory_id <= 0 or decision not in _VALID_DECISIONS:
                continue
            signal = signals.setdefault(memory_id, {
                "status": decision,
                "agents": [],
                "reasons": [],
                "receipt_sources": [],
            })
            agent = str(receipt.get("agent_name") or "").strip()
            source = str(receipt.get("receipt_source") or "").strip()
            reason = str(receipt.get("reason") or "").strip()[:500]
            if agent and agent not in signal["agents"]:
                signal["agents"].append(agent)
            if source and source not in signal["receipt_sources"]:
                signal["receipt_sources"].append(source)
            if reason and reason not in signal["reasons"]:
                signal["reasons"].append(reason)
            if decision == "rejected":
                signal["status"] = "rejected"
    return signals


def emit_memory_feedback(
    logger: logging.Logger,
    receipts: Iterable[Mapping[str, Any]],
    *,
    stage: str,
) -> None:
    """Emit immediate, best-effort receipt events before final RAG commit."""
    for receipt in receipts:
        try:
            log_event(
                logger,
                "rag.feedback",
                status="reported",
                agent=receipt.get("agent_name"),
                stage=stage,
                memory_id=receipt.get("memory_id"),
                decision=receipt.get("status"),
                receipt_source=receipt.get("receipt_source"),
                reason=receipt.get("reason") or None,
            )
        except Exception:
            # Telemetry must never turn a valid crawl into a failed task.
            continue
