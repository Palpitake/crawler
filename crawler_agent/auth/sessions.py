"""Scoped authentication-session metadata and quarantine lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

from crawler_agent.core.common import write_json_atomic

from .models import AuthState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or fallback)).strip("._")
    return cleaned or fallback


def browser_environment_fingerprint(env: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if env is None else env
    facts = {
        "engine": source.get("BROWSER_ENGINE", "chromium"),
        "profile": source.get("BROWSER_PROFILE", "default"),
        "locale": source.get("BROWSER_LOCALE", "default"),
        "timezone": source.get("BROWSER_TIMEZONE", "default"),
        "proxy_region": source.get("BROWSER_PROXY_REGION", "direct"),
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def inspect_storage_state(
    payload: Mapping[str, Any],
    *,
    now_unix: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate Playwright storage-state structure without exposing secret values."""
    cookies = payload.get("cookies")
    origins = payload.get("origins")
    if not isinstance(cookies, list) or not isinstance(origins, list):
        return {"ok": False, "error_code": "invalid_storage_state_schema"}
    if not cookies and not origins:
        return {"ok": False, "error_code": "empty_storage_state"}
    now = _now().timestamp() if now_unix is None else float(now_unix)
    persistent_expiries = []
    session_cookie_count = 0
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        try:
            expires = float(cookie.get("expires", -1) or -1)
        except (TypeError, ValueError):
            expires = -1
        if expires <= 0:
            session_cookie_count += 1
        else:
            persistent_expiries.append(expires)
    expired_cookie_count = sum(expiry <= now for expiry in persistent_expiries)
    live_cookie_count = sum(expiry > now for expiry in persistent_expiries)
    stale_hint = bool(
        persistent_expiries
        and expired_cookie_count == len(persistent_expiries)
        and session_cookie_count == 0
    )
    diagnostics = {
        "cookie_count": len(cookies),
        "origin_count": len(origins),
        "session_cookie_count": session_cookie_count,
        "persistent_cookie_count": len(persistent_expiries),
        "expired_cookie_count": expired_cookie_count,
        "live_cookie_count": live_cookie_count,
        "stale_hint": stale_hint,
        "checked_at_unix": int(now),
    }
    if stale_hint and not origins:
        return {"ok": False, "error_code": "storage_state_expired", **diagnostics}
    return {"ok": True, **diagnostics}


