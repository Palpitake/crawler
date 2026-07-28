from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from crawler_agent.rag.feedback import (
    browser_memory_receipts,
    code_memory_receipts,
    emit_memory_feedback,
    merge_memory_receipts,
)
from crawler_agent.rag.writer import _usage_rows


def _state(*, browser_usage=None, code_review=None):
    card = {
        "memory_id": 11,
        "memory_type": "strategy",
        "domain": "example.com",
        "route_template": "/items",
        "data_source": "api",
        "match_score": 0.9,
    }
    return {
        "thread_id": "task-feedback",
        "user_request": "collect https://example.com/items",
        "target_url": "https://example.com/items",
        "target_fields": ["title"],
        "data_source": "api",
        "parser_result": {
            "data_source": "api",
            "memory_usage": list(browser_usage or []),
        },
        "execution_result": {
            "success": True,
            "items_count": 3,
            "pagination_complete": True,
            "ai_review": dict(code_review or {}),
        },
        "rag_memory_views": {
            "supervisor": [dict(card)],
            "browser": [dict(card)],
            "code": [dict(card)],
            "failures": [],
        },
    }


class FeedbackNormalizationTests(unittest.TestCase):
    def test_rejection_wins_without_losing_agent_attribution(self) -> None:
        browser = browser_memory_receipts({
            "memory_usage": [{"memory_id": 11, "status": "used", "reason": "selected endpoint"}],
        })
        code = code_memory_receipts({
            "memory_ids_rejected": [11],
            "memory_rejection_reason": "runtime response differed",
        })
        signal = merge_memory_receipts([browser, code])[11]
        self.assertEqual(signal["status"], "rejected")
        self.assertEqual(signal["agents"], ["browser", "code"])
        self.assertEqual(
            signal["receipt_sources"],
            ["browser.submit_parser", "code.ai_review"],
        )

    def test_immediate_feedback_event_contains_real_agent_and_decision(self) -> None:
        receipts = browser_memory_receipts({
            "memory_usage": [{"memory_id": 7, "status": "used", "reason": "verified route"}],
        })
        with patch("crawler_agent.rag.feedback.log_event") as mocked:
            emit_memory_feedback(logging.getLogger("test"), receipts, stage="parser_submission")
        mocked.assert_called_once()
        args, kwargs = mocked.call_args
        self.assertEqual(args[1], "rag.feedback")
        self.assertEqual(kwargs["status"], "reported")
        self.assertEqual(kwargs["agent"], "browser")
        self.assertEqual(kwargs["decision"], "used")
        self.assertEqual(kwargs["receipt_source"], "browser.submit_parser")

    def test_code_feedback_event_is_reported_immediately(self) -> None:
        receipts = code_memory_receipts({
            "memory_ids_rejected": [9],
            "memory_rejection_reason": "current response changed",
        })
        with patch("crawler_agent.rag.feedback.log_event") as mocked:
            emit_memory_feedback(logging.getLogger("test"), receipts, stage="ai_review")
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs["status"], "reported")
        self.assertEqual(kwargs["agent"], "code")
        self.assertEqual(kwargs["decision"], "rejected")
        self.assertEqual(kwargs["receipt_source"], "code.ai_review")


class UsageAttributionTests(unittest.TestCase):
    def test_browser_receipt_overrides_supervisor_view_attribution(self) -> None:
        rows = _usage_rows(_state(browser_usage=[{
            "memory_id": 11,
            "status": "used",
            "reason": "browser used endpoint",
        }]), "verified_success")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_name"], "browser")
        self.assertEqual(rows[0]["details"]["explicit_agents"], ["browser"])
        self.assertEqual(rows[0]["details"]["receipt_source"], "browser.submit_parser")

    def test_code_receipt_is_attributed_to_code(self) -> None:
        rows = _usage_rows(_state(code_review={
            "memory_ids_used": [11],
            "memory_usage_reason": "code reused pagination",
        }), "verified_success")
        self.assertEqual(rows[0]["agent_name"], "code")

    def test_browser_and_code_receipts_use_multi_agent(self) -> None:
        rows = _usage_rows(_state(
            browser_usage=[{"memory_id": 11, "status": "used", "reason": "browser route"}],
            code_review={"memory_ids_used": [11], "memory_usage_reason": "code pagination"},
        ), "verified_success")
        self.assertEqual(rows[0]["agent_name"], "multi_agent")
        self.assertEqual(rows[0]["details"]["explicit_agents"], ["browser", "code"])
        self.assertEqual(rows[0]["details"]["receipt_source"], "multiple")

    def test_failed_code_receipt_keeps_browser_and_code_attribution(self) -> None:
        state = _state(browser_usage=[{
            "memory_id": 11,
            "status": "used",
            "reason": "browser route",
        }])
        state["failed_code_attempt"] = {
            "ai_review": {
                "memory_ids_rejected": [11],
                "memory_rejection_reason": "code validation failed",
            }
        }
        rows = _usage_rows(state, "strategy_failure")
        self.assertEqual(rows[0]["agent_name"], "multi_agent")
        self.assertEqual(rows[0]["validation_result"], "rejected")
        self.assertEqual(rows[0]["details"]["explicit_agents"], ["browser", "code"])


if __name__ == "__main__":
    unittest.main()
