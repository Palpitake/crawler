from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler_agent.auth import AuthSessionStore, AuthState, inspect_storage_state, scoped_session_name


TARGET = "https://tenant.github.io/private/items"


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        self.store = AuthSessionStore(
            self.root,
            quarantine_failures=2,
            metadata_ttl_days=1,
            clock=lambda: self.now,
        )

    def record(self, state):
        return self.store.record_verification(
            "session",
            state=state,
            target_url=TARGET,
            environment_fingerprint="env-a",
            reason_codes=["test_reason"],
        )

    def test_two_inconclusive_verifications_quarantine_session(self):
        first = self.record(AuthState.PROVISIONAL)
        self.assertEqual(first["status"], "provisional")
        self.assertTrue(self.store.can_load("session", environment_fingerprint="env-a").allowed)
        second = self.record(AuthState.PROVISIONAL)
        self.assertEqual(second["status"], "quarantined")
        self.assertFalse(self.store.can_load("session", environment_fingerprint="env-a").allowed)

    def test_success_recovers_before_quarantine(self):
        self.record(AuthState.PROVISIONAL)
        value = self.record(AuthState.VERIFIED)
        self.assertEqual(value["status"], "active")
        self.assertEqual(value["consecutive_failures"], 0)
        self.assertTrue(self.store.can_load("session", environment_fingerprint="env-a").allowed)

    def test_hard_rejection_and_stale_are_immediate(self):
        rejected = self.record(AuthState.REJECTED)
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(self.store.can_load("session", environment_fingerprint="env-a").allowed)
        self.store.remove("session")
        stale = self.record(AuthState.STALE)
        self.assertEqual(stale["status"], "stale")
        self.assertFalse(self.store.can_load("session", environment_fingerprint="env-a").allowed)

    def test_metadata_expiry_marks_session_stale(self):
        self.store.record_loaded(
            "session",
            target_url=TARGET,
            environment_fingerprint="env-a",
            diagnostics={"cookie_count": 2},
        )
        self.now += timedelta(days=2)
        decision = self.store.can_load("session", environment_fingerprint="env-a")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "session_metadata_expired")
        self.assertEqual(self.store.load_metadata("session")["status"], "stale")

    def test_environment_mismatch_blocks_load(self):
        self.store.record_loaded(
            "session", target_url=TARGET, environment_fingerprint="env-a"
        )
        decision = self.store.can_load("session", environment_fingerprint="env-b")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "session_environment_changed")

    def test_session_scope_separates_host_account_and_environment(self):
        base_env = {"BROWSER_PROFILE": "p1", "BROWSER_LOCALE": "zh-CN"}
        first = scoped_session_name(TARGET, account_alias="alice", env=base_env)
        account = scoped_session_name(TARGET, account_alias="bob", env=base_env)
        environment = scoped_session_name(
            TARGET, account_alias="alice", env={**base_env, "BROWSER_LOCALE": "en-US"}
        )
        tenant = scoped_session_name(
            "https://other.github.io/private/items", account_alias="alice", env=base_env
        )
        self.assertEqual(len({first, account, environment, tenant}), 4)

    def test_metadata_diagnostics_do_not_persist_secrets(self):
        self.store.record_loaded(
            "session",
            target_url=TARGET,
            environment_fingerprint="env-a",
            diagnostics={
                "cookie_count": 2,
                "live_cookie_count": 1,
                "token": "secret-token",
                "cookie_value": "secret-cookie",
            },
        )
        serialized = json.dumps(self.store.load_metadata("session"))
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("secret-cookie", serialized)
        self.assertEqual(self.store.load_metadata("session")["diagnostics"]["cookie_count"], 2)

    def test_storage_state_inspection_returns_counts_only(self):
        result = inspect_storage_state(
            {
                "cookies": [
                    {"name": "session", "value": "do-not-return", "expires": -1},
                    {"name": "persist", "value": "do-not-return", "expires": 2000},
                ],
                "origins": [],
            },
            now_unix=1000,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["cookie_count"], 2)
        self.assertNotIn("cookies", result)
        self.assertNotIn("do-not-return", json.dumps(result))

    def test_remove_resets_lifecycle(self):
        self.record(AuthState.REJECTED)
        self.assertTrue(self.store.remove("session"))
        self.assertEqual(self.store.load_metadata("session"), {})
        self.assertTrue(self.store.can_load("session", environment_fingerprint="env-a").allowed)


if __name__ == "__main__":
    unittest.main()
