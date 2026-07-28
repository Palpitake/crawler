"""LLM API usage tracking for pi-agent-core and pi-coding-agent.

Records duration, provider usage, cache traffic, cost, tool counts, and failures
through the unified structured logging schema.
"""

from __future__ import annotations

import time
import os
import json
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from crawler_agent.core.logger import get_logger, log_event

logger = get_logger("runtime.model")

DEEPSEEK_PRICING = {
    "input_per_1m": 0.14,   # $0.14 / 1M tokens (deepseek-v3)
    "output_per_1m": 0.28,  # $0.28 / 1M tokens
}

MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-v4-flash")

CHARS_PER_TOKEN = 3.2


@dataclass
class ApiCallRecord:
    phase: str
    round_num: int
    agent_name: str
    model: str
    duration_seconds: float
    input_chars: int
    output_chars: int
    input_tokens_est: int
    output_tokens_est: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_est: float
    timestamp: str
    success: bool
    error: Optional[str] = None
    tool_calls: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "round": self.round_num,
            "agent": self.agent_name,
            "model": self.model,
            "duration_s": round(self.duration_seconds, 2),
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "input_tokens_est": self.input_tokens_est,
            "output_tokens_est": self.output_tokens_est,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens_est": self.total_tokens,
            "cost_est_usd": round(self.cost_est, 6),
            "success": self.success,
            "error": self.error,
            "tool_calls": self.tool_calls,
            "timestamp": self.timestamp,
        }


def _estimate_tokens(char_count: int) -> int:
    return max(1, int(char_count / CHARS_PER_TOKEN))


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * DEEPSEEK_PRICING["input_per_1m"]
        + output_tokens / 1_000_000 * DEEPSEEK_PRICING["output_per_1m"]
    )


