"""Translate Browser observations into normalized authentication evidence."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from urllib.parse import urlparse

from .contracts import host_allowed, target_location_matches, validate_redirect_chain
from .models import AuthEvidence, AuthState, AuthVerificationContract


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "verified", "success"}:
        return True
    if text in {"0", "false", "no", "rejected", "failed"}:
        return False
    return None


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _probe_blocked(probe: Mapping[str, Any]) -> bool:
    return bool(
        _integer(probe.get("password_inputs")) > 0
        or probe.get("hard_gate")
        or _integer(probe.get("auth_overlays")) > 0
    )


def collect_auth_evidence(
    parser: Mapping[str, Any],
    pipeline_info: Optional[Mapping[str, Any]],
    contract: AuthVerificationContract,
) -> AuthEvidence:
    pipeline = _mapping(pipeline_info)
    auth = _mapping(parser.get("auth"))
    phases = _mapping(pipeline.get("phases"))
    login = _mapping(phases.get("login"))
    probe = _mapping(
        auth.get("probe")
        or login.get("post_login_probe")
        or login.get("resume_probe")
    )
    pre_login_probe = _mapping(login.get("pre_login_probe"))
    current_url = str(probe.get("href") or probe.get("current_url") or "")
    site_match, route_match = target_location_matches(current_url, contract)
    explicit_target_match = _optional_bool(probe.get("target_url_match"))
    if explicit_target_match is True:
        site_match, route_match = True, True
    elif explicit_target_match is False and route_match is None:
        route_match = False

    endpoints = parser.get("api_endpoints")
    current_auth_epoch = _integer(login.get("auth_epoch"))
    target_api_observed = any(
        isinstance(item, Mapping)
        and str(item.get("source") or "").lower() == "observed"
        and item.get("verified") is True
        and (
            item.get("observed_after_auth") is True
            or (
                current_auth_epoch > 0
                and _integer(item.get("auth_epoch")) == current_auth_epoch
            )
        )
        for item in (endpoints if isinstance(endpoints, list) else [])
    )
    target_data_observed = bool(
        login.get("target_data_observed")
        or _integer(probe.get("target_content_chars")) >= contract.min_body_chars
    )
    field_matches = probe.get("target_field_matches")
    normalized_matches = tuple(
        str(value)[:120]
        for value in (field_matches if isinstance(field_matches, list) else [])
        if str(value).strip()
    )
    redirect_values = probe.get("redirect_chain") or login.get("redirect_chain") or []
    redirect_chain = (
        list(redirect_values) if isinstance(redirect_values, (list, tuple)) else []
    )
    redirect_trusted = None
    if redirect_chain:
        redirect_trusted, _ = validate_redirect_chain(redirect_chain, contract)
    try:
        current_host = (urlparse(current_url).hostname or "").lower()
    except Exception:
        current_host = ""

    pipeline_claim = str(login.get("authentication_state") or "").lower()
    claimed_value = (
        pipeline_claim
        if pipeline_claim in {AuthState.STALE.value, AuthState.REJECTED.value}
        else auth.get("authentication_state") or auth.get("state") or pipeline_claim
    )
    if not claimed_value and (
        auth.get("auth_required") or parser.get("page_type") == "auth_required"
    ):
        claimed_value = AuthState.REQUIRED.value
    claimed_state = AuthState.from_value(claimed_value)
    claimed_authenticated = _optional_bool(auth.get("authenticated"))
    challenge_hints = _integer(probe.get("challenge_hints"))
    if any((
        auth.get("challenge_detected"),
        auth.get("captcha_detected"),
        auth.get("mfa_required"),
        login.get("challenge_detected"),
    )):
        challenge_hints = max(1, challenge_hints)

    return AuthEvidence(
        probe_ok=bool(probe.get("ok")),
        current_url=current_url,
        target_site_match=site_match,
        target_route_match=route_match,
        body_text_chars=_integer(probe.get("body_text_chars")),
        password_inputs=_integer(probe.get("password_inputs")),
        hard_gate=bool(probe.get("hard_gate")),
        auth_overlays=_integer(probe.get("auth_overlays")),
        challenge_hints=challenge_hints,
        authenticated_hints=_integer(probe.get("authenticated_hints")),
        target_field_matches=normalized_matches,
        target_data_observed=target_data_observed,
        target_api_observed=target_api_observed,
        manual_login_attempted=bool(login.get("attempted") or auth.get("manual_login_attempted")),
        saved_state_loaded=bool(login.get("state_loaded")),
        pre_login_blocked=_probe_blocked(pre_login_probe),
        redirect_chain_trusted=redirect_trusted,
        on_allowed_auth_domain=host_allowed(current_host, contract),
        claimed_state=claimed_state,
        claimed_authenticated=claimed_authenticated,
        session_name=(
            str(auth.get("session_name") or login.get("session_name") or "").strip()
            or None
        ),
        login_url=(str(auth.get("login_url") or "").strip() or None),
        raw_probe=dict(probe),
    )