def scoped_session_name(
    target_url: str,
    *,
    account_alias: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    source = os.environ if env is None else env
    host = (urlparse(str(target_url or "")).hostname or "site").lower()
    # Keep tenant subdomains isolated even when they share a registrable suffix.
    family = host
    alias = account_alias or source.get("BROWSER_ACCOUNT_ALIAS", "default")
    fingerprint = browser_environment_fingerprint(source)
    return _safe(f"{family}__{_safe(str(alias))}__{fingerprint}")


@dataclass(frozen=True)
class SessionLoadDecision:
    allowed: bool
    reason: str
    metadata: Dict[str, Any]


class AuthSessionStore:
    """Persist non-secret session metadata separately from storage-state JSON."""

    def __init__(
        self,
        auth_directory: Path,
        *,
        quarantine_failures: Optional[int] = None,
        metadata_ttl_days: Optional[int] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.auth_directory = Path(auth_directory)
        self.metadata_directory = self.auth_directory / ".metadata"
        self._clock = clock or _now
        try:
            configured_failures = int(os.getenv("BROWSER_AUTH_QUARANTINE_FAILURES", "2"))
        except ValueError:
            configured_failures = 2
        try:
            configured_ttl = int(os.getenv("BROWSER_AUTH_METADATA_TTL_DAYS", "30"))
        except ValueError:
            configured_ttl = 30
        self.quarantine_failures = max(
            1, configured_failures if quarantine_failures is None else quarantine_failures
        )
        self.metadata_ttl_days = max(
            1, configured_ttl if metadata_ttl_days is None else metadata_ttl_days
        )

    def metadata_path(self, session_name: str) -> Path:
        return self.metadata_directory / f"{_safe(session_name)}.json"

    def load_metadata(self, session_name: str) -> Dict[str, Any]:
        path = self.metadata_path(session_name)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def remove(self, session_name: str) -> bool:
        path = self.metadata_path(session_name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def can_load(
        self,
        session_name: str,
        *,
        environment_fingerprint: Optional[str] = None,
    ) -> SessionLoadDecision:
        metadata = self.load_metadata(session_name)
        if not metadata:
            return SessionLoadDecision(True, "legacy_or_untracked_session", {})
        status = str(metadata.get("status") or "active")
        if status in {"quarantined", "stale", "rejected"}:
            return SessionLoadDecision(False, f"session_{status}", metadata)
        expires_at = _parse_time(metadata.get("expires_at"))
        now = self._clock()
        if expires_at and expires_at <= now:
            metadata = {**metadata, "status": "stale", "updated_at": now.isoformat()}
            self._write(session_name, metadata)
            return SessionLoadDecision(False, "session_metadata_expired", metadata)
        expected = str(environment_fingerprint or "")
        actual = str(metadata.get("environment_fingerprint") or "")
        if expected and actual and expected != actual:
            return SessionLoadDecision(False, "session_environment_changed", metadata)
        return SessionLoadDecision(True, "session_active", metadata)

    def record_loaded(
        self,
        session_name: str,
        *,
        target_url: str,
        environment_fingerprint: str,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = self._clock()
        existing = self.load_metadata(session_name)
        value = {
            **existing,
            "schema_version": 1,
            "session_name": _safe(session_name),
            "target_host": (urlparse(target_url).hostname or "").lower(),
            "environment_fingerprint": environment_fingerprint,
            "status": "provisional" if not existing.get("last_verified_at") else existing.get("status", "active"),
            "last_loaded_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": existing.get("expires_at") or (now + timedelta(days=self.metadata_ttl_days)).isoformat(),
            "diagnostics": _safe_diagnostics(diagnostics or {}),
            "consecutive_failures": int(existing.get("consecutive_failures", 0) or 0),
        }
        self._write(session_name, value)
        return value

    def record_verification(
        self,
        session_name: str,
        *,
        state: AuthState,
        target_url: str,
        environment_fingerprint: str,
        reason_codes: list[str],
    ) -> Dict[str, Any]:
        now = self._clock()
        existing = self.load_metadata(session_name)
        failures = int(existing.get("consecutive_failures", 0) or 0)
        status = str(existing.get("status") or "provisional")
        verified_at = existing.get("last_verified_at")
        if state is AuthState.VERIFIED:
            failures = 0
            status = "active"
            verified_at = now.isoformat()
        elif state is AuthState.REJECTED:
            failures += 1
            status = "rejected"
        elif state is AuthState.STALE:
            failures += 1
            status = "stale"
        elif state in {AuthState.REQUIRED, AuthState.CHALLENGE, AuthState.PROVISIONAL}:
            failures += 1
            status = "quarantined" if failures >= self.quarantine_failures else "provisional"
        value = {
            **existing,
            "schema_version": 1,
            "session_name": _safe(session_name),
            "target_host": (urlparse(target_url).hostname or "").lower(),
            "environment_fingerprint": environment_fingerprint,
            "status": status,
            "consecutive_failures": failures,
            "last_auth_state": state.value,
            "last_verification_target": str(target_url or ""),
            "last_reason_codes": list(dict.fromkeys(str(item) for item in reason_codes if item))[:20],
            "last_verified_at": verified_at,
            "updated_at": now.isoformat(),
            "expires_at": existing.get("expires_at") or (now + timedelta(days=self.metadata_ttl_days)).isoformat(),
        }
        self._write(session_name, value)
        return value

    def _write(self, session_name: str, value: Mapping[str, Any]) -> None:
        write_json_atomic(self.metadata_path(session_name), dict(value))


def _safe_diagnostics(value: Mapping[str, Any]) -> Dict[str, Any]:
    allowed = {
        "cookie_count", "origin_count", "session_cookie_count",
        "persistent_cookie_count", "expired_cookie_count", "live_cookie_count",
        "stale_hint", "checked_at_unix",
    }
    return {key: value.get(key) for key in allowed if key in value}
