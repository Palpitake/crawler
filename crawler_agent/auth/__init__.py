"""Authentication contracts, evidence evaluation, and session lifecycle."""

from .contracts import build_verification_contract, validate_redirect_chain
from .decision import decide_auth_state
from .models import (
    AuthDecision,
    AuthEvidence,
    AuthState,
    AuthVerificationContract,
    AUTH_BLOCKING_STATES,
    AUTH_RESOLUTION_STATES,
)
from .service import evaluate_auth_facts
from .sessions import AuthSessionStore, inspect_storage_state, scoped_session_name

__all__ = [
    "AuthDecision",
    "AuthEvidence",
    "AuthSessionStore",
    "AuthState",
    "AuthVerificationContract",
    "AUTH_BLOCKING_STATES",
    "AUTH_RESOLUTION_STATES",
    "build_verification_contract",
    "decide_auth_state",
    "evaluate_auth_facts",
    "inspect_storage_state",
    "scoped_session_name",
    "validate_redirect_chain",
]
