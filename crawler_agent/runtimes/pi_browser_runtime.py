"""JSONL bridge between Python and the pi-agent-core Browser/Supervisor runtimes.

The Node process owns the model/tool loop. Python only executes allow-listed
browser tools locally, so cookies and response bodies never cross a second HTTP
service boundary and no shell/file tools are exposed to the model. In the full
browser profile, Pi also owns evidence interpretation and the final parser
decision; this bridge does not score or verify its candidate.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from crawler_agent.core.common import json_line as _json_line
from crawler_agent.core.logger import get_logger, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PI_RUNTIME_DIR = PROJECT_ROOT / "pi-browser-agent"
PI_BROWSER_ENTRYPOINT = PI_RUNTIME_DIR / "src" / "browser-agent.mjs"
PI_SUPERVISOR_ENTRYPOINT = PI_RUNTIME_DIR / "src" / "supervisor-agent.mjs"
_INSTALL_LOCK = threading.Lock()
logger = get_logger("runtime.pi")


class PiRuntimeUnavailable(RuntimeError):
    """Raised when the selected Pi runtime cannot be started."""


def _reader(stream: Any, output: "queue.Queue[Optional[str]]") -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put(line)
    finally:
        output.put(None)


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _pi_dependency_path() -> Path:
    return PI_RUNTIME_DIR / "node_modules" / "@earendil-works" / "pi-agent-core"


def _pi_coding_dependency_path() -> Path:
    return PI_RUNTIME_DIR / "node_modules" / "@earendil-works" / "pi-coding-agent"


def _bootstrap_dependencies() -> None:
    if _pi_dependency_path().exists() and _pi_coding_dependency_path().exists():
        return
    if not _env_flag("PI_AUTO_INSTALL", True):
        raise PiRuntimeUnavailable(
            "pi_dependencies_missing: run `npm ci --prefix pi-browser-agent` "
            "or enable PI_AUTO_INSTALL=true"
        )
    package_lock = PI_RUNTIME_DIR / "package-lock.json"
    if not package_lock.exists():
        raise PiRuntimeUnavailable(f"pi_package_lock_missing:{package_lock}")
    npm = os.getenv("PI_AGENT_NPM") or shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise PiRuntimeUnavailable(
            "npm_executable_missing: install Node.js/npm or set PI_AGENT_NPM"
        )
    try:
        install_timeout = max(
            30,
            min(int(os.getenv("PI_INSTALL_TIMEOUT_SECONDS", "300")), 900),
        )
    except Exception:
        install_timeout = 300
    npm_cache = os.getenv("PI_NPM_CACHE") or str(
        Path(tempfile.gettempdir()) / "crawler-pi-npm-cache"
    )
    with _INSTALL_LOCK:
        if _pi_dependency_path().exists() and _pi_coding_dependency_path().exists():
            return
        log_event(logger, "runtime.setup", status="started", runtime="pi-agent-suite", action="install_dependencies", path=PI_RUNTIME_DIR)
        try:
            completed = subprocess.run(
                [
                    npm,
                    "ci",
                    "--prefix",
                    str(PI_RUNTIME_DIR),
                    "--no-audit",
                    "--no-fund",
                    "--cache",
                    npm_cache,
                ],
                cwd=str(ROOT),
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=install_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PiRuntimeUnavailable(
                f"pi_dependency_install_timeout:{install_timeout}s"
            ) from exc
        except OSError as exc:
            raise PiRuntimeUnavailable(
                f"pi_dependency_install_start_failed:{exc}"
            ) from exc
        output_tail = str(completed.stdout or "")[-4000:]
        if (
            completed.returncode != 0
            or not _pi_dependency_path().exists()
            or not _pi_coding_dependency_path().exists()
        ):
            raise PiRuntimeUnavailable(
                "pi_dependency_install_failed:"
                f"exit={completed.returncode}; output={output_tail}"
            )
        log_event(logger, "runtime.setup", status="success", runtime="pi-agent-suite", action="install_dependencies")


def _runtime_preflight(node: str, entrypoint: Path = PI_BROWSER_ENTRYPOINT) -> None:
    if not entrypoint.exists():
        raise PiRuntimeUnavailable(f"pi_entrypoint_missing:{entrypoint}")
    if not shutil.which(node) and not Path(node).exists():
        raise PiRuntimeUnavailable(f"node_executable_missing:{node}")
    try:
        version_text = subprocess.check_output(
            [node, "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).strip().lstrip("v")
        parts = tuple(int(part) for part in version_text.split(".")[:3])
    except Exception as exc:
        raise PiRuntimeUnavailable(f"node_version_check_failed:{exc}") from exc
    if parts < (22, 19, 0):
        raise PiRuntimeUnavailable(
            f"node_version_unsupported:{version_text}; required>=22.19.0"
        )
    _bootstrap_dependencies()


def _resolve_max_completion_tokens(value: Optional[int] = None) -> int:
    """Return a gateway-safe output budget for Pi model requests."""
    try:
        raw = value if value is not None else os.getenv(
            "PI_MAX_COMPLETION_TOKENS", "32768"
        )
        return max(1024, min(int(raw), 131072))
    except Exception:
        return 32768


def run_pi_browser_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    tool_handler: Callable[[str, Dict[str, Any]], Any],
    max_turns: int = 12,
    max_tools: int = 32,
    timeout_seconds: int = 360,
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    tool_profile: str = "full_browser",
    max_completion_tokens: Optional[int] = None,
    test_script: Optional[List[Dict[str, Any]]] = None,
    record_api: bool = True,
    agent_name: str = "browser",
    phase_name: str = "explore",
    round_num: int = 0,
    allowed_tools: Optional[List[str]] = None,
    operation_mode: str = "explore",
    required_action: Optional[str] = None,
    require_successful_run: bool = False,
    entrypoint: Optional[Path] = None,
    runtime_name: str = "pi-agent-core",
    initial_messages: Optional[List[Dict[str, Any]]] = None,
    state_summary: Optional[Dict[str, Any]] = None,
    recommended_actions: Optional[List[str]] = None,
    thinking_level: str = "low",
    checkpoint_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run one Pi Agent session and synchronously service its browser tools.

    ``test_script`` is used only by the offline regression test.  It selects
    pi-ai's in-memory faux provider while retaining the real Pi Agent loop and
    the same JSONL tool bridge used in production.
    """

    node = os.getenv("PI_AGENT_NODE", "node")
    selected_entrypoint = Path(entrypoint) if entrypoint is not None else PI_BROWSER_ENTRYPOINT
    _runtime_preflight(node, selected_entrypoint)
    max_turns = max(1, min(int(max_turns), 60))
    max_tools = max(1, min(int(max_tools), 100))
    # A full Code session may contain several bounded crawler executions.  The
    # outer Pi budget must therefore be larger than a single crawler timeout.
    timeout_seconds = max(15, min(int(timeout_seconds), 14400))

    resolved_provider = provider or os.getenv("PI_MODEL_PROVIDER", "deepseek")
    resolved_base_url = base_url or os.getenv("PI_MODEL_BASE_URL", "")
    if not resolved_base_url and resolved_provider == "deepseek":
        resolved_base_url = os.getenv("DEEPSEEK_BASE_URL", "")
    resolved_max_tokens = _resolve_max_completion_tokens(max_completion_tokens)
    payload: Dict[str, Any] = {
        "type": "start",
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "max_turns": max_turns,
        "max_tools": max_tools,
        "provider": resolved_provider,
        "model": model_name or os.getenv("PI_MODEL_NAME") or os.getenv(
            "MODEL_NAME", "deepseek-v4-flash"
        ),
        "base_url": resolved_base_url,
        "tool_profile": tool_profile,
        "max_completion_tokens": resolved_max_tokens,
        "thinking_level": str(thinking_level or "low"),
        "operation_mode": str(operation_mode or "explore"),
        "required_action": str(required_action or "") or None,
    }
    if initial_messages:
        payload["initial_messages"] = initial_messages
    if isinstance(state_summary, dict):
        payload["state_summary"] = state_summary
    if recommended_actions is not None:
        payload["recommended_actions"] = [str(value) for value in recommended_actions if str(value)]
    if require_successful_run:
        payload["require_successful_run"] = True
    if allowed_tools is not None:
        payload["allowed_tools"] = [str(name) for name in allowed_tools if str(name)]
    if test_script is not None:
        payload["test_script"] = test_script

    env = dict(os.environ)
    env.setdefault("NODE_NO_WARNINGS", "1")
    process = subprocess.Popen(
        [node, str(selected_entrypoint)],
        cwd=str(PI_RUNTIME_DIR),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_lines: List[str] = []
    stdout_thread = threading.Thread(
        target=_reader, args=(process.stdout, stdout_queue), daemon=True
    )

    def drain_stderr() -> None:
        for line in iter(process.stderr.readline, ""):
            stderr_lines.append(line.rstrip())
            if len(stderr_lines) > 100:
                del stderr_lines[:-100]

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    started = time.monotonic()
    events: List[Dict[str, Any]] = []
    tool_names: List[str] = []
    final: Dict[str, Any] = {}
    latest_transcript: List[Dict[str, Any]] = []
    latest_state_summary: Dict[str, Any] = {}
    latest_recommended_actions: List[str] = []
    protocol_error: Optional[str] = None
    tool_budget_exhausted = False

    try:
        process.stdin.write(_json_line(payload))
        process.stdin.flush()
        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                protocol_error = "pi_agent_timeout"
                break
            try:
                raw_line = stdout_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    protocol_error = f"pi_agent_exited:{process.returncode}"
                    break
                continue
            if raw_line is None:
                if not final:
                    protocol_error = f"pi_agent_stream_closed:{process.poll()}"
                break
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                protocol_error = f"pi_protocol_invalid_json:{raw_line[:300]}"
                break

            message_type = str(message.get("type") or "")
            if message_type == "tool_call":
                call_id = str(message.get("id") or "")
                name = str(message.get("name") or "")
                arguments = message.get("args")
                arguments = arguments if isinstance(arguments, dict) else {}
                if len(tool_names) >= max_tools:
                    tool_budget_exhausted = True
                    tool_result: Any = {
                        "ok": False,
                        "error": "pi_tool_budget_exhausted",
                    }
                else:
                    tool_names.append(name)
                    try:
                        tool_result = tool_handler(name, arguments)
                    except Exception as exc:  # tool errors are returned to Pi
                        tool_result = {"ok": False, "error": str(exc)}
                is_error = bool(
                    isinstance(tool_result, dict)
                    and tool_result.get("error")
                    and not tool_result.get("ok")
                )
                process.stdin.write(_json_line({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": not is_error,
                    "result": tool_result,
                    "error": (
                        str(tool_result.get("error"))
                        if is_error and isinstance(tool_result, dict)
                        else None
                    ),
                }))
                process.stdin.flush()
            elif message_type == "event":
                if message.get("event") == "transcript_checkpoint":
                    if isinstance(message.get("messages"), list):
                        latest_transcript = message.get("messages")
                    if isinstance(message.get("state_summary"), dict):
                        latest_state_summary = message.get("state_summary")
                    if isinstance(message.get("recommended_actions"), list):
                        latest_recommended_actions = [str(v) for v in message.get("recommended_actions") if str(v)]
                    events.append({
                        "type": "event",
                        "event": "transcript_checkpoint",
                        "turn": message.get("turn"),
                        "message_count": len(latest_transcript),
                    })
                    if checkpoint_handler is not None:
                        try:
                            checkpoint_handler({
                                "turn": message.get("turn"),
                                "messages": latest_transcript,
                                "state_summary": latest_state_summary,
                                "recommended_actions": latest_recommended_actions,
                            })
                        except Exception as exc:
                            log_event(logger, "checkpoint.save", level="WARNING", status="failed", agent=agent_name, phase=phase_name, scope="transcript", error_type="checkpoint_handler_failed", reason=str(exc))
                else:
                    events.append(message)
                if len(events) > 100:
                    del events[:-100]
            elif message_type == "final":
                final = message
                break
            elif message_type == "fatal":
                protocol_error = str(message.get("error") or "pi_agent_fatal")
                break
    except (BrokenPipeError, OSError) as exc:
        protocol_error = f"pi_protocol_io_error:{exc}"
    finally:
        if protocol_error:
            _terminate(process)
        else:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate(process)
        try:
            process.stdin.close()
        except Exception:
            pass

    stderr_tail = "\n".join(stderr_lines[-40:])[-8000:]
    duration = time.monotonic() - started
    error = protocol_error or final.get("error") or None
    resolved_tool_names = list(tool_names)
    result = {
        "ok": bool(final and not error),
        "candidate": final.get("candidate") if isinstance(final, dict) else {},
        "assistant_text": final.get("assistant_text", "") if final else "",
        "turns": int(final.get("turns", 0) or 0) if final else 0,
        "tool_calls": resolved_tool_names,
        "events": events,
        "usage": final.get("usage", {}) if final else {},
        "error": error,
        "stop_reason": final.get("stop_reason") if final else None,
        "abort_source": final.get("abort_source") if final else (
            "protocol_timeout" if protocol_error == "pi_agent_timeout"
            else "protocol_io" if str(protocol_error or "").startswith("pi_protocol_io_error")
            else None
        ),
        "tool_budget_exhausted": bool(
            tool_budget_exhausted or (final.get("tool_budget_exhausted") if final else False)
        ),
        "duration_seconds": round(duration, 3),
        "stderr_tail": stderr_tail,
        "runtime": runtime_name,
        "transcript": (final.get("transcript") if isinstance(final.get("transcript"), list) else latest_transcript),
        "state_summary": (final.get("state_summary") if isinstance(final.get("state_summary"), dict) else latest_state_summary),
        "recommended_actions": (final.get("recommended_actions") if isinstance(final.get("recommended_actions"), list) else latest_recommended_actions),
    }

    if record_api:
        try:
            from crawler_agent.core.api_logger import get_tracker

            get_tracker(agent_name).record(
                phase=phase_name,
                round_num=round_num,
                input_text=user_prompt,
                output_text=str(result.get("assistant_text") or "") + "\n" + json.dumps(
                    result.get("candidate") or {}, ensure_ascii=False, default=str
                ),
                duration=duration,
                success=bool(result["ok"]),
                error=str(error) if error else None,
                tool_calls=resolved_tool_names,
                usage=result.get("usage", {}),
                runtime_name=runtime_name,
            )
        except Exception:
            # Telemetry must never make the browser analysis fail.
            pass
    return result


def run_pi_supervisor_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    tool_handler: Callable[[str, Dict[str, Any]], Any],
    max_turns: int = 28,
    max_tools: int = 24,
    timeout_seconds: int = 14400,
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
    test_script: Optional[List[Dict[str, Any]]] = None,
    record_api: bool = True,
    round_num: int = 0,
    initial_messages: Optional[List[Dict[str, Any]]] = None,
    state_summary: Optional[Dict[str, Any]] = None,
    recommended_actions: Optional[List[str]] = None,
    thinking_level: str = "low",
    checkpoint_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run the native pi-agent-core Supervisor capability loop."""
    return run_pi_browser_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tool_handler=tool_handler,
        max_turns=max_turns,
        max_tools=max_tools,
        timeout_seconds=timeout_seconds,
        model_name=model_name,
        provider=provider,
        base_url=base_url,
        tool_profile="supervisor_native",
        max_completion_tokens=max_completion_tokens,
        test_script=test_script,
        record_api=record_api,
        agent_name="supervisor",
        phase_name="pipeline",
        round_num=round_num,
        entrypoint=PI_SUPERVISOR_ENTRYPOINT,
        runtime_name="pi-agent-core",
        initial_messages=initial_messages,
        state_summary=state_summary,
        recommended_actions=recommended_actions,
        thinking_level=thinking_level,
        checkpoint_handler=checkpoint_handler,
    )
