"""Build task-scoped authentication verification contracts."""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .models import AuthVerificationContract


def normalize_route(value: str) -> str:
    try:
        path = urlparse(str(value or "")).path or "/"
    except Exception:
        path = "/"
    path = re.sub(r"/{2,}", "/", path)
    return path if path == "/" else path.rstrip("/")


def build_verification_contract(
    target_url: str,
    requested_fields: Iterable[object] = (),
    *,
    env: Optional[Mapping[str, str]] = None,
) -> AuthVerificationContract:
    source = os.environ if env is None else env
    parsed = urlparse(str(target_url or ""))
    host = (parsed.hostname or "").lower()
    configured = {
        value.strip().lower().rstrip(".")
        for value in str(source.get("BROWSER_AUTH_ALLOWED_DOMAINS", "") or "").split(",")
        if value.strip()
    }
    # Authentication trust uses exact hosts. Cross-subdomain and IdP access
    # must be declared explicitly (optionally with a leading ``*.`` rule).
    allowed = {host, *configured}
    try:
        min_chars = max(100, min(int(source.get("BROWSER_AUTH_MIN_BODY_CHARS", "300")), 10_000))
    except (TypeError, ValueError):
        min_chars = 300
    return AuthVerificationContract(
        target_url=str(target_url or ""),
        target_host=host,
        target_route=normalize_route(target_url),
        requested_fields=tuple(
            dict.fromkeys(str(value).strip() for value in requested_fields if str(value).strip())
        ),
        allowed_auth_domains=tuple(sorted(value for value in allowed if value)),
        min_body_chars=min_chars,
        require_target_evidence=True,
    )


def host_allowed(host: str, contract: AuthVerificationContract) -> bool:
    value = str(host or "").strip().lower().rstrip(".")
    if not value:
        return False
    for rule in contract.allowed_auth_domains:
        normalized = str(rule or "").strip().lower().rstrip(".")
        if value == normalized:
            return True
        if normalized.startswith("*.") and value.endswith(normalized[1:]):
            return True
    return False


def target_location_matches(
    current_url: str,
    contract: AuthVerificationContract,
) -> tuple[Optional[bool], Optional[bool]]:
    try:
        parsed = urlparse(str(current_url or ""))
    except Exception:
        return None, None
    host = (parsed.hostname or "").lower()
    if not host:
        return None, None
    same_site = host == contract.target_host
    current_route = normalize_route(current_url)
    target_route = contract.target_route
    route_match = bool(
        current_route == target_route
        or target_route == "/"
        or current_route.startswith(target_route + "/")
    )
    return same_site, route_match


def validate_redirect_chain(
    urls: Sequence[object],
    contract: AuthVerificationContract,
) -> tuple[bool, tuple[str, ...]]:
    """Validate every observable redirect against the task's trust boundary."""
    rejected = []
    for value in urls:
        try:
            host = (urlparse(str(value or "")).hostname or "").lower()
        except Exception:
            host = ""
        if host and not host_allowed(host, contract):
            rejected.append(host)
    return not rejected, tuple(dict.fromkeys(rejected))