class ApiCallTracker:
    def __init__(self, agent_name: str = "agent"):
        self.agent_name = agent_name
        self.records: List[ApiCallRecord] = []
        self._lock = threading.Lock()

    def clear(self) -> None:
        """清空当前任务之前的累计记录。"""
        with self._lock:
            self.records.clear()

    def record(
        self,
        phase: str,
        round_num: int,
        input_text: str,
        output_text: str,
        duration: float,
        success: bool = True,
        error: Optional[str] = None,
        tool_calls: Optional[List[str]] = None,
        usage: Optional[Dict[str, Any]] = None,
        runtime_name: str = "pi-agent-core",
    ) -> ApiCallRecord:
        input_chars = len(input_text or "")
        output_chars = len(output_text or "")
        if isinstance(usage, dict) and usage:
            input_tokens = int(usage.get("input", 0) or 0)
            output_tokens = int(usage.get("output", 0) or 0)
            cache_read_tokens = int(usage.get("cacheRead", 0) or 0)
            cache_write_tokens = int(usage.get("cacheWrite", 0) or 0)
            total_tokens = int(usage.get("totalTokens", 0) or 0) or (
                input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
            )
            usage_cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
            cost = float(usage_cost.get("total", 0) or 0)
        else:
            input_tokens = _estimate_tokens(input_chars)
            output_tokens = _estimate_tokens(output_chars)
            cache_read_tokens = 0
            cache_write_tokens = 0
            total_tokens = input_tokens + output_tokens
            cost = _estimate_cost(input_tokens, output_tokens)

        record = ApiCallRecord(
            phase=phase,
            round_num=round_num,
            agent_name=self.agent_name,
            model=MODEL_NAME,
            duration_seconds=duration,
            input_chars=input_chars,
            output_chars=output_chars,
            input_tokens_est=input_tokens,
            output_tokens_est=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            total_tokens=total_tokens,
            cost_est=cost,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            success=success,
            error=error,
            tool_calls=list(tool_calls or []),
        )

        with self._lock:
            self.records.append(record)

        tool_counts = Counter(str(name) for name in (tool_calls or []))
        tool_summary = ",".join(
            f"{name}:{count}" for name, count in sorted(tool_counts.items())
        ) or "-"
        error_text = str(error or "")
        terminated = bool(not success and (
            "budget_exhausted" in error_text
            or "tool_budget" in error_text
            or "turn_budget" in error_text
            or "aborted" in error_text.lower()
            or "timeout" in error_text.lower()
        ))
        event_status = "success" if success else ("terminated" if terminated else "failed")
        log_event(
            logger,
            "model.session",
            level="INFO" if success else "WARNING",
            status=event_status,
            agent=self.agent_name,
            phase=phase,
            runtime=runtime_name,
            model_attempt=round_num,
            model=MODEL_NAME,
            duration_ms=int(duration * 1000),
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            tokens_total=total_tokens,
            cost_usd=round(cost, 6),
            tools=tool_summary,
            runtime_status=event_status,
            terminal_reason=error_text[:1000] if terminated else None,
            error_type="model_session_failed" if (not success and not terminated) else None,
            reason=error_text[:1000] if (error_text and not terminated) else None,
        )

        return record

    @contextmanager
    def track(self, phase: str = "default", round_num: int = 0):
        t0 = time.time()
        input_container: Dict[str, Any] = {"text": ""}
        output_container: Dict[str, Any] = {"text": ""}
        success = True
        error = None

        def set_input(text: str) -> None:
            input_container["text"] = text

        def set_output(text: str) -> None:
            output_container["text"] = text

        # 保持原有 set_input("...") 用法，同时允许 capture.set_output("...")。
        set_input.set_output = set_output  # type: ignore[attr-defined]

        try:
            yield set_input
        except Exception as e:
            success = False
            error = str(e)[:500]
            raise
        finally:
            duration = time.time() - t0
            self.record(
                phase=phase,
                round_num=round_num,
                input_text=input_container["text"],
                output_text=output_container.get("text", ""),
                duration=duration,
                success=success,
                error=error,
            )

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self.records)

        if not records:
            return {
                "total_calls": 0,
                "total_duration_s": 0,
                "total_tokens_est": 0,
                "total_cost_est_usd": 0,
                "phases": {},
            }

        success_count = sum(1 for r in records if r.success)
        fail_count = sum(1 for r in records if not r.success)
        total_duration = sum(r.duration_seconds for r in records)
        total_tokens = sum(r.total_tokens for r in records)
        total_cost = sum(r.cost_est for r in records)

        phases: Dict[str, Dict[str, Any]] = {}
        for r in records:
            if r.phase not in phases:
                phases[r.phase] = {
                    "calls": 0,
                    "success": 0,
                    "duration_s": 0,
                    "tokens_est": 0,
                    "cost_est_usd": 0,
                }
            p = phases[r.phase]
            p["calls"] += 1
            if r.success:
                p["success"] += 1
            p["duration_s"] += r.duration_seconds
            # Pi reports cumulative usage, including cache traffic.  Aggregate
            # the same total here as at the task level so phase summaries do
            # not silently disagree with the individual call records.
            p["tokens_est"] += r.total_tokens
            p["cost_est_usd"] += r.cost_est

        for p in phases.values():
            p["duration_s"] = round(p["duration_s"], 2)
            p["cost_est_usd"] = round(p["cost_est_usd"], 6)

        return {
            "total_calls": len(records),
            "success_calls": success_count,
            "fail_calls": fail_count,
            "total_duration_s": round(total_duration, 2),
            "total_tokens_est": total_tokens,
            "total_cost_est_usd": round(total_cost, 6),
            "model": MODEL_NAME,
            "phases": phases,
            "calls": [r.to_dict() for r in records],
        }

    def to_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)


GLOBAL_TRACKERS: Dict[str, ApiCallTracker] = {}
_TRACKERS_LOCK = threading.Lock()


def get_tracker(name: str = "supervisor") -> ApiCallTracker:
    with _TRACKERS_LOCK:
        if name not in GLOBAL_TRACKERS:
            GLOBAL_TRACKERS[name] = ApiCallTracker(name)
        return GLOBAL_TRACKERS[name]


def get_all_summaries() -> Dict[str, Any]:
    result = {}
    with _TRACKERS_LOCK:
        for name, tracker in GLOBAL_TRACKERS.items():
            result[name] = tracker.summary()
    return result


def reset_all_trackers() -> None:
    """在新任务开始前重置全局统计，避免不同任务互相污染。"""
    with _TRACKERS_LOCK:
        trackers = list(GLOBAL_TRACKERS.values())
    for tracker in trackers:
        tracker.clear()
