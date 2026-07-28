from __future__ import annotations

import unittest

from crawler_agent.auth import (
    AuthState,
    AuthVerificationContract,
    build_verification_contract,
    evaluate_auth_facts,
    validate_redirect_chain,
)


TARGET = "https://tenant.example.com/private/items"
FIELDS = ["title", "author"]


def _evaluate(
    *,
    probe=None,
    auth=None,
    login=None,
    previous=None,
    endpoints=None,
    contract=None,
):
    parser = {
        "target_url": TARGET,
        "fields": FIELDS,
        "auth": {**(auth or {}), **({"probe": probe} if probe is not None else {})},
    }
    if endpoints is not None:
        parser["api_endpoints"] = endpoints
    return evaluate_auth_facts(
        parser,
        {"phases": {"login": dict(login or {})}},
        previous or {},
        contract=contract or build_verification_contract(TARGET, FIELDS, env={}),
    )


class AuthDomainTests(unittest.TestCase):
    def assert_state(self, facts, state, authenticated, satisfied=False):
        self.assertEqual(facts["state"], state)
        self.assertEqual(facts["authentication_state"], state)
        self.assertIs(facts["authenticated"], authenticated)
        self.assertIs(facts["contract_satisfied"], satisfied)
        self.assertEqual(facts["source"], "auth_decision_engine")

    def test_login_gate_wins_over_ai_success_claim(self):
        facts = _evaluate(
            probe={"ok": True, "href": TARGET, "body_text_chars": 900, "password_inputs": 1},
            auth={"authentication_state": "verified", "authenticated": True},
        )
        self.assert_state(facts, "required", False)
        self.assertIn("login_gate_observed", facts["reason_codes"])

    def test_challenge_has_highest_precedence(self):
        facts = _evaluate(
            probe={
                "ok": True, "href": TARGET, "body_text_chars": 900,
                "password_inputs": 1, "challenge_hints": 1,
            },
            auth={"authentication_state": "verified", "authenticated": True},
        )
        self.assert_state(facts, "challenge", False)

    def test_conservative_required_claim_is_preserved_without_probe(self):
        facts = _evaluate(auth={"authentication_state": "required"})
        self.assert_state(facts, "required", False)
        self.assertIn("agent_reported_login_gate_requires_probe", facts["reason_codes"])

    def test_ai_success_claim_alone_is_ignored(self):
        facts = _evaluate(
            auth={
                "authentication_state": "verified",
                "authenticated": True,
                "verification_state": "success",
            },
        )
        self.assert_state(facts, "unknown", None)
        self.assertIn("unverified_agent_success_claim_ignored", facts["reason_codes"])

    def test_manual_confirmation_without_target_evidence_is_provisional(self):
        facts = _evaluate(
            probe={"ok": True, "href": TARGET, "body_text_chars": 900},
            auth={"authentication_state": "verified", "authenticated": True},
            login={"attempted": True},
        )
        self.assert_state(facts, "provisional", None)
        self.assertIn("unverified_agent_success_claim_ignored", facts["reason_codes"])

    def test_manual_login_with_target_field_evidence_is_verified(self):
        facts = _evaluate(
            probe={
                "ok": True,
                "href": TARGET,
                "body_text_chars": 900,
                "target_field_matches": ["title"],
            },
            login={"attempted": True},
        )
        self.assert_state(facts, "verified", True, True)

    def test_saved_state_with_observed_api_is_verified(self):
        facts = _evaluate(
            probe={"ok": True, "href": TARGET, "body_text_chars": 900},
            login={"state_loaded": True, "auth_epoch": 1},
            endpoints=[{
                "url": "https://tenant.example.com/api/items",
                "source": "observed",
                "verified": True,
                "auth_epoch": 1,
            }],
        )
        self.assert_state(facts, "verified", True, True)

    def test_saved_state_without_probe_is_provisional_not_exception(self):
        facts = _evaluate(login={"state_loaded": True, "authentication_state": "provisional"})
        self.assert_state(facts, "provisional", None)
        self.assertIn("post_auth_probe_missing", facts["reason_codes"])

    def test_previous_verified_session_hitting_login_wall_becomes_stale(self):
        facts = _evaluate(
            probe={"ok": True, "href": TARGET, "body_text_chars": 500, "hard_gate": True},
            login={"state_loaded": True},
            previous={"state": "verified"},
        )
        self.assert_state(facts, "stale", False)
        self.assertIn("previously_verified_session_invalidated", facts["reason_codes"])

    def test_allowed_sso_intermediate_is_provisional(self):
        contract = build_verification_contract(
            TARGET, FIELDS, env={"BROWSER_AUTH_ALLOWED_DOMAINS": "login.identity.test"}
        )
        facts = _evaluate(
            probe={"ok": True, "href": "https://login.identity.test/authorize", "body_text_chars": 700},
            login={"attempted": True},
            contract=contract,
        )
        self.assert_state(facts, "provisional", None)

    def test_untrusted_auth_location_is_rejected(self):
        facts = _evaluate(
            probe={
                "ok": True,
                "href": "https://attacker.example.net/continue",
                "body_text_chars": 700,
                "target_url_match": True,
                "target_field_matches": ["title"],
            },
            login={"attempted": True},
        )
        self.assert_state(facts, "rejected", False)
        self.assertIn("untrusted_auth_location", facts["reason_codes"])

    def test_redirect_chain_enforces_exact_host_allowlist(self):
        contract = build_verification_contract(
            TARGET, FIELDS, env={"BROWSER_AUTH_ALLOWED_DOMAINS": "login.identity.test"}
        )
        trusted, rejected = validate_redirect_chain(
            [TARGET, "https://login.identity.test/oauth", TARGET], contract
        )
        self.assertTrue(trusted)
        self.assertEqual(rejected, ())
        trusted, rejected = validate_redirect_chain(
            [TARGET, "https://attacker.github.io/oauth", TARGET], contract
        )
        self.assertFalse(trusted)
        self.assertEqual(rejected, ("attacker.github.io",))

    def test_contract_round_trip_preserves_policy(self):
        contract = build_verification_contract(
            TARGET, FIELDS, env={
                "BROWSER_AUTH_ALLOWED_DOMAINS": "login.identity.test",
                "BROWSER_AUTH_MIN_BODY_CHARS": "777",
            },
        )
        restored = AuthVerificationContract.from_dict(contract.to_dict())
        self.assertEqual(restored, contract)

    def test_sparse_parser_is_safe(self):
        facts = evaluate_auth_facts({}, {}, {})
        self.assert_state(facts, "unknown", None)
        self.assertEqual(AuthState.from_value(facts["state"]), AuthState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
