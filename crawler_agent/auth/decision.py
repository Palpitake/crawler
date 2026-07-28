"""Deterministic authentication state reduction."""

from __future__ import annotations

from typing import Iterable, Optional

from .models import (
    AuthDecision,
    AuthEvidence,
    AuthState,
    AuthVerificationContract,
)


def _decision(
    state: AuthState,
    authenticated: Optional[bool],
    verification_state: str,
    contract_satisfied: bool,
    confidence: float,
    reasons: Iterable[str],
    evidence: AuthEvidence,
) -> AuthDecision:
    return AuthDecision(
        state=state,
        authenticated=authenticated,
        verification_state=verification_state,
        contract_satisfied=contract_satisfied,
        confidence=confidence,
        reason_codes=tuple(dict.fromkeys(str(value) for value in reasons if value)),
        evidence=evidence,
    )


def decide_auth_state(
    evidence: AuthEvidence,
    contract: AuthVerificationContract,
    *,
    previous_state: AuthState = AuthState.UNKNOWN,
) -> AuthDecision:
    """Reduce raw evidence to state; AI claims are advisory, never sufficient."""
    authentication_context = bool(
        evidence.manual_login_attempted
        or evidence.saved_state_loaded
        or previous_state in {AuthState.PROVISIONAL, AuthState.VERIFIED, AuthState.STALE}
    )
    if evidence.redirect_chain_trusted is False:
        return _decision(
            AuthState.REJECTED, False, "rejected", False, 0.99,
            ["untrusted_auth_redirect"], evidence,
        )
    if evidence.current_url and not evidence.on_allowed_auth_domain and authentication_context:
        return _decision(
            AuthState.REJECTED, False, "rejected", False, 0.95,
            ["untrusted_auth_location"], evidence,
        )
    if evidence.challenge_detected:
        return _decision(
            AuthState.CHALLENGE, False, "unverified", False, 0.98,
            ["challenge_signal_observed"], evidence,
        )
    if evidence.login_gate_detected:
        if previous_state is AuthState.VERIFIED or evidence.saved_state_loaded:
            return _decision(
                AuthState.STALE, False, "rejected", False, 0.99,
                ["previously_verified_session_invalidated", "login_gate_observed"], evidence,
            )
        return _decision(
            AuthState.REQUIRED, False, "unverified", False, 0.96,
            ["login_gate_observed"], evidence,
        )
    if evidence.claimed_state is AuthState.CHALLENGE:
        return _decision(
            AuthState.CHALLENGE, False, "unverified", False, 0.78,
            ["agent_reported_challenge_requires_probe"], evidence,
        )
    if evidence.claimed_state is AuthState.REQUIRED:
        if previous_state is AuthState.VERIFIED:
            return _decision(
                AuthState.STALE, False, "rejected", False, 0.8,
                ["previously_verified_session_invalidated", "agent_reported_login_gate"], evidence,
            )
        return _decision(
            AuthState.REQUIRED, False, "unverified", False, 0.75,
            ["agent_reported_login_gate_requires_probe"], evidence,
        )

    target_page_clear = bool(
        evidence.probe_ok
        and evidence.target_reached
        and evidence.body_text_chars >= contract.min_body_chars
    )
    if contract.requested_fields:
        strong_target_evidence = evidence.target_evidence_observed
    else:
        strong_target_evidence = bool(
            evidence.target_evidence_observed
            or (evidence.pre_login_blocked and evidence.authenticated_hints > 0)
        )
    contract_satisfied = bool(
        target_page_clear
        and (strong_target_evidence or not contract.require_target_evidence)
        and evidence.redirect_chain_trusted is not False
    )
    if contract_satisfied and authentication_context:
        reasons = ["target_resource_contract_satisfied"]
        if evidence.pre_login_blocked:
            reasons.append("login_gate_cleared_after_authentication")
        if evidence.target_data_observed:
            reasons.append("target_data_observed")
        if evidence.target_api_observed:
            reasons.append("target_api_observed")
        if evidence.authenticated_hints:
            reasons.append("authenticated_ui_hint_observed")
        return _decision(
            AuthState.VERIFIED, True, "verified", True, 0.97, reasons, evidence,
        )

    if evidence.manual_login_attempted or evidence.saved_state_loaded:
        reasons = ["authentication_context_requires_target_verification"]
        if not evidence.probe_ok:
            reasons.append("post_auth_probe_missing")
        elif not evidence.target_reached:
            reasons.append("target_resource_not_reached")
        elif evidence.body_text_chars < contract.min_body_chars:
            reasons.append("target_page_too_sparse")
        else:
            reasons.append("target_resource_evidence_missing")
        if evidence.claimed_state == AuthState.VERIFIED or evidence.claimed_authenticated is True:
            reasons.append("unverified_agent_success_claim_ignored")
        return _decision(
            AuthState.PROVISIONAL, None, "unverified", False, 0.82, reasons, evidence,
        )

    if evidence.claimed_state in {AuthState.REJECTED, AuthState.STALE}:
        return _decision(
            evidence.claimed_state, False, "rejected", False, 0.75,
            ["agent_reported_authentication_failure"], evidence,
        )

    if evidence.claimed_state == AuthState.NOT_REQUIRED and target_page_clear:
        return _decision(
            AuthState.NOT_REQUIRED, False, "not_required", False, 0.9,
            ["target_page_accessible_without_authentication"], evidence,
        )

    if evidence.claimed_authenticated is False or evidence.claimed_state == AuthState.ANONYMOUS:
        return _decision(
            AuthState.ANONYMOUS, False, "unverified", False, 0.7,
            ["anonymous_context_reported"], evidence,
        )

    reasons = ["insufficient_authentication_evidence"]
    if evidence.claimed_state == AuthState.VERIFIED or evidence.claimed_authenticated is True:
        reasons.append("unverified_agent_success_claim_ignored")
    return _decision(
        AuthState.UNKNOWN, None, "unverified", False, 0.45, reasons, evidence,
    )
