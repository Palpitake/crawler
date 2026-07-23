"""Headless bridge for Pi's native coding-agent SDK.

Unlike the Browser JSONL bridge, Code does not expose Python file/debug/run
tools.  Pi's own read/write/edit/bash harness operates in the crawler workspace
and returns one final transcript for Agent self-review and minimum artifact checks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pi_browser_runtime import PI_RUNTIME_DIR, _runtime_preflight


PI_CODE_ENTRYPOINT = PI_RUNTIME_DIR / "src" / "code-agent.mjs"


def _json_line(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str) + "\n"


def _safe_child_env() -> Dict[str, str]:
    """Keep model credentials out of native Pi bash child processes."""
    env: Dict[str, str] = {}
    denied_fragments = (
        "API_KEY", "APIKEY", "TOKEN", "PASSWORD", "PASSWD", "SECRET",
        "CREDENTIAL", "TRACE",
    )
    for key, value in os.environ.items():
        upper = key.upper()
        if any(fragment in upper for fragment in denied_fragments):
            continue
        env[key] = value
    env.setdefault("NODE_NO_WARNINGS", "1")
    return env


def _model_api_key(provider: str) -> str:
    explicit = os.getenv("PI_MODEL_API_KEY", "")
    if explicit:
        return explicit
    normalized = provider.upper().replace("-", "_")
    return (
        os.getenv(f"{normalized}_API_KEY", "")
        or (os.getenv("DEEPSEEK_API_KEY", "") if provider == "deepseek" else "")
        or os.getenv("OPENAI_API_KEY", "")
    )


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except Exception:
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            process.kill()
    try:
        process.wait(timeout=5)
    except Exception:
        pass


def run_pi_coding_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    workspace: str,
    max_turns: int = 32,
    max_tools: int = 24,
    bash_timeout_seconds: int = 1800,
    timeout_seconds: int = 9000,
    primary_code_file: Optional[str] = None,
    max_writes: int = 2,
    allowed_domains: Optional[List[str]] = None,
    resume_existing_file: bool = False,
    initial_code_hash: Optional[str] = None,
    recovery_checkpoint: Optional[Dict[str, Any]] = None,
    execution_mode: str = "full",
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    test_script: Optional[List[Dict[str, Any]]] = None,
    record_api: bool = True,
) -> Dict[str, Any]:
    """Run one native Pi coding session with no Python tool-call bridge."""
    node = os.getenv("PI_AGENT_NODE", "node")
    if not PI_CODE_ENTRYPOINT.exists():
        raise RuntimeError(f"pi_coding_entrypoint_missing:{PI_CODE_ENTRYPOINT}")
    _runtime_preflight(node, PI_CODE_ENTRYPOINT)

    resolved_provider = provider or os.getenv("PI_MODEL_PROVIDER", "deepseek")
    resolved_model = model_name or os.getenv("PI_MODEL_NAME") or os.getenv(
        "MODEL_NAME", "deepseek-v4-flash"
    )
    resolved_base_url = base_url or os.getenv("PI_MODEL_BASE_URL", "")
    if not resolved_base_url and resolved_provider == "deepseek":
        resolved_base_url = os.getenv("DEEPSEEK_BASE_URL", "")
    timeout_seconds = max(60, min(int(timeout_seconds), 14400))
    payload: Dict[str, Any] = {
        "type": "start",
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "cwd": str(Path(workspace).resolve()),
        "provider": resolved_provider,
        "model": resolved_model,
        "base_url": resolved_base_url,
        "api_key": _model_api_key(resolved_provider),
        "max_turns": max(4, min(int(max_turns), 60)),
        "max_tools": max(4, min(int(max_tools), 60)),
        "bash_timeout_seconds": max(30, min(int(bash_timeout_seconds), 3600)),
        "primary_code_file": str(primary_code_file or ""),
        "max_writes": max(1, min(int(max_writes), 6)),
        "allowed_domains": [str(value).lower() for value in (allowed_domains or []) if str(value).strip()],
        "resume_existing_file": bool(resume_existing_file),
        "initial_code_hash": str(initial_code_hash or ""),
        "recovery_checkpoint": recovery_checkpoint if isinstance(recovery_checkpoint, dict) else {},
        "execution_mode": str(execution_mode or "full"),
        "thinking_level": os.getenv("PI_CODE_THINKING_LEVEL", "medium"),
    }
    if test_script is not None:
        payload["test_script"] = test_script

    popen_kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        [node, str(PI_CODE_ENTRYPOINT)],
        cwd=str(PI_RUNTIME_DIR),
        env=_safe_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    started = time.monotonic()
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input=_json_line(payload),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(process)
        stdout, stderr = process.communicate()

    events: List[Dict[str, Any]] = []
    final: Dict[str, Any] = {}
    protocol_error: Optional[str] = None
    for raw_line in str(stdout or "").splitlines():
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            protocol_error = f"pi_coding_protocol_invalid_json:{raw_line[:300]}"
            continue
        if message.get("type") == "event":
            events.append(message)
        elif message.get("type") == "final":
            final = message
        elif message.get("type") == "fatal":
            protocol_error = str(message.get("error") or "pi_coding_fatal")

    duration = time.monotonic() - started
    error = (
        "pi_coding_timeout" if timed_out else protocol_error
        or final.get("error")
        or ("pi_coding_tool_budget_exhausted" if final.get("budget_exhausted") else None)
        or (f"pi_coding_exited:{process.returncode}" if process.returncode else None)
    )
    result = {
        "ok": bool(final and final.get("ok") and not error),
        "assistant_text": final.get("assistant_text", ""),
        "tool_calls": final.get("tool_calls", []),
        "tool_results": final.get("tool_results", []),
        "turns": int(final.get("turns", 0) or 0),
        "bash_output": final.get("bash_output", ""),
        "active_tools": final.get("active_tools", []),
        "budget_exhausted": bool(final.get("budget_exhausted", False)),
        "usage": final.get("usage", {}),
        "events": events[-100:],
        "error": error,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 3),
        "stderr_tail": str(stderr or "")[-8000:],
        "runtime": "pi-coding-agent",
        "session_id": final.get("session_id"),
        "session_file": final.get("session_file"),
        "recovery": final.get("recovery", {}),
    }

    if record_api:
        try:
            from api_logger import get_tracker

            get_tracker("code").record(
                phase="native",
                round_num=0,
                input_text=user_prompt,
                output_text=str(result.get("assistant_text") or ""),
                duration=duration,
                success=bool(result["ok"]),
                error=str(error) if error else None,
                tool_calls=list(result.get("tool_calls") or []),
                usage=result.get("usage", {}),
                runtime_name="pi-coding-agent",
            )
        except Exception:
            pass
    return result
