"""Dependency-free authentication domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse


class AuthState(str, Enum):
    UNKNOWN = "unknown"
    ANONYMOUS = "anonymous"
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    CHALLENGE = "challenge"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"
    REJECTED = "rejected"
    STALE = "stale"

    @classmethod
    def from_value(cls, value: Any) -> "AuthState":
        normalized = str(value or "").strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.UNKNOWN


AUTH_RESOLUTION_STATES = frozenset({
    AuthState.REQUIRED.value,
    AuthState.CHALLENGE.value,
    AuthState.PROVISIONAL.value,
    AuthState.STALE.value,
})

AUTH_BLOCKING_STATES = frozenset({*AUTH_RESOLUTION_STATES, AuthState.REJECTED.value})


@dataclass(frozen=True)
class AuthVerificationContract:
    target_url: str
    target_host: str
    target_route: str
    requested_fields: Tuple[str, ...] = ()
    allowed_auth_domains: Tuple[str, ...] = ()
    min_body_chars: int = 300
    require_target_evidence: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthVerificationContract":
        """Restore a serialized contract without silently changing its policy."""
        try:
            min_body_chars = max(0, int(value.get("min_body_chars", 300)))
        except (TypeError, ValueError):
            min_body_chars = 300
        target_url = str(value.get("target_url") or "")
        target_host = str(value.get("target_host") or "").lower()
        if not target_host:
            target_host = (urlparse(target_url).hostname or "").lower()
        requested = value.get("requested_fields")
        allowed = value.get("allowed_auth_domains")
        allowed_values = tuple(
            str(item).strip().lower().rstrip(".") for item in allowed
            if str(item).strip()
        ) if isinstance(allowed, (list, tuple)) else ()
        if target_host and target_host not in allowed_values:
            allowed_values = (*allowed_values, target_host)
        return cls(
            target_url=target_url,
            target_host=target_host,
            target_route=str(value.get("target_route") or "/"),
            requested_fields=tuple(
                str(item).strip() for item in requested
                if str(item).strip()
            ) if isinstance(requested, (list, tuple)) else (),
            allowed_auth_domains=allowed_values,
            min_body_chars=min_body_chars,
            require_target_evidence=value.get("require_target_evidence") is not False,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_url": self.target_url,
            "target_host": self.target_host,
            "target_route": self.target_route,
            "requested_fields": list(self.requested_fields),
            "allowed_auth_domains": list(self.allowed_auth_domains),
            "min_body_chars": self.min_body_chars,
            "require_target_evidence": self.require_target_evidence,
        }


@dataclass(frozen=True)
class AuthEvidence:
    probe_ok: bool = False
    current_url: str = ""
    target_site_match: Optional[bool] = None
    target_route_match: Optional[bool] = None
    body_text_chars: int = 0
    password_inputs: int = 0
    hard_gate: bool = False
    auth_overlays: int = 0
    challenge_hints: int = 0
    authenticated_hints: int = 0
    target_field_matches: Tuple[str, ...] = ()
    target_data_observed: bool = False
    target_api_observed: bool = False
    manual_login_attempted: bool = False
    saved_state_loaded: bool = False
    pre_login_blocked: bool = False
    redirect_chain_trusted: Optional[bool] = None
    on_allowed_auth_domain: bool = False
    claimed_state: AuthState = AuthState.UNKNOWN
    claimed_authenticated: Optional[bool] = None
    session_name: Optional[str] = None
    login_url: Optional[str] = None
    raw_probe: Optional[Dict[str, Any]] = None

    @property
    def challenge_detected(self) -> bool:
        return self.challenge_hints > 0

    @property
    def login_gate_detected(self) -> bool:
        return self.password_inputs > 0 or self.hard_gate or self.auth_overlays > 0

    @property
    def target_reached(self) -> bool:
        return self.target_site_match is True and self.target_route_match is not False

    @property
    def target_evidence_observed(self) -> bool:
        return bool(
            self.target_data_observed
            or self.target_api_observed
            or self.target_field_matches
        )


@dataclass(frozen=True)
class AuthDecision:
    state: AuthState
    authenticated: Optional[bool]
    verification_state: str
    contract_satisfied: bool
    confidence: float
    reason_codes: Tuple[str, ...]
    evidence: AuthEvidence

    def to_facts(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "authentication_state": self.state.value,
            "auth_required": self.state in {
                AuthState.REQUIRED, AuthState.CHALLENGE, AuthState.PROVISIONAL,
                AuthState.REJECTED, AuthState.STALE,
            },
            "manual_login_required": self.state in {
                AuthState.REQUIRED, AuthState.CHALLENGE, AuthState.STALE,
            },
            "authenticated": self.authenticated,
            "verification_state": self.verification_state,
            "contract_satisfied": self.contract_satisfied,
            "auth_confidence": round(max(0.0, min(self.confidence, 1.0)), 4),
            "reason_codes": list(self.reason_codes),
            "manual_login_attempted": self.evidence.manual_login_attempted,
            "challenge_detected": self.evidence.challenge_detected,
            "session_name": self.evidence.session_name,
            "login_url": self.evidence.login_url,
            "probe": dict(self.evidence.raw_probe or {}),
            "source": "auth_decision_engine",
        }
