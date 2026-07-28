"""
mcp_browser_tools.py

Playwright MCP adapter layer -- maps MCP Server tools to the
local .invoke() tool interface used by the Browser pipeline.

Launch: npx -y @playwright/mcp@0.0.78 --headless (stdio transport)
"""

from __future__ import annotations

import json
import os
import re
import time
import subprocess
import threading
import platform
import queue
import sys
import uuid
from typing import Any, Dict, List, Optional
from pathlib import Path
from collections import deque
import atexit
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field
from crawler_agent.core.collection_evidence import (
    evaluate_transaction_evidence,
    freeze_request_evidence,
    matches_request_family as evidence_matches_request_family,
    request_family as evidence_request_family,
    request_query_value as evidence_request_query_value,
    request_state_fingerprint as evidence_request_state_fingerprint,
    request_url as evidence_request_url,
    select_request_window,
)
from crawler_agent.auth.contracts import build_verification_contract, host_allowed
from crawler_agent.auth.sessions import AuthSessionStore, inspect_storage_state
from crawler_agent.core.logger import get_logger, log_event
from crawler_agent.core.runtime_facts import sanitize_url
from crawler_agent.core.tooling import tool
from crawler_agent.version import BUILD_DATE, VERSION


logger = get_logger("tool.browser")
DEFAULT_MCP_PACKAGE = "@playwright/mcp@0.0.78"
MCP_BROWSER_TOOLS_BUILD = f"{BUILD_DATE}-mcp-browser-tools-v{VERSION}"


# ===========================================================================
# MCP Client -- lightweight JSON-RPC over stdio
# ===========================================================================

class MCPClient:
    """Communicate with Playwright MCP Server via initialized stdio JSON-RPC."""

    def __init__(
        self,
        command: str = "npx",
        args: Optional[List[str]] = None,
        startup_timeout: float = 45.0,
        request_timeout: float = 120.0,
    ):
        self.command = command
        self.args = args or ["-y", DEFAULT_MCP_PACKAGE, "--headless"]
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._lock = threading.RLock()
        self._responses: queue.Queue = queue.Queue()
        self._stderr_tail = deque(maxlen=50)
        self._initialized = False
        atexit.register(self.stop)

    def start(self) -> None:
        if self._process and self._process.poll() is None and self._initialized:
            return

        self.stop()
        self._responses = queue.Queue()
        self._stderr_tail.clear()
        self._request_id = 0

        command_line = [self.command] + self.args
        use_shell = platform.system() == "Windows"
        popen_args: Any = subprocess.list2cmdline(command_line) if use_shell else command_line

        try:
            self._process = subprocess.Popen(
                popen_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=use_shell,
            )
        except Exception as exc:
            raise RuntimeError(f"无法启动 Playwright MCP: {exc}") from exc

        threading.Thread(
            target=self._read_stdout,
            args=(self._process,),
            name="playwright-mcp-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(self._process,),
            name="playwright-mcp-stderr",
            daemon=True,
        ).start()

        try:
            self._initialize()
        except Exception as exc:
            detail = self._stderr_summary()
            self.stop()
            if detail:
                raise RuntimeError(f"Playwright MCP 初始化失败: {exc}; stderr={detail}") from exc
            raise RuntimeError(f"Playwright MCP 初始化失败: {exc}") from exc

    def _read_stdout(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw_line in iter(stream.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            try:
                self._responses.put(json.loads(line))
            except json.JSONDecodeError:
                log_event(logger, "runtime.protocol", level="WARNING", status="failed", runtime="playwright-mcp", action="decode_output", error_type="invalid_jsonl", reason=line[:500])

    def _read_stderr(self, process: subprocess.Popen) -> None:
        stream = process.stderr
        if stream is None:
            return
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            log_event(logger, "runtime.stderr", level="DEBUG", status="running", runtime="playwright-mcp", message=line)

    def _stderr_summary(self) -> str:
        return " | ".join(list(self._stderr_tail)[-8:])[:3000]

    def _write_message(self, message: Dict[str, Any]) -> None:
        process = self._process
        if not process or process.poll() is not None or process.stdin is None:
            detail = self._stderr_summary()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Playwright MCP 进程未运行{suffix}")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except Exception as exc:
            detail = self._stderr_summary()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"向 Playwright MCP 写入失败{suffix}") from exc

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._write_message({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            })

            deadline = time.monotonic() + (timeout or self.request_timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = self._stderr_summary()
                    suffix = f"; stderr={detail}" if detail else ""
                    raise TimeoutError(f"MCP 请求超时: method={method}{suffix}")
                try:
                    response = self._responses.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    process = self._process
                    if not process or process.poll() is not None:
                        detail = self._stderr_summary()
                        suffix = f": {detail}" if detail else ""
                        raise RuntimeError(f"Playwright MCP 在响应 {method} 前退出{suffix}")
                    continue

                # Ignore notifications. A single synchronous client has no other
                # outstanding request, so only the matching id is a response.
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response.get("error") or {}
                    message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    raise RuntimeError(f"MCP {method} 失败: {message}")
                result = response.get("result", {})
                return result if isinstance(result, dict) else {"value": result}

    def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params:
                message["params"] = params
            self._write_message(message)

    def _initialize(self) -> None:
        requested_version = os.getenv("MCP_PROTOCOL_VERSION", "2025-11-25")
        result = self._request(
            "initialize",
            {
                "protocolVersion": requested_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "crawler-browser-agent",
                    "version": "1.0.0",
                },
            },
            timeout=self.startup_timeout,
        )
        negotiated = str(result.get("protocolVersion") or "")
        if not negotiated:
            raise RuntimeError("MCP initialize 响应缺少 protocolVersion")
        self._send_notification("notifications/initialized")
        self._initialized = True
        log_event(logger, "runtime.ready", status="ready", runtime="playwright-mcp", transport="mcp", protocol=negotiated, build=MCP_BROWSER_TOOLS_BUILD)

    def stop(self) -> None:
        process = self._process
        self._initialized = False
        self._process = None
        if process and process.poll() is None:
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()

    def call_tool(self, tool_name: str, arguments: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self._process or self._process.poll() is not None or not self._initialized:
            self.start()

        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )
        content = result.get("content", [])
        text_parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ] if isinstance(content, list) else []
        combined = "\n".join(text_parts)

        if result.get("isError"):
            return {"ok": False, "error": combined or "Playwright MCP tool failed"}

        if combined:
            try:
                parsed = json.loads(combined)
                if isinstance(parsed, dict):
                    return parsed
                return {"ok": True, "result": parsed, "text": combined}
            except json.JSONDecodeError:
                return {"ok": True, "text": combined}

        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return {"ok": True, "structured_content": structured}
        return {"ok": True}


_MCP: Optional[MCPClient] = None
_MCP_CONFIG_LOCK = threading.RLock()
_MCP_CONFIG: Dict[str, Any] = {
    "headless": True,
    "storage_state_path": None,
}


def _build_mcp_args() -> List[str]:
    args = [
        "-y",
        os.getenv("PLAYWRIGHT_MCP_PACKAGE", DEFAULT_MCP_PACKAGE),
    ]
    if _MCP_CONFIG.get("headless", True):
        args.append("--headless")
    args.extend([
        "--browser", os.getenv("PLAYWRIGHT_MCP_BROWSER", "chrome"),
        "--no-sandbox",
        "--isolated",
        "--image-responses", "omit",
    ])
    storage_state_path = _MCP_CONFIG.get("storage_state_path")
    if storage_state_path:
        args.extend(["--storage-state", str(storage_state_path)])
    return args


def configure_browser_mcp(
    storage_state_path: Optional[str] = None,
    headless: bool = True,
    restart: bool = False,
) -> Dict[str, Any]:
    """Configure the singleton MCP browser and restart it when context options change."""
    global _MCP

    normalized_state: Optional[str] = None
    if storage_state_path:
        path = Path(storage_state_path).expanduser().resolve()
        if not path.is_file():
            return {"ok": False, "error": f"登录态文件不存在: {path}"}
        normalized_state = str(path)

    with _MCP_CONFIG_LOCK:
        changed = (
            bool(_MCP_CONFIG.get("headless", True)) != bool(headless)
            or _MCP_CONFIG.get("storage_state_path") != normalized_state
        )
        if _MCP is not None and (changed or restart):
            _MCP.stop()
            _MCP = None
        _MCP_CONFIG["headless"] = bool(headless)
        _MCP_CONFIG["storage_state_path"] = normalized_state

    return {
        "ok": True,
        "headless": bool(headless),
        "storage_state_loaded": bool(normalized_state),
    }


def _mcp() -> MCPClient:
    global _MCP
    with _MCP_CONFIG_LOCK:
        if _MCP is None:
            _MCP = MCPClient(command="npx", args=_build_mcp_args())
            _MCP.start()
        return _MCP


# ===========================================================================
# Ref management -- accessibility tree ref <-> CSS selector
# ===========================================================================

class RefMap:
    """Maintain MCP ref (@eN) to CSS selector mapping."""

    def __init__(self):
        self.ref_to_selector: Dict[str, str] = {}
        self.selector_to_ref: Dict[str, str] = {}

    def update_from_snapshot(self, snapshot_text: str) -> None:
        for match in re.finditer(r"\[ref=(e\d+)\]", snapshot_text):
            ref = match.group(1)
            self.ref_to_selector[ref] = f"[data-pw-ref='{ref}']"

    def resolve_ref(self, ref_or_selector: str) -> str:
        if ref_or_selector.startswith("@"):
            return ref_or_selector[1:]
        return ref_or_selector


_REF_MAP = RefMap()


# ===========================================================================
# Tools -- mapped to MCP calls
# ===========================================================================

class NavigateArgs(BaseModel):
    url: str = Field(..., description="Target URL")
    wait_until: str = Field("domcontentloaded", description="Wait condition")


@tool("browser_open", args_schema=NavigateArgs)
def browser_open(url: str, wait_until: str = "domcontentloaded") -> Dict[str, Any]:
    """Navigate to a URL."""
    navigate = _mcp().call_tool("browser_navigate", {"url": url})
    if not navigate.get("ok"):
        return navigate
    snapshot = _mcp().call_tool("browser_snapshot", {})
    if not snapshot.get("ok"):
        return snapshot
    _REF_MAP.update_from_snapshot(snapshot.get("text", ""))
    return {"ok": True, "url": url, "snapshot_preview": snapshot.get("text", "")[:500]}


class SnapshotArgs(BaseModel):
    max_text_chars: int = Field(4000, description="Max text chars")
    max_links: int = Field(40, description="Max links")


@tool("browser_snapshot", args_schema=SnapshotArgs)
def browser_snapshot(max_text_chars: int = 4000, max_links: int = 40) -> Dict[str, Any]:
    """Get the current page accessibility-tree snapshot."""
    result = _mcp().call_tool("browser_snapshot", {})
    if not result.get("ok"):
        return result
    snapshot_text = result.get("text", "")
    _REF_MAP.update_from_snapshot(snapshot_text)
    return {
        "ok": True,
        "snapshot": snapshot_text[:max_text_chars],
        "ref_count": len(_REF_MAP.ref_to_selector),
        "refs": list(_REF_MAP.ref_to_selector.keys())[:20],
    }


class ClickArgs(BaseModel):
    selector: str = Field(..., description="CSS selector or @ref")
    button: str = Field("left", description="Mouse button")
    double_click: bool = Field(False, description="Double click?")


@tool("browser_click", args_schema=ClickArgs)
def browser_click(selector: str, button: str = "left", double_click: bool = False) -> Dict[str, Any]:
    """Click an element. Supports CSS selector or @eN ref."""
    target = _REF_MAP.resolve_ref(selector)
    kwargs: Dict[str, Any] = {"target": target}
    if button != "left":
        kwargs["button"] = button
    if double_click:
        kwargs["doubleClick"] = True
    return _mcp().call_tool("browser_click", kwargs)


class FillArgs(BaseModel):
    selector: str = Field(..., description="Input selector or @ref")
    value: str = Field(..., description="Value to fill")


@tool("browser_fill", args_schema=FillArgs)
def browser_fill(selector: str, value: str) -> Dict[str, Any]:
    """Fill a form field."""
    target = _REF_MAP.resolve_ref(selector)
    return _mcp().call_tool("browser_type", {"target": target, "text": value})


class SelectArgs(BaseModel):
    selector: str = Field(..., description="Dropdown selector or @ref")
    value: str = Field(..., description="Value to select")


@tool("browser_select", args_schema=SelectArgs)
def browser_select(selector: str, value: str) -> Dict[str, Any]:
    """Select a dropdown option."""
    target = _REF_MAP.resolve_ref(selector)
    return _mcp().call_tool("browser_select_option", {"target": target, "values": [value]})


class ScrollArgs(BaseModel):
    direction: str = Field("down", description="Scroll direction: up/down")
    amount: int = Field(3, description="Number of scrolls")


@tool("browser_scroll", args_schema=ScrollArgs)
def browser_scroll(direction: str = "down", amount: int = 3) -> Dict[str, Any]:
    """Scroll the page."""
    key = "PageDown" if direction == "down" else "PageUp"
    for _ in range(amount):
        result = _mcp().call_tool("browser_press_key", {"key": key})
        if not result.get("ok"):
            return result
        time.sleep(0.3)
    return {"ok": True, "direction": direction, "scrolled": amount}


@tool("browser_infinite_scroll")
def browser_infinite_scroll(max_scrolls: int = 10, pause_ms: int = 1500) -> Dict[str, Any]:
    """Infinite scroll -- keep scrolling until no new content."""
    js = (
        "async () => {"
        "let lastHeight=0,noChangeCount=0;"
        f"for(let i=0;i<{max_scrolls};i++){{"
        "window.scrollTo(0,document.body.scrollHeight);"
        f"await new Promise(r=>setTimeout(r,{pause_ms}));"
        "if(document.body.scrollHeight===lastHeight){noChangeCount++;if(noChangeCount>=2)break;}"
        "else{noChangeCount=0;lastHeight=document.body.scrollHeight;}"
        "}"
        "return JSON.stringify({scrolled:true,finalHeight:document.body.scrollHeight});}"
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    return {"ok": True, "detail": result.get("text", "")}


def _decode_nested_json_result(value: Any) -> Any:
    """Decode JSON.stringify() values returned as a JSON string by MCP."""
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            break
        stripped = current.strip()
        if not stripped.startswith(("{", "[")):
            break
        try:
            current = json.loads(stripped)
        except json.JSONDecodeError:
            break
    return current


def _parse_mcp_result(text: str) -> Any:
    """Parse MCP result text, extracting actual value from Markdown format.

    MCP returns results like:
    ### Result
    "some value"
    ### Ran Playwright code
    ```js
    await page.evaluate('...');
    ```
    """
    if not isinstance(text, str):
        return text

    # Try to extract from ### Result section
    result_match = re.search(r'### Result\s*\n(.*?)(?:\n### |\Z)', text, re.DOTALL)
    if result_match:
        value = result_match.group(1).strip()
        # Try to parse as JSON
        try:
            return _decode_nested_json_result(json.loads(value))
        except json.JSONDecodeError:
            # Remove surrounding quotes if present
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
            return value

    # Try to parse entire text as JSON
    try:
        return _decode_nested_json_result(json.loads(text))
    except json.JSONDecodeError:
        pass

    return text


class EvaluateArgs(BaseModel):
    javascript: str = Field(..., description="JavaScript to execute")


@tool("browser_evaluate", args_schema=EvaluateArgs)
def browser_evaluate(javascript: str) -> Dict[str, Any]:
    """Execute JavaScript on the page."""
    js = javascript.strip()
    if not js.startswith(("async", "function", "()")):
        js = f"() => {js}"
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    raw_text = result.get("text", result)
    parsed = _parse_mcp_result(raw_text)
    return {"ok": True, "result": parsed}


class TextArgs(BaseModel):
    max_chars: int = Field(8000, description="Max characters")


@tool("browser_text", args_schema=TextArgs)
def browser_text(max_chars: int = 8000) -> Dict[str, Any]:
    """Extract page text content."""
    js = f"() => (document.body.innerText||'').substring(0,{max_chars})"
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    raw_text = result.get("text", result)
    parsed = _parse_mcp_result(raw_text)
    return {"ok": True, "text": parsed}


@tool("browser_html")
def browser_html(max_chars: int = 8000) -> Dict[str, Any]:
    """Extract page HTML."""
    js = f"() => (document.body.innerHTML||'').substring(0,{max_chars})"
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    raw_text = result.get("text", result)
    parsed = _parse_mcp_result(raw_text)
    return {"ok": True, "html": parsed}


@tool("browser_links")
def browser_links(max_links: int = 40) -> Dict[str, Any]:
    """Extract page links."""
    js = (
        f"() => JSON.stringify("
        f"[...document.querySelectorAll('a[href]')].slice(0,{max_links}).map("
        "a=>({text:(a.innerText||'').trim().substring(0,200),href:a.href})))"
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    raw_text = result.get("text", "[]")
    parsed = _parse_mcp_result(raw_text)
    if isinstance(parsed, str):
        try:
            links = json.loads(parsed)
        except json.JSONDecodeError:
            links = []
    elif isinstance(parsed, list):
        links = parsed
    else:
        links = []
    return {"ok": True, "links": links, "count": len(links)}


@tool("browser_screenshot")
def browser_screenshot() -> Dict[str, Any]:
    """Take a page screenshot."""
    return _mcp().call_tool("browser_take_screenshot", {})


class NetworkLogArgs(BaseModel):
    resource_type: Optional[str] = Field(None, description="Filter: xhr/fetch/document")
    url_pattern: Optional[str] = Field(None, description="URL regex filter")
    include_body: bool = Field(False, description="Include selected response bodies")
    max_items: int = Field(30, description="Max items to return")
    max_body_items: int = Field(5, description="Maximum response bodies to read")
    max_body_chars: int = Field(8000, description="Maximum characters per response body")
    after_index: Optional[int] = Field(
        None,
        description="Return only requests whose MCP index is newer than this value.",
    )


def _is_comment_filter_pattern(pattern: str) -> bool:
    tokens = {
        token.strip().lower()
        for token in str(pattern or "").split("|")
        if token.strip()
    }
    return bool(tokens) and tokens.issubset({
        "comment", "comments", "reply", "replies", "评论", "回复",
    })


def _is_comment_api_request_url(request_url: str) -> bool:
    """Match API identity, never comment-looking analytics query values."""
    try:
        parsed = urlparse(str(request_url or ""))
    except Exception:
        return False
    path = str(parsed.path or "").lower()
    if re.search(
        r"(?:^|[/_.-])(?:comment|comments|reply|replies)(?:$|[/_.-])",
        path,
        re.I,
    ):
        return True
    # Some APIs use a generic path and an explicit resource query key. Only
    # accept exact, short keys; never scan arbitrary query values.
    safe_keys = {
        str(key).lower() for key in parse_qs(parsed.query, keep_blank_values=True)
        if len(str(key)) <= 80
    }
    return bool(safe_keys.intersection({
        "comment", "comments", "comment_id", "root_comment_id",
        "reply", "replies", "reply_id", "root_reply_id",
    }))


def _extract_mcp_section(text: str, heading: str) -> str:
    if not isinstance(text, str):
        return ""
    match = re.search(
        rf"###\s+{re.escape(heading)}\s*\n(.*?)(?:\n###\s+|\Z)",
        text,
        re.I | re.S,
    )
    payload = match.group(1).strip() if match else text.strip()
    fenced = re.fullmatch(r"```(?:json|text)?\s*\n?(.*?)\n?```", payload, re.I | re.S)
    return fenced.group(1).strip() if fenced else payload


def _request_identity_by_index(index: int) -> Dict[str, Any]:
    """Resolve one captured request index to its URL without exposing headers."""
    try:
        result = _mcp().call_tool(
            "browser_network_requests",
            {"filter": "", "static": False},
        )
        if not result.get("ok"):
            return {}
        for line in str(result.get("text", "")).splitlines():
            match = re.match(r"^\s*(\d+)\.\s+(.+)", line)
            if not match or int(match.group(1)) != int(index):
                continue
            description = match.group(2).strip()
            url_match = re.search(r"https?://[^\s\]\)]+", description)
            request_url = url_match.group(0).rstrip(".,;") if url_match else ""
            return {
                "url": request_url,
                "description": description,
            }
    except Exception:
        return {}
    return {}


def _network_response_body(index: int, max_chars: int = 12000) -> Dict[str, Any]:
    # Comment-list payloads can easily exceed 200 KiB because every row carries
    # author metadata and nested replies.  The Browser Pipeline keeps these
    # bytes outside the LLM prompt, so allowing a larger deterministic read does
    # not increase model context size.
    max_chars = max(500, min(int(max_chars), 1_000_000))
    result = _mcp().call_tool(
        "browser_network_request",
        {"index": int(index), "part": "response-body"},
    )
    if not result.get("ok"):
        return result
    raw_text = str(result.get("text", ""))
    body = _extract_mcp_section(raw_text, "Response body")
    # Older Playwright MCP builds wrap every tool result in ``### Result``.
    # Avoid handing that Markdown heading to the parser as if it were JSON.
    if body == raw_text.strip() and re.match(r"^###\s+Result\b", raw_text, re.I):
        body = _extract_mcp_section(raw_text, "Result")
    if not body:
        return {"ok": False, "error": f"request #{index} 没有可读取的响应正文"}
    truncated = len(body) > max_chars
    identity = _request_identity_by_index(index)
    clipped_body = body[:max_chars]
    return {
        "ok": True,
        "index": int(index),
        "url": identity.get("url", ""),
        "description": identity.get("description", ""),
        "body": clipped_body,
        "body_chars": len(body),
        "truncated": truncated,
        "response_summary": _response_body_summary(body),
    }


def _network_body_priority(request: Dict[str, Any]) -> tuple:
    """Rank likely collection/list APIs before auxiliary comment endpoints."""
    text = " ".join(
        str(request.get(key) or "")
        for key in ("url", "description")
    ).lower()
    score = 0
    if re.search(r"/(?:comment|comments|reply|replies)(?:/|\?|$)", text):
        score += 20
    if re.search(r"(?:/|\b)(?:main|list|root|feed)(?:/|\?|\b)", text):
        score += 35
    if re.search(r"reply/(?:wbi/)?main|comment/(?:main|list)", text):
        score += 80
    if re.search(r"status|config|setting|permission|interaction|prohibition", text):
        score -= 30
    # Auxiliary metadata/detail endpoints often contain the same resource word
    # as a collection endpoint. Keep them visible, but inspect real list/feed
    # responses first so the agent does not mistake a description response for
    # the target records.
    if re.search(r"(?:/|\b)(?:description|metadata|detail|info|stat)(?:/|\?|\b)", text):
        score -= 55
    return (-score, int(request.get("index", 0) or 0))


def _response_body_summary(body: str) -> Dict[str, Any]:
    """Return compact, site-agnostic JSON structure evidence for the agent."""
    text = str(body or "").strip()
    summary: Dict[str, Any] = {
        "chars": len(text),
        "json": False,
        "top_keys": [],
        "data_keys": [],
        "list_candidates": [],
        "pagination_candidates": [],
        "unique_item_ids": 0,
        "sample_item_ids": [],
    }
    try:
        payload = json.loads(text)
    except Exception:
        payload = None

    item_id_keys = {
        "id", "cid", "rpid", "reply_id", "comment_id", "item_id", "record_id",
    }
    pagination_pattern = re.compile(
        r"cursor|offset|page|next|has_more|is_end|end|continuation|total|count",
        re.I,
    )
    discovered_ids = set()
    list_candidates: List[Dict[str, Any]] = []
    pagination_candidates: List[Dict[str, Any]] = []

    def walk(
        node: Any,
        path: str = "$",
        depth: int = 0,
        record_context: bool = False,
    ) -> None:
        if depth > 10 or len(list_candidates) >= 30:
            return
        if isinstance(node, dict):
            for key, value in list(node.items())[:300]:
                key_text = str(key)
                child_path = f"{path}.{key_text}"
                if (
                    record_context
                    and key_text.lower() in item_id_keys
                    and isinstance(value, (str, int))
                ):
                    discovered_ids.add(str(value))
                if pagination_pattern.search(key_text) and isinstance(
                    value, (str, int, float, bool, type(None))
                ):
                    pagination_candidates.append({"path": child_path, "value": value})
                walk(value, child_path, depth + 1, False)
        elif isinstance(node, list):
            sample = next((item for item in node if isinstance(item, dict)), None)
            if node:
                candidate: Dict[str, Any] = {"path": path, "length": len(node)}
                if isinstance(sample, dict):
                    candidate["sample_keys"] = [str(key) for key in list(sample.keys())[:30]]
                    candidate["sample_id_keys"] = [
                        str(key) for key in sample if str(key).lower() in item_id_keys
                    ][:10]
                list_candidates.append(candidate)
            for index, value in enumerate(node[:30]):
                walk(
                    value,
                    f"{path}[{index}]",
                    depth + 1,
                    isinstance(value, dict),
                )

    if isinstance(payload, (dict, list)):
        summary["json"] = True
        if isinstance(payload, dict):
            summary["top_keys"] = [str(key) for key in list(payload.keys())[:20]]
            data = payload.get("data")
            if isinstance(data, dict):
                summary["data_keys"] = [str(key) for key in list(data.keys())[:30]]
            if "code" in payload:
                summary["code"] = payload.get("code")
            if "message" in payload or "msg" in payload:
                summary["message"] = str(payload.get("message") or payload.get("msg") or "")[:300]
        walk(payload)
        summary["list_candidates"] = list_candidates[:15]
        summary["pagination_candidates"] = pagination_candidates[:30]
        summary["unique_item_ids"] = len(discovered_ids)
        summary["sample_item_ids"] = sorted(discovered_ids)[:50]
    else:
        code_match = re.search(r'"code"\s*:\s*(-?\d+)', text[:8000])
        if code_match:
            summary["code"] = int(code_match.group(1))
        summary["json_prefix"] = text.startswith(("{", "["))
    return summary


class NetworkResponseBodyArgs(BaseModel):
    index: int = Field(..., description="Request index returned by browser_network_log")
    max_chars: int = Field(12000, description="Maximum body characters")


@tool("browser_network_response_body", args_schema=NetworkResponseBodyArgs)
def browser_network_response_body(index: int, max_chars: int = 12000) -> Dict[str, Any]:
    """Read only the response body of one captured request, without exposing auth headers."""
    return _network_response_body(index, max_chars)


@tool("browser_network_log", args_schema=NetworkLogArgs)
def browser_network_log(
    resource_type: Optional[str] = None,
    url_pattern: Optional[str] = None,
    include_body: bool = False,
    max_items: int = 30,
    max_body_items: int = 5,
    max_body_chars: int = 8000,
    after_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Get captured requests and optionally selected response bodies.

    ``browser_network_requests.filter`` has changed semantics between MCP
    releases (some versions treat it as a literal substring, others as a
    pattern).  Always fetch the captured list and apply ``url_pattern``
    locally so an expression such as ``comment|reply`` cannot accidentally
    select unrelated requests.  Response bodies are read only after this
    deterministic filtering step.
    """
    filter_arg = url_pattern or ""
    max_items = max(1, min(int(max_items), 100))
    max_body_items = max(0, min(int(max_body_items), 50))
    max_body_chars = max(500, min(int(max_body_chars), 1_000_000))
    after_index_value = max(0, int(after_index or 0))

    list_result = _mcp().call_tool(
        "browser_network_requests",
        {"filter": "", "static": False},
    )
    if not list_result.get("ok"):
        return list_result

    text = list_result.get("text", "")
    raw_requests: List[Dict[str, Any]] = []
    try:
        url_filter = re.compile(filter_arg, re.I) if filter_arg else None
        filter_error = None
    except re.error as exc:
        # Invalid model-generated regexes remain useful as literal filters.
        url_filter = re.compile(re.escape(filter_arg), re.I)
        filter_error = str(exc)

    resource_filter = str(resource_type or "").strip().lower()
    for line in text.split("\n"):
        match = re.match(r"^\s*(\d+)\.\s+(.+)", line)
        if not match:
            continue
        idx = int(match.group(1))
        desc = match.group(2).strip()
        url_match = re.search(r"https?://[^\s\]\)]+", desc)
        request_url = url_match.group(0).rstrip(".,;") if url_match else ""
        haystack = request_url or desc
        if url_filter:
            if _is_comment_filter_pattern(filter_arg) and request_url:
                if not _is_comment_api_request_url(request_url):
                    continue
            elif not url_filter.search(haystack):
                continue
        # MCP output does not always expose resource type. Only enforce the
        # filter when a type marker is actually present in the description.
        type_match = re.search(
            r"(?:resource[_\s-]*type|type)\s*[:=]\s*([a-z_-]+)",
            desc,
            re.I,
        )
        detected_type = type_match.group(1).lower() if type_match else ""
        if resource_filter and detected_type and detected_type != resource_filter:
            continue
        req: Dict[str, Any] = {"index": idx, "description": desc}
        if request_url:
            req["url"] = request_url
        if detected_type:
            req["resource_type"] = detected_type
        raw_requests.append(req)

    window = select_request_window(
        raw_requests,
        max_items=max_items,
        after_index=after_index_value,
    )
    matched_before_index = int(window.get("matched_before_index", 0) or 0)
    highest_index = int(window.get("highest_index", 0) or 0)
    raw_requests = [
        request for request in raw_requests
        if not after_index_value
        or int(request.get("index", 0) or 0) > after_index_value
    ]
    requests = list(window.get("requests") or [])
    body_attempts = 0
    body_successes = 0
    body_failures = 0
    body_errors: List[Dict[str, Any]] = []
    matched_urls: List[str] = [
        str(request.get("url")) for request in requests if request.get("url")
    ]
    body_read_order: List[int] = []
    if include_body:
        # Do not assume the first URL containing "comment" or "reply" is the
        # collection endpoint.  Pages commonly request status/config endpoints
        # first and the actual list later.
        body_candidates = sorted(requests, key=_network_body_priority)
        for req in body_candidates[:max_body_items]:
            idx = int(req["index"])
            body_read_order.append(idx)
            body_attempts += 1
            try:
                body_result = _network_response_body(idx, max_body_chars)
                if body_result.get("ok"):
                    body_successes += 1
                    req["response_body"] = body_result.get("body", "")
                    req["body_chars"] = body_result.get("body_chars", 0)
                    req["body_truncated"] = body_result.get("truncated", False)
                    req["response_summary"] = (
                        body_result.get("response_summary")
                        if isinstance(body_result.get("response_summary"), dict)
                        else _response_body_summary(str(body_result.get("body", "")))
                    )
                else:
                    body_failures += 1
                    req["body_error"] = body_result.get("error")
                    body_errors.append({"index": idx, "error": req["body_error"]})
            except Exception as exc:
                body_failures += 1
                req["body_error"] = str(exc)
                body_errors.append({"index": idx, "error": str(exc)})

    return {
        "ok": True,
        "requests": requests,
        "total": len(requests),
        "raw_total": sum(
            1 for line in text.split("\n")
            if re.match(r"^\s*\d+\.\s+", line)
        ),
        "matched_total": len(raw_requests),
        "matched_total_before_index": matched_before_index,
        "after_index": after_index_value or None,
        "highest_index": highest_index,
        "filter": filter_arg,
        "local_filter_applied": bool(filter_arg),
        "filter_error": filter_error,
        "resource_type_filter": resource_filter or None,
        "matched_urls": matched_urls[:max_items],
        "response_bodies_attempted": body_attempts,
        "response_bodies_succeeded": body_successes,
        "response_bodies_failed": body_failures,
        "response_body_errors": body_errors[:10],
        "response_body_read_order": body_read_order,
    }


class ActivateCommentsArgs(BaseModel):
    max_scrolls: int = Field(2, description="Maximum comment-container or window scrolls")
    pause_ms: int = Field(900, description="Pause after click/scroll")


@tool("browser_activate_comments", args_schema=ActivateCommentsArgs)
def browser_activate_comments(max_scrolls: int = 2, pause_ms: int = 900) -> Dict[str, Any]:
    """Reveal a comments component and trigger lazy requests, including open Shadow DOM."""
    max_scrolls = max(0, min(int(max_scrolls), 8))
    pause_ms = max(200, min(int(pause_ms), 3000))
    js = r"""async () => {
        const maxScrolls = __MAX_SCROLLS__;
        const pauseMs = __PAUSE_MS__;
        const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
        const visible = el => {
            if (!el || !(el instanceof Element)) return false;
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' &&
                   Number(s.opacity || 1) > 0.02 && r.width > 2 && r.height > 2;
        };
        const label = el => (
            el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || ''
        ).replace(/\s+/g, ' ').trim();
        const rootsAndElements = () => {
            const roots = [document], elements = [], seen = new Set();
            for (let i = 0; i < roots.length; i++) {
                const root = roots[i];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                let found = [];
                try { found = [...root.querySelectorAll('*')]; } catch (_) {}
                elements.push(...found);
                for (const el of found) {
                    if (el.shadowRoot && !seen.has(el.shadowRoot)) roots.push(el.shadowRoot);
                }
            }
            return {roots, elements};
        };
        const queryAllDeep = (selector) => {
            const {roots} = rootsAndElements();
            const out = [];
            for (const root of roots) {
                try { out.push(...root.querySelectorAll(selector)); } catch (_) {}
            }
            return [...new Set(out)];
        };
        const descendantCommentHints = el => {
            const selector = '[class*="comment" i],[id*="comment" i],[data-e2e*="comment" i],'
                + '[class*="reply" i],[id*="reply" i],[data-e2e*="reply" i],bili-comment-thread';
            const roots = [el], seen = new Set();
            let count = 0;
            for (let i = 0; i < roots.length; i++) {
                const root = roots[i];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                try { count += root.querySelectorAll(selector).length; } catch (_) {}
                let nested = [];
                try { nested = [...root.querySelectorAll('*')]; } catch (_) {}
                for (const child of nested) if (child.shadowRoot) roots.push(child.shadowRoot);
                if (root.shadowRoot) roots.push(root.shadowRoot);
            }
            return count;
        };
        const esc = value => window.CSS?.escape ? CSS.escape(String(value)) :
            String(value).replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
        const selectorOf = el => {
            if (el.id) return '#' + esc(el.id);
            const attrName = el.hasAttribute('data-e2e') ? 'data-e2e' :
                (el.hasAttribute('data-testid') ? 'data-testid' : '');
            const testId = attrName ? el.getAttribute(attrName) : '';
            if (testId) return `${el.tagName.toLowerCase()}[${attrName}="${testId}"]`;
            const classes = [...el.classList].filter(Boolean).slice(0, 3);
            return el.tagName.toLowerCase() + classes.map(c => '.' + esc(c)).join('');
        };

        const explicitAnchors = queryAllDeep(
            '#commentapp,#comment-app,#comments,[id*="comment" i],'
            + 'bili-comments,bili-comment-thread,[data-e2e*="comment" i],'
            + '[class*="comment-container" i],[class*="comments-container" i]'
        );
        const anchorRanked = explicitAnchors.map(el => {
            const hint = `${el.tagName} ${el.id} ${el.className || ''} ${el.getAttribute('data-e2e') || ''}`;
            let score = /bili-comments|commentapp|comment-app|comments$/i.test(hint) ? 80 : 30;
            score += Math.min(descendantCommentHints(el), 30) * 2;
            if (visible(el)) score += 10;
            return {el, score, hint};
        }).sort((a, b) => b.score - a.score);
        const anchor = anchorRanked[0] || null;
        let anchorScrolled = false;
        if (anchor) {
            try {
                anchor.el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                window.scrollBy(0, -Math.min(160, Math.floor(innerHeight * 0.15)));
                anchorScrolled = true;
                await wait(pauseMs);
            } catch (_) {}
        } else {
            // Comments on long video/article pages are often mounted only after
            // the window approaches the lower part of the document.
            const targetTop = Math.max(0, document.documentElement.scrollHeight - innerHeight * 2);
            window.scrollTo({top: targetTop, behavior: 'instant'});
            await wait(pauseMs);
        }

        const interactiveCandidates = () => queryAllDeep(
            'button,[role="button"],[role="tab"],a,li,[tabindex],'
            + '[class*="sort" i],[class*="filter" i],[class*="tab" i],span,div'
        ).filter(el => {
            if (!visible(el)) return false;
            const text = label(el);
            return text && text.length <= 100;
        });
        const controls = interactiveCandidates();
        const trigger = controls.find(el => {
            const text = label(el);
            return /^(全部)?评论(?:\s*[\d.,万w+]+)?$|^[\d.,万w+]+\s*(条)?评论$|^comments?(?:\s*[\d.,km+]+)?$/i.test(text)
                || /^(查看|展开|打开)\s*(全部)?评论$/i.test(text);
        });
        let triggerClicked = false;
        if (trigger) {
            try { trigger.click(); triggerClicked = true; await wait(pauseMs); } catch (_) {}
        }

        // Prefer a chronological/all-comments view. Some sites report
        // is_end=true for a short "hot" list even when the total count is much
        // larger, which is not a valid terminal condition for exhaustive work.
        const commentContext = el => {
            const parts = [];
            let current = el;
            for (let i = 0; current && i < 6; i++, current = current.parentElement) {
                parts.push(`${current.tagName || ''} ${current.id || ''} ${current.className || ''}`);
            }
            try {
                const host = el.getRootNode()?.host;
                if (host) parts.push(`${host.tagName || ''} ${host.id || ''} ${host.className || ''}`);
            } catch (_) {}
            return parts.join(' ');
        };
        const clickableOf = el => {
            try {
                return el.closest('button,a,li,[role="button"],[role="tab"],[tabindex]') || el;
            } catch (_) { return el; }
        };
        const rankSortControls = () => interactiveCandidates().map(el => {
            const text = label(el).replace(/\s+/g, ' ').trim();
            const chronological = /(最新评论|最新发布|最新回复|按时间(?:排序)?|时间排序|newest|most recent|chronological)/i.test(text);
            const allComments = /(全部评论|所有评论|查看全部|all comments)/i.test(text);
            const shortNewest = /^(最新|new)(?:\s*(?:评论|回复|comments?))?(?:\s*[（(]?\d+[）)]?)?$/i.test(text);
            const context = commentContext(el);
            const attrHint = `${el.tagName || ''} ${el.id || ''} ${el.className || ''} ${el.getAttribute('data-type') || ''} ${el.getAttribute('data-value') || ''}`;
            let score = chronological ? 160 : (shortNewest ? 110 : (allComments ? 80 : 0));
            if (/(comment|reply|评论|回复)/i.test(context)) score += 45;
            if (/(sort|filter|tab|time|new)/i.test(attrHint)) score += 25;
            if (el.getAttribute('role') === 'tab') score += 15;
            if (text.length > 50) score -= 80;
            const clickEl = clickableOf(el);
            return {el, clickEl, text, score, context, attrHint};
        }).filter(item => item.score >= 90).sort((a, b) => b.score - a.score);

        let sortMenuClicked = false;
        let sortRanked = rankSortControls();
        if (!sortRanked.length) {
            const menuTarget = interactiveCandidates().map(el => {
                const text = label(el).replace(/\s+/g, ' ').trim();
                const context = commentContext(el);
                const hint = `${el.id || ''} ${el.className || ''}`;
                let score = /^(排序|评论排序|最热|按热度|hot|sort)$/i.test(text) ? 80 : 0;
                if (/(comment|reply|评论|回复)/i.test(context)) score += 45;
                if (/(sort|filter)/i.test(hint)) score += 25;
                return {el: clickableOf(el), text, score};
            }).filter(item => item.score >= 90).sort((a, b) => b.score - a.score)[0] || null;
            if (menuTarget) {
                try {
                    menuTarget.el.click();
                    sortMenuClicked = true;
                    await wait(pauseMs);
                    sortRanked = rankSortControls();
                } catch (_) {}
            }
        }
        const sortTarget = sortRanked[0] || null;
        let sortClicked = false;
        let sortSelectedBefore = false;
        if (sortTarget) {
            sortSelectedBefore = sortTarget.clickEl.getAttribute('aria-selected') === 'true'
                || /(?:^|\s)(active|selected|current)(?:\s|$)/i.test(String(sortTarget.clickEl.className || ''));
            try {
                sortTarget.clickEl.click();
                sortClicked = true;
                await wait(pauseMs);
            } catch (_) {}
        }

        const deep = rootsAndElements();
        const scrollables = deep.elements.filter(el => {
            if (['BODY', 'HTML'].includes(el.tagName)) return false;
            if (!visible(el) || el.scrollHeight <= el.clientHeight + 80) return false;
            const overflow = getComputedStyle(el).overflowY;
            return ['auto','scroll','overlay'].includes(overflow) || el.scrollHeight > el.clientHeight * 1.5;
        });
        const ranked = scrollables.map(el => {
            const hint = `${el.id} ${el.className} ${el.getAttribute('data-e2e') || ''} ${el.getAttribute('aria-label') || ''}`;
            const descendantHints = descendantCommentHints(el);
            const text = (el.innerText || '').slice(0, 3000);
            let score = Math.min(descendantHints, 30) * 3;
            if (/(comment|reply|评论|回复)/i.test(hint)) score += 40;
            if (/(评论|回复|comment|reply)/i.test(text)) score += 12;
            return {el, score, hint, descendantHints};
        }).sort((a, b) => b.score - a.score);

        const selected = ranked[0]?.score > 0 ? ranked[0] : null;
        let scrolls = 0;
        let windowScrolls = anchorScrolled ? 1 : 0;
        let beforeHeight = 0;
        let afterHeight = 0;
        if (selected) {
            beforeHeight = selected.el.scrollHeight;
            for (let i = 0; i < maxScrolls; i++) {
                // Use both scrollTo and a bubbling event: virtualized lists often
                // listen on the container and do not react to window scrolling.
                selected.el.scrollTo({top: selected.el.scrollHeight, behavior: 'instant'});
                selected.el.scrollTop = selected.el.scrollHeight;
                selected.el.dispatchEvent(new Event('scroll', {bubbles: true}));
                scrolls++;
                await wait(pauseMs);
            }
            afterHeight = selected.el.scrollHeight;
        } else {
            for (let i = 0; i < maxScrolls; i++) {
                const beforeY = window.scrollY;
                window.scrollBy({top: Math.max(500, Math.floor(innerHeight * 0.8)), behavior: 'instant'});
                window.dispatchEvent(new Event('scroll'));
                if (window.scrollY === beforeY && anchor) {
                    anchor.el.scrollIntoView({block: 'end', behavior: 'instant'});
                }
                windowScrolls++;
                scrolls++;
                await wait(pauseMs);
            }
        }

        const finalDeep = rootsAndElements();
        const commentHints = finalDeep.elements.filter(el => {
            const hint = `${el.tagName} ${el.id} ${el.className || ''} ${el.getAttribute('data-e2e') || ''}`;
            return /(comment|reply|评论|回复)/i.test(hint);
        }).length;
        return JSON.stringify({
            trigger_clicked: triggerClicked,
            trigger_text: trigger ? label(trigger).slice(0, 120) : '',
            sort_clicked: sortClicked,
            sort_text: sortTarget ? sortTarget.text.slice(0, 120) : '',
            sort_selected_before: sortSelectedBefore,
            sort_menu_clicked: sortMenuClicked,
            sort_candidates: sortRanked.slice(0, 10).map(item => ({
                text: item.text.slice(0, 100),
                score: item.score,
                tag: item.clickEl.tagName || item.el.tagName || '',
                hint: String(item.attrHint || '').slice(0, 180)
            })),
            container_found: Boolean(selected),
            container_selector: selected ? selectorOf(selected.el) : '',
            container_hint: selected ? selected.hint.slice(0, 240) : '',
            anchor_found: Boolean(anchor),
            anchor_selector: anchor ? selectorOf(anchor.el) : '',
            anchor_hint: anchor ? anchor.hint.slice(0, 240) : '',
            anchor_scrolled: anchorScrolled,
            shadow_roots: Math.max(0, finalDeep.roots.length - 1),
            descendant_comment_hints: selected ? selected.descendantHints : 0,
            comment_hints_on_page: commentHints,
            scrolls,
            window_scrolls: windowScrolls,
            strategy: selected ? 'nested_container' : (anchor ? 'anchor_window' : 'document_window'),
            before_height: beforeHeight,
            after_height: afterHeight
        });
    }"""
    js = js.replace("__MAX_SCROLLS__", str(max_scrolls)).replace("__PAUSE_MS__", str(pause_ms))
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    parsed = _parse_mcp_result(result.get("text", "{}"))
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return {"ok": False, "error": "评论激活结果无法解析", "raw": parsed[:1000]}
    return {"ok": True, **(parsed if isinstance(parsed, dict) else {})}


class DiscoverCollectionPaginationArgs(BaseModel):
    anchor_selector: Optional[str] = Field(
        None,
        description="Optional collection anchor. Empty means discover structurally.",
    )
    url_pattern: Optional[str] = Field(
        None,
        description="Fallback network URL regex when no observed request-family URL exists.",
    )
    request_family_url: Optional[str] = Field(
        None,
        description=(
            "Observed collection endpoint used to match the same host and path while "
            "allowing the page to regenerate query cursors and signatures."
        ),
    )
    verification_data_path: Optional[str] = Field(
        None,
        description="Verified collection JSON path used for transaction evidence.",
    )
    verification_field_mapping: Dict[str, str] = Field(
        default_factory=dict,
        description="Verified row field mapping used only for generic fallback identities.",
    )
    baseline_row_fingerprints: List[str] = Field(
        default_factory=list,
        description="Stable row identities from the already verified baseline response.",
    )
    verification_cursor_param: Optional[str] = Field(
        None,
        description="Verified cursor query parameter; when set, it must advance.",
    )
    max_candidates: int = Field(24, description="Maximum structural controls to rank")
    max_transactions: int = Field(12, description="Maximum click/scroll transactions")
    max_scrolls: int = Field(6, description="Maximum collection scroll transactions")
    pause_ms: int = Field(900, description="Pause after each transaction")


def _request_url(request: Dict[str, Any]) -> str:
    return evidence_request_url(request)


def _request_family(url: Optional[str]) -> Dict[str, str]:
    return evidence_request_family(url)


def _request_family_pattern(family: Dict[str, str]) -> Optional[str]:
    host = str(family.get("host") or "")
    path = str(family.get("path") or "/")
    if not host:
        return None
    return rf"^https?://{re.escape(host)}{re.escape(path)}(?:[?#]|$)"


def _matches_request_family(request: Dict[str, Any], family: Dict[str, str]) -> bool:
    return evidence_matches_request_family(request, family)


def _request_state_fingerprint(request: Dict[str, Any]) -> str:
    return evidence_request_state_fingerprint(request)


def _request_indices(network: Any, family: Dict[str, str]) -> set:
    if not isinstance(network, dict):
        return set()
    return {
        int(request.get("index", 0) or 0)
        for request in (network.get("requests") or [])
        if isinstance(request, dict)
        and _matches_request_family(request, family)
        and int(request.get("index", 0) or 0) > 0
    }



class ExploreCollectionActionArgs(BaseModel):
    action: str = Field(
        ...,
        description="One safe exploration action: click, scroll, infinite_scroll, wait, or reload.",
    )
    target: Optional[str] = Field(
        None,
        description="CSS selector or @ref for click. The Agent chooses this from current observations.",
    )
    direction: str = Field("down", description="Scroll direction: up or down")
    amount: int = Field(2, description="Number of PageUp/PageDown operations")
    max_scrolls: int = Field(4, description="Maximum scrolls for infinite_scroll")
    wait_ms: int = Field(900, description="Settling time after the action")
    url_pattern: Optional[str] = Field(
        None,
        description="Optional fallback URL regex when no verified request family is available.",
    )
    request_family_url: Optional[str] = Field(
        None,
        description="Verified baseline endpoint. Matching uses host/path and ignores signatures.",
    )
    verification_data_path: Optional[str] = Field(None)
    verification_field_mapping: Dict[str, str] = Field(default_factory=dict)
    baseline_row_fingerprints: List[str] = Field(default_factory=list)
    verification_cursor_param: Optional[str] = Field(None)


@tool("browser_explore_collection_action", args_schema=ExploreCollectionActionArgs)
def browser_explore_collection_action(
    action: str,
    target: Optional[str] = None,
    direction: str = "down",
    amount: int = 2,
    max_scrolls: int = 4,
    wait_ms: int = 900,
    url_pattern: Optional[str] = None,
    request_family_url: Optional[str] = None,
    verification_data_path: Optional[str] = None,
    verification_field_mapping: Optional[Dict[str, str]] = None,
    baseline_row_fingerprints: Optional[List[str]] = None,
    verification_cursor_param: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute one Agent-chosen action and immediately return its network delta.

    This tool deliberately does not choose a tab, button, selector, or scroll
    strategy.  The Browser Agent owns that decision and can adapt its next
    action from the returned snapshot and response evidence.  The deterministic
    layer only freezes response bodies and evaluates a previously verified
    request/schema contract.
    """
    action_name = str(action or "").strip().lower()
    allowed_actions = {"click", "scroll", "infinite_scroll", "wait", "reload"}
    if action_name not in allowed_actions:
        return {
            "ok": False,
            "error": "unsupported_exploration_action",
            "allowed_actions": sorted(allowed_actions),
        }
    direction = "up" if str(direction).strip().lower() == "up" else "down"
    amount = max(1, min(int(amount), 8))
    max_scrolls = max(1, min(int(max_scrolls), 10))
    wait_ms = max(250, min(int(wait_ms), 5000))

    family = _request_family(request_family_url)
    family_pattern = _request_family_pattern(family)
    network_pattern = family_pattern or url_pattern
    # Capture the global delta as well as the current family. The seed endpoint
    # is only a hypothesis: an Agent action may reveal a better collection API.
    before_network = browser_network_log.invoke({
        "url_pattern": None,
        "include_body": False,
        "max_items": 100,
    })
    before_network = before_network if isinstance(before_network, dict) else {}
    before_indices = {
        int(item.get("index", 0) or 0)
        for item in (before_network.get("requests") or [])
        if isinstance(item, dict) and int(item.get("index", 0) or 0) > 0
    }
    high_water = max(
        max(before_indices, default=0),
        int(before_network.get("highest_index", 0) or 0),
    )

    target_descriptor: Dict[str, Any] = {"requested": target}
    if action_name == "click" and str(target or "").strip():
        try:
            before_snapshot = browser_snapshot.invoke({
                "max_text_chars": 8000,
                "max_links": 40,
            })
        except Exception:
            before_snapshot = {}
        snapshot_text = str(
            before_snapshot.get("snapshot") or ""
        ) if isinstance(before_snapshot, dict) else ""
        ref_value = str(target or "").strip().lstrip("@")
        target_line = next(
            (
                line.strip() for line in snapshot_text.splitlines()
                if ref_value and f"[ref={ref_value}]" in line
            ),
            "",
        )
        role_match = re.search(
            r"\b(button|tab|menuitem|option|link|listitem)\b",
            target_line,
            re.I,
        )
        name_match = re.search(r'["\']([^"\']{1,160})["\']', target_line)
        target_descriptor.update({
            "ref": ref_value if re.fullmatch(r"e\d+", ref_value) else None,
            "role": role_match.group(1).lower() if role_match else None,
            "name": name_match.group(1).strip() if name_match else None,
            "snapshot_line": target_line[:500],
        })

    if action_name == "click":
        if not str(target or "").strip():
            return {"ok": False, "error": "click_target_required"}
        action_result = browser_click.invoke({"selector": str(target)})
    elif action_name == "scroll":
        action_result = browser_scroll.invoke({"direction": direction, "amount": amount})
    elif action_name == "infinite_scroll":
        action_result = browser_infinite_scroll.invoke({
            "max_scrolls": max_scrolls,
            "pause_ms": wait_ms,
        })
    elif action_name == "reload":
        action_result = browser_reload.invoke({})
    else:
        time.sleep(wait_ms / 1000)
        action_result = {"ok": True, "waited_ms": wait_ms}

    if action_name != "wait":
        time.sleep(wait_ms / 1000)
    after_network = browser_network_log.invoke({
        "url_pattern": None,
        "include_body": True,
        "max_items": 100,
        "max_body_items": 30,
        "max_body_chars": 1_000_000,
        "after_index": high_water if high_water > 0 else None,
    })
    after_network = after_network if isinstance(after_network, dict) else {}
    all_requests = [
        dict(item) for item in (after_network.get("requests") or [])
        if isinstance(item, dict)
        and int(item.get("index", 0) or 0) > high_water
    ]
    family_requests = [
        item for item in all_requests
        if not family or _matches_request_family(item, family)
    ]
    frozen_requests = freeze_request_evidence(family_requests)
    frozen_discovery_requests = freeze_request_evidence(all_requests)
    alternate_families = []
    seen_alternate_families = set()
    for request in frozen_discovery_requests:
        request_family_value = _request_family(_request_url(request))
        family_key = (
            request_family_value.get("host"),
            request_family_value.get("path"),
        )
        if not request_family_value or request_family_value == family:
            continue
        if family_key in seen_alternate_families:
            continue
        seen_alternate_families.add(family_key)
        alternate_families.append(request_family_value)

    data_path = str(verification_data_path or "").strip()
    mapping = (
        dict(verification_field_mapping)
        if isinstance(verification_field_mapping, dict)
        else {}
    )
    cursor_param = str(verification_cursor_param or "").strip()
    contract_ready = bool(family and data_path)
    baseline_states = {
        _request_state_fingerprint({"url": str(request_family_url or "")})
    } if family else set()
    baseline_cursor_values = {
        evidence_request_query_value(
            {"url": str(request_family_url or "")}, cursor_param
        )
    } if cursor_param else set()
    evaluation = evaluate_transaction_evidence(
        baseline_states=baseline_states,
        baseline_rows={
            str(value) for value in (baseline_row_fingerprints or []) if str(value)
        },
        requests=frozen_requests,
        data_path=data_path,
        field_mapping=mapping,
        cursor_param=cursor_param or None,
        baseline_cursor_values=baseline_cursor_values,
    ) if contract_ready else {
        "accepted": False,
        "reason": "verification_contract_missing",
        "request_count": len(frozen_requests),
        "body_count": sum(bool(item.get("response_body")) for item in frozen_requests),
        "new_request_state_count": 0,
        "new_cursor_state_count": 0,
        "new_unique_row_count": 0,
    }

    try:
        snapshot = browser_snapshot.invoke({"max_text_chars": 2800, "max_links": 30})
    except Exception as exc:
        snapshot = {"ok": False, "error": str(exc)}
    return {
        "ok": bool(
            isinstance(action_result, dict)
            and action_result.get("ok", not bool(action_result.get("error")))
        ),
        "strategy": "agent_chosen_action_with_atomic_network_feedback",
        "action": {
            "name": action_name,
            "target": target,
            "target_descriptor": target_descriptor if action_name == "click" else None,
            "direction": direction if action_name == "scroll" else None,
            "amount": amount if action_name == "scroll" else None,
            "max_scrolls": max_scrolls if action_name == "infinite_scroll" else None,
            "wait_ms": wait_ms,
            "result": action_result,
        },
        "request_family": family,
        "family_filter": network_pattern,
        "high_water_index": high_water,
        "discovery_request_count": len(frozen_discovery_requests),
        "alternate_request_families": alternate_families[:20],
        "new_request_count": len(frozen_requests),
        "new_request_indices": [
            int(item.get("index", 0) or 0) for item in frozen_requests
        ],
        "new_request_state_count": int(
            evaluation.get("new_request_state_count", 0) or 0
        ),
        "new_cursor_state_count": int(
            evaluation.get("new_cursor_state_count", 0) or 0
        ),
        "new_unique_row_count": int(
            evaluation.get("new_unique_row_count", 0) or 0
        ),
        "accepted": bool(evaluation.get("accepted")),
        "acceptance_reason": evaluation.get("reason"),
        "verification_contract_ready": contract_ready,
        "network": {
            **after_network,
            "requests": frozen_discovery_requests,
            "total": len(frozen_discovery_requests),
            "matched_total": len(frozen_discovery_requests),
            "atomic_capture": True,
        },
        "evidence_network": {
            "ok": True,
            "requests": frozen_requests,
            "total": len(frozen_requests),
            "matched_total": len(frozen_requests),
            "filter": network_pattern,
            "atomic_capture": True,
        },
        "snapshot": snapshot,
        "error": (
            str(action_result.get("error") or "")[:500]
            if isinstance(action_result, dict)
            else ""
        ),
    }


@tool("browser_discover_collection_pagination", args_schema=DiscoverCollectionPaginationArgs)
def browser_discover_collection_pagination(
    anchor_selector: Optional[str] = None,
    url_pattern: Optional[str] = None,
    request_family_url: Optional[str] = None,
    verification_data_path: Optional[str] = None,
    verification_field_mapping: Optional[Dict[str, str]] = None,
    baseline_row_fingerprints: Optional[List[str]] = None,
    verification_cursor_param: Optional[str] = None,
    max_candidates: int = 24,
    max_transactions: int = 12,
    max_scrolls: int = 6,
    pause_ms: int = 900,
) -> Dict[str, Any]:
    """Discover collection pagination with isolated, evidence-bound transactions.

    One candidate is clicked, its collection is scrolled, and the MCP network
    delta is inspected immediately.  A failed candidate is rolled back before
    another candidate is considered.  Matching uses the observed endpoint's
    host/path, never a site adapter or a replayed signed URL.
    """
    max_candidates = max(1, min(int(max_candidates), 60))
    max_transactions = max(1, min(int(max_transactions), 20))
    max_scrolls = max(1, min(int(max_scrolls), 10))
    pause_ms = max(250, min(int(pause_ms), 3000))
    family = _request_family(request_family_url)
    family_pattern = _request_family_pattern(family)
    network_pattern = family_pattern or url_pattern
    verification_path = str(verification_data_path or "").strip()
    verification_mapping = (
        dict(verification_field_mapping)
        if isinstance(verification_field_mapping, dict)
        else {}
    )
    baseline_rows = {
        str(value) for value in (baseline_row_fingerprints or []) if str(value)
    }
    cursor_param = str(verification_cursor_param or "").strip()
    baseline_cursor_values = {
        evidence_request_query_value({"url": str(request_family_url or "")}, cursor_param)
    } if cursor_param else set()
    verification_contract_ready = bool(family and verification_path)

    def read_network(
        include_body: bool = False,
        after_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        value = browser_network_log.invoke({
            "url_pattern": network_pattern,
            "include_body": include_body,
            "max_items": 100,
            "max_body_items": 50 if include_body else 0,
            "max_body_chars": 1_000_000,
            "after_index": after_index,
        })
        return value if isinstance(value, dict) else {"ok": False}

    def capture_bodies(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Freeze bodies by exact index before another click, rollback or scan."""
        captured: List[Dict[str, Any]] = []
        for request in requests[:30]:
            if not isinstance(request, dict):
                continue
            frozen = dict(request)
            index = int(frozen.get("index", 0) or 0)
            if index and not frozen.get("response_body"):
                try:
                    body_result = _network_response_body(index, 1_000_000)
                except Exception as exc:
                    body_result = {"ok": False, "error": str(exc)}
                if isinstance(body_result, dict) and body_result.get("ok"):
                    frozen["response_body"] = str(body_result.get("body") or "")
                    frozen["body_chars"] = body_result.get("body_chars", 0)
                    frozen["body_truncated"] = body_result.get("truncated", False)
                    frozen["response_summary"] = _response_body_summary(
                        frozen["response_body"]
                    )
                else:
                    frozen["body_error"] = str(
                        (body_result or {}).get("error", "body_capture_failed")
                    )[:500]
            captured.append(frozen)
        return captured

    initial_network = read_network(False)
    initial_indices = _request_indices(initial_network, family)
    initial_states = {
        _request_state_fingerprint(request)
        for request in (initial_network.get("requests") or [])
        if isinstance(request, dict) and _matches_request_family(request, family)
    }

    setup_js = r'''async () => {
        const key = '__crawlerCollectionExplorerV31';
        const anchorSelector = __ANCHOR_SELECTOR__;
        const maxCandidates = __MAX_CANDIDATES__;
        const pauseMs = __PAUSE_MS__;
        const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
        const visible = el => {
            if (!el || !(el instanceof Element) || !el.isConnected) return false;
            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) > 0.02 && rect.width > 4 && rect.height > 4;
        };
        const label = el => String(
            el?.getAttribute?.('aria-label') || el?.getAttribute?.('title')
            || el?.innerText || el?.textContent || ''
        ).replace(/\s+/g, ' ').trim();
        const collectDeep = () => {
            const roots = [document], elements = [], seen = new Set();
            for (let index = 0; index < roots.length; index++) {
                const root = roots[index];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                let found = [];
                try { found = [...root.querySelectorAll('*')]; } catch (_) {}
                elements.push(...found);
                for (const el of found) if (el.shadowRoot) roots.push(el.shadowRoot);
            }
            return {roots, elements: [...new Set(elements)]};
        };
        const queryDeep = selector => {
            const output = [];
            for (const root of collectDeep().roots) {
                try { output.push(...root.querySelectorAll(selector)); } catch (_) {}
            }
            return [...new Set(output)];
        };
        const composedParent = el => {
            if (!el) return null;
            if (el.parentElement) return el.parentElement;
            try { return el.getRootNode()?.host || null; } catch (_) { return null; }
        };
        const composedContains = (ancestor, el) => {
            let current = el;
            for (let depth = 0; ancestor && current && depth < 40; depth++) {
                if (current === ancestor) return true;
                current = composedParent(current);
            }
            return false;
        };
        const selectedLike = el => {
            const cls = String(el?.className || '');
            return el?.getAttribute?.('aria-selected') === 'true'
                || el?.getAttribute?.('aria-checked') === 'true'
                || el?.getAttribute?.('data-state') === 'active'
                || /(^|[\s_-])(active|selected|current|checked)([\s_-]|$)/i.test(cls);
        };
        const descriptor = (el, explorerId = '') => {
            const rect = el.getBoundingClientRect();
            return {
                explorer_id: explorerId,
                tag: String(el.tagName || '').toLowerCase(),
                role: String(el.getAttribute('role') || '').toLowerCase(),
                text: label(el).slice(0, 120),
                id: String(el.id || '').slice(0, 100),
                class_hint: String(el.className || '').slice(0, 180),
                aria_selected: el.getAttribute('aria-selected'),
                selected: selectedLike(el),
                rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                    width: Math.round(rect.width), height: Math.round(rect.height)}
            };
        };
        let deep = collectDeep();
        let anchor = null;
        if (anchorSelector) anchor = queryDeep(anchorSelector).find(visible) || null;
        if (!anchor) {
            const anchors = queryDeep(
                '[id*="comment" i],[class*="comment" i],[data-e2e*="comment" i],'
                + '[id*="reply" i],[class*="reply" i],[data-e2e*="reply" i],'
                + '[role="feed"],[role="list"],main,article'
            ).filter(visible);
            anchors.sort((a, b) => {
                const score = el => {
                    const hint = `${el.tagName} ${el.id} ${el.className || ''} ${el.getAttribute('role') || ''}`;
                    const rect = el.getBoundingClientRect();
                    return (/(comment|reply|feed|list)/i.test(hint) ? 120 : 0)
                        + Math.min(60, el.querySelectorAll?.('[role="listitem"],li,article')?.length || 0)
                        + Math.min(30, Math.round(rect.height / 100));
                };
                return score(b) - score(a);
            });
            anchor = anchors[0] || null;
        }
        if (anchor) {
            try {
                anchor.scrollIntoView({block: 'start', inline: 'nearest', behavior: 'instant'});
                await wait(pauseMs);
            } catch (_) {}
        }
        deep = collectDeep();
        const anchorRect = anchor?.getBoundingClientRect?.() || null;
        const unsafe = /(delete|remove|purchase|buy|pay|submit|logout|sign out|publish|send|删除|移除|购买|支付|提交|退出登录|发布|发送)/i;
        const structural = deep.elements.filter(el => {
            if (!visible(el)) return false;
            if (anchor && !composedContains(anchor, el) && !composedContains(el, anchor)) return false;
            const rect = el.getBoundingClientRect();
            if (rect.width > innerWidth * 0.96 && rect.height > innerHeight * 0.7) return false;
            const style = getComputedStyle(el);
            const role = String(el.getAttribute('role') || '').toLowerCase();
            const tag = String(el.tagName || '').toUpperCase();
            const text = label(el);
            if (unsafe.test(text)) return false;
            if (tag === 'BUTTON' && String(el.getAttribute('type') || '').toLowerCase() === 'submit') return false;
            if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return false;
            return ['BUTTON', 'LI', 'SUMMARY'].includes(tag)
                || (tag === 'A' && (!el.href || el.origin === location.origin))
                || ['button', 'tab', 'menuitem', 'option', 'switch'].includes(role)
                || el.hasAttribute('tabindex') || style.cursor === 'pointer';
        });
        const ranked = structural.map(el => {
            const text = label(el);
            const hint = `${el.tagName} ${el.id} ${el.className || ''} ${el.getAttribute('role') || ''}`;
            const role = String(el.getAttribute('role') || '').toLowerCase();
            const tag = String(el.tagName || '').toUpperCase();
            const rect = el.getBoundingClientRect();
            const parent = composedParent(el);
            let peers = [];
            try { peers = [...(parent?.children || [])].filter(visible); } catch (_) {}
            const hasActivePeer = peers.some(peer => peer !== el && selectedLike(peer));
            const childControls = el.querySelectorAll?.('button,a,[role="button"],[role="tab"],[tabindex]')?.length || 0;
            let score = anchor && composedContains(anchor, el) ? 100 : 20;
            if (role === 'tab') score += 110;
            else if (['menuitem', 'option'].includes(role)) score += 80;
            else if (role === 'button') score += 45;
            if (['BUTTON', 'LI', 'SUMMARY'].includes(tag)) score += 30;
            if (hasActivePeer && !selectedLike(el)) score += 100;
            if (el.hasAttribute('aria-selected') || el.hasAttribute('aria-checked')) score += 45;
            if (/(sort|filter|tab|page|next|more|load|time|new|recent|all|comment|reply|排序|筛选|最新|全部|加载|更多|下一)/i.test(`${hint} ${text}`)) score += 60;
            if (anchorRect && rect.top <= anchorRect.top + Math.max(260, anchorRect.height * 0.3)) score += 35;
            if (selectedLike(el)) score -= 70;
            if (childControls > 3) score -= Math.min(100, childControls * 10);
            if (!text) score -= 15;
            return {el, score, hasActivePeer};
        }).sort((a, b) => b.score - a.score).slice(0, maxCandidates);
        ranked.forEach((item, index) => { item.explorerId = `c${index}`; });
        window[key] = {anchor, ranked, pageUrl: location.href, last: null};
        return JSON.stringify({
            anchor_found: Boolean(anchor),
            anchor: anchor ? descriptor(anchor) : null,
            page_url: location.href,
            shadow_roots: Math.max(0, deep.roots.length - 1),
            candidate_count: ranked.length,
            candidates: ranked.map(item => ({
                score: item.score,
                has_active_peer: item.hasActivePeer,
                ...descriptor(item.el, item.explorerId)
            }))
        });
    }'''
    setup_js = (
        setup_js.replace("__ANCHOR_SELECTOR__", json.dumps(anchor_selector or ""))
        .replace("__MAX_CANDIDATES__", str(max_candidates))
        .replace("__PAUSE_MS__", str(pause_ms))
    )

    def setup_registry() -> Dict[str, Any]:
        result = _mcp().call_tool("browser_evaluate", {"function": setup_js})
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "candidate discovery failed")}
        parsed = _parse_mcp_result(result.get("text", "{}"))
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                return {"ok": False, "error": "candidate discovery result is not JSON"}
        return {"ok": True, **(parsed if isinstance(parsed, dict) else {})}

    first_setup = setup_registry()
    if not first_setup.get("ok"):
        return {
            "ok": False,
            "error": first_setup.get("error", "collection exploration failed"),
            "request_family": family,
            "baseline": initial_network,
        }
    accessibility_reserve = min(4, max(2, max_transactions // 4))
    dom_transaction_limit = max(1, max_transactions - accessibility_reserve)
    candidates = list(first_setup.get("candidates") or [])[:dom_transaction_limit]
    page_url = str(first_setup.get("page_url") or "")
    transactions: List[Dict[str, Any]] = []
    success_indices: set = set()
    success_requests: List[Dict[str, Any]] = []
    success_evaluation: Dict[str, Any] = {}
    success_candidate: Optional[Dict[str, Any]] = None
    resource_delta_count = 0

    transaction_template = r'''async () => {
        const key = '__crawlerCollectionExplorerV31';
        const candidateId = __CANDIDATE_ID__;
        const maxScrolls = __MAX_SCROLLS__;
        const pauseMs = __PAUSE_MS__;
        const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
        const registry = window[key];
        const item = registry?.ranked?.find(value => value.explorerId === candidateId);
        const el = item?.el;
        if (!el || !el.isConnected) return JSON.stringify({ok: false, error: 'candidate_detached'});
        const visible = node => {
            if (!node || !(node instanceof Element) || !node.isConnected) return false;
            const style = getComputedStyle(node), rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 4 && rect.height > 4;
        };
        const selectedLike = node => {
            const cls = String(node?.className || '');
            return node?.getAttribute?.('aria-selected') === 'true'
                || node?.getAttribute?.('aria-checked') === 'true'
                || node?.getAttribute?.('data-state') === 'active'
                || /(^|[\s_-])(active|selected|current|checked)([\s_-]|$)/i.test(cls);
        };
        const collectDeep = () => {
            const roots = [document], elements = [], seen = new Set();
            for (let index = 0; index < roots.length; index++) {
                const root = roots[index];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                let found = [];
                try { found = [...root.querySelectorAll('*')]; } catch (_) {}
                elements.push(...found);
                for (const node of found) if (node.shadowRoot) roots.push(node.shadowRoot);
            }
            return [...new Set(elements)];
        };
        const composedParent = node => node?.parentElement || node?.getRootNode?.()?.host || null;
        const composedContains = (ancestor, node) => {
            let current = node;
            for (let depth = 0; ancestor && current && depth < 40; depth++) {
                if (current === ancestor) return true;
                current = composedParent(current);
            }
            return false;
        };
        const parent = composedParent(el);
        let peers = [];
        try { peers = [...(parent?.children || [])].filter(visible); } catch (_) {}
        const previousActive = peers.find(peer => peer !== el && selectedLike(peer)) || null;
        const resourceBefore = new Set(
            performance.getEntriesByType('resource')
                .filter(entry => ['fetch', 'xmlhttprequest'].includes(String(entry.initiatorType || '').toLowerCase()))
                .map(entry => entry.name)
        );
        const beforeWindowY = window.scrollY;
        let clicked = false, error = '';
        try {
            el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
            el.click();
            clicked = true;
            await wait(pauseMs);
        } catch (exc) { error = String(exc); }
        const anchor = registry.anchor?.isConnected ? registry.anchor : null;
        const scrollables = collectDeep().filter(node => {
            if (!visible(node) || (anchor && !composedContains(anchor, node))) return false;
            const style = getComputedStyle(node);
            return node.scrollHeight > node.clientHeight + 80
                && (['auto', 'scroll', 'overlay'].includes(style.overflowY)
                    || node.scrollHeight > node.clientHeight * 1.5);
        }).sort((a, b) => {
            const score = node => {
                const hint = `${node.tagName} ${node.id} ${node.className || ''} ${node.getAttribute('role') || ''}`;
                const rows = node.querySelectorAll?.('[role="listitem"],li,article,[class*="item" i]')?.length || 0;
                return (node.scrollHeight - node.clientHeight) + Math.min(rows, 100) * 500
                    + (/(list|feed|comment|reply|result|item)/i.test(hint) ? 20000 : 0);
            };
            return score(b) - score(a);
        });
        const scrollTarget = scrollables[0] || null;
        const scrollPositions = scrollables.slice(0, 3).map(node => ({node, top: node.scrollTop}));
        const scrollEvents = [];
        for (let index = 0; index < maxScrolls; index++) {
            const useContainer = Boolean(scrollTarget && index < Math.max(1, maxScrolls - 2));
            let before, after;
            if (useContainer) {
                before = scrollTarget.scrollTop;
                scrollTarget.scrollTop = Math.min(
                    scrollTarget.scrollHeight,
                    scrollTarget.scrollTop + Math.max(450, Math.floor(scrollTarget.clientHeight * 0.9))
                );
                scrollTarget.dispatchEvent(new Event('scroll', {bubbles: true}));
                after = scrollTarget.scrollTop;
            } else {
                before = window.scrollY;
                window.scrollBy({top: Math.max(600, Math.floor(innerHeight * 0.9)), behavior: 'instant'});
                window.dispatchEvent(new Event('scroll'));
                after = window.scrollY;
            }
            await wait(pauseMs);
            scrollEvents.push({kind: useContainer ? 'container' : 'window', before: Math.round(before), after: Math.round(after)});
        }
        registry.last = {previousActive, beforeWindowY, scrollPositions};
        const resources = performance.getEntriesByType('resource')
            .filter(entry => ['fetch', 'xmlhttprequest'].includes(String(entry.initiatorType || '').toLowerCase()))
            .map(entry => entry.name).filter(url => !resourceBefore.has(url));
        return JSON.stringify({
            ok: clicked, clicked, error: error.slice(0, 240),
            previous_active_found: Boolean(previousActive),
            selected_after: selectedLike(el),
            scroll_events: scrollEvents,
            new_resources: resources.slice(0, 30)
        });
    }'''

    rollback_js = r'''async () => {
        const registry = window.__crawlerCollectionExplorerV31;
        const state = registry?.last;
        if (!state) return JSON.stringify({ok: false, selection_restored: false, error: 'rollback_state_missing'});
        const pauseMs = __PAUSE_MS__;
        let selectionRestored = false, error = '';
        try {
            if (state.previousActive?.isConnected) {
                state.previousActive.click();
                selectionRestored = true;
                await new Promise(resolve => setTimeout(resolve, pauseMs));
            }
            for (const item of state.scrollPositions || []) {
                if (item.node?.isConnected) item.node.scrollTop = item.top;
            }
            window.scrollTo({top: state.beforeWindowY, behavior: 'instant'});
        } catch (exc) { error = String(exc); }
        registry.last = null;
        return JSON.stringify({ok: !error, selection_restored: selectionRestored, error: error.slice(0, 240)});
    }'''.replace("__PAUSE_MS__", str(pause_ms))

    for ordinal, _original_candidate in enumerate(candidates):
        if len(transactions) >= max_transactions:
            break
        current_setup = first_setup if ordinal == 0 else setup_registry()
        current_candidates = list(current_setup.get("candidates") or []) if current_setup.get("ok") else []
        if ordinal >= len(current_candidates):
            break
        candidate = current_candidates[ordinal]
        candidate_id = str(candidate.get("explorer_id") or f"c{ordinal}")
        before_network = read_network(False)
        before_indices = _request_indices(before_network, family)
        before_highest = max(
            max(before_indices, default=0),
            int(before_network.get("highest_index", 0) or 0),
        )
        transaction_js = (
            transaction_template.replace("__CANDIDATE_ID__", json.dumps(candidate_id))
            .replace("__MAX_SCROLLS__", str(max_scrolls))
            .replace("__PAUSE_MS__", str(pause_ms))
        )
        call_result = _mcp().call_tool("browser_evaluate", {"function": transaction_js})
        parsed_call = _parse_mcp_result(call_result.get("text", "{}")) if call_result.get("ok") else {}
        if isinstance(parsed_call, str):
            try:
                parsed_call = json.loads(parsed_call)
            except json.JSONDecodeError:
                parsed_call = {"ok": False, "error": "transaction_result_not_json"}
        parsed_call = parsed_call if isinstance(parsed_call, dict) else {}
        after_network = read_network(
            False,
            after_index=before_highest if before_highest > 0 else None,
        )
        new_indices = {
            index for index in _request_indices(after_network, family)
            if index > before_highest and index not in before_indices
        }
        new_family_requests = [
            request for request in (after_network.get("requests") or [])
            if isinstance(request, dict)
            and int(request.get("index", 0) or 0) in new_indices
            and _matches_request_family(request, family)
        ]
        frozen_requests = capture_bodies(new_family_requests)
        evidence_evaluation = evaluate_transaction_evidence(
            baseline_states=initial_states,
            baseline_rows=baseline_rows,
            requests=frozen_requests,
            data_path=verification_path,
            field_mapping=verification_mapping,
            cursor_param=cursor_param or None,
            baseline_cursor_values=baseline_cursor_values,
        ) if verification_contract_ready else {
            "accepted": False,
            "reason": "verification_contract_missing",
            "body_count": sum(
                1 for request in frozen_requests if request.get("response_body")
            ),
            "new_request_state_count": 0,
            "new_unique_row_count": 0,
        }
        transaction = {
            "kind": "isolated_dom_candidate",
            "ordinal": ordinal,
            "target": candidate,
            "clicked": bool(parsed_call.get("clicked")),
            "previous_active_found": bool(parsed_call.get("previous_active_found")),
            "selected_after": parsed_call.get("selected_after"),
            "scroll_events": (parsed_call.get("scroll_events") or [])[:10],
            "resource_delta_count": len(parsed_call.get("new_resources") or []),
            "new_family_request_count": len(frozen_requests),
            "captured_body_count": int(evidence_evaluation.get("body_count", 0) or 0),
            "new_request_state_count": int(
                evidence_evaluation.get("new_request_state_count", 0) or 0
            ),
            "new_unique_row_count": int(
                evidence_evaluation.get("new_unique_row_count", 0) or 0
            ),
            "new_cursor_state_count": int(
                evidence_evaluation.get("new_cursor_state_count", 0) or 0
            ),
            "acceptance_reason": evidence_evaluation.get("reason"),
            "new_request_indices": sorted(new_indices)[:30],
            "error": str(parsed_call.get("error") or call_result.get("error") or "")[:240],
        }
        resource_delta_count += transaction["resource_delta_count"]
        transactions.append(transaction)
        if evidence_evaluation.get("accepted"):
            success_indices = set(new_indices)
            success_requests = freeze_request_evidence(frozen_requests)
            success_evaluation = dict(evidence_evaluation)
            success_candidate = candidate
            transaction["accepted"] = True
            break

        rollback_result = _mcp().call_tool("browser_evaluate", {"function": rollback_js})
        rollback_value = _parse_mcp_result(rollback_result.get("text", "{}")) if rollback_result.get("ok") else {}
        if isinstance(rollback_value, str):
            try:
                rollback_value = json.loads(rollback_value)
            except json.JSONDecodeError:
                rollback_value = {}
        selection_restored = bool(
            isinstance(rollback_value, dict) and rollback_value.get("selection_restored")
        )
        transaction["rollback"] = {
            "selection_restored": selection_restored,
            "error": str(
                rollback_value.get("error") if isinstance(rollback_value, dict) else ""
            )[:240],
        }
        if not selection_restored and page_url:
            reload_result = _mcp().call_tool("browser_navigate", {"url": page_url})
            transaction["rollback"]["reloaded"] = bool(reload_result.get("ok"))
            if reload_result.get("ok"):
                time.sleep(min(6.0, pause_ms * 2 / 1000))

    accessibility_transactions: List[Dict[str, Any]] = []
    # Closed shadow roots are not visible to page JavaScript. Accessibility
    # candidates are also tested one at a time and rolled back/reloaded before
    # another candidate is resolved from a fresh snapshot.
    if not success_indices and len(transactions) < max_transactions:
        snapshot = _mcp().call_tool("browser_snapshot", {})
        snapshot_text = str(snapshot.get("text", "")) if snapshot.get("ok") else ""
        safe_labels: List[Dict[str, str]] = []
        for line in snapshot_text.splitlines():
            ref_match = re.search(r"\[ref=(e\d+)\]", line)
            role_match = re.search(r"\b(button|tab|menuitem|option)\b", line, re.I)
            if not ref_match or not role_match:
                continue
            if re.search(
                r"delete|remove|purchase|buy|pay|submit|logout|sign out|"
                r"删除|移除|购买|支付|提交|退出登录",
                line,
                re.I,
            ):
                continue
            role = role_match.group(1).lower()
            safe_label = bool(re.search(
                r"\b(?:sort|filter|load|more|next|all|new|recent|comment|reply)\b|"
                r"排序|筛选|加载|更多|下一|全部|最新|评论|回复",
                line,
                re.I,
            ))
            if role not in {"tab", "menuitem", "option"} and not safe_label:
                continue
            signature = re.sub(r"\[ref=e\d+\]", "[ref]", line.strip())
            if not any(item["signature"] == signature for item in safe_labels):
                safe_labels.append({"role": role, "signature": signature, "label": line.strip()[:240]})
            if len(safe_labels) >= 5:
                break

        scroll_only_js = r'''async () => {
            const maxScrolls = __MAX_SCROLLS__, pauseMs = __PAUSE_MS__;
            const registry = window.__crawlerCollectionExplorerV31;
            const anchor = registry?.anchor?.isConnected ? registry.anchor : null;
            if (anchor) anchor.scrollIntoView({block: 'start', behavior: 'instant'});
            for (let index = 0; index < maxScrolls; index++) {
                window.scrollBy({top: Math.max(600, Math.floor(innerHeight * 0.9)), behavior: 'instant'});
                window.dispatchEvent(new Event('scroll'));
                await new Promise(resolve => setTimeout(resolve, pauseMs));
            }
            return JSON.stringify({ok: true, scrolls: maxScrolls});
        }'''.replace("__MAX_SCROLLS__", str(max_scrolls)).replace("__PAUSE_MS__", str(pause_ms))

        for descriptor in safe_labels:
            if len(transactions) + len(accessibility_transactions) >= max_transactions:
                break
            fresh_snapshot = _mcp().call_tool("browser_snapshot", {})
            fresh_text = str(fresh_snapshot.get("text", "")) if fresh_snapshot.get("ok") else ""
            current_ref = ""
            selected_ref = ""
            for line in fresh_text.splitlines():
                ref_match = re.search(r"\[ref=(e\d+)\]", line)
                if not ref_match:
                    continue
                signature = re.sub(r"\[ref=e\d+\]", "[ref]", line.strip())
                if signature == descriptor["signature"]:
                    current_ref = ref_match.group(1)
                line_role = re.search(r"\b(button|tab|menuitem|option)\b", line, re.I)
                safe_selected = bool(re.search(
                    r"\b(?:sort|filter|load|more|next|all|new|recent|comment|reply)\b|"
                    r"排序|筛选|加载|更多|下一|全部|最新|评论|回复",
                    line,
                    re.I,
                ))
                if (
                    line_role
                    and line_role.group(1).lower() == descriptor["role"]
                    and safe_selected
                    and re.search(r"selected|checked|current", line, re.I)
                ):
                    selected_ref = selected_ref or ref_match.group(1)
            if not current_ref:
                continue
            before_network = read_network(False)
            before_indices = _request_indices(before_network, family)
            before_highest = max(
                max(before_indices, default=0),
                int(before_network.get("highest_index", 0) or 0),
            )
            click_result = _mcp().call_tool("browser_click", {"target": current_ref})
            time.sleep(pause_ms / 1000)
            _mcp().call_tool("browser_evaluate", {"function": scroll_only_js})
            after_network = read_network(
                False,
                after_index=before_highest if before_highest > 0 else None,
            )
            new_indices = {
                index for index in _request_indices(after_network, family)
                if index > before_highest and index not in before_indices
            }
            new_family_requests = [
                request for request in (after_network.get("requests") or [])
                if isinstance(request, dict)
                and int(request.get("index", 0) or 0) in new_indices
                and _matches_request_family(request, family)
            ]
            frozen_requests = capture_bodies(new_family_requests)
            evidence_evaluation = evaluate_transaction_evidence(
                baseline_states=initial_states,
                baseline_rows=baseline_rows,
                requests=frozen_requests,
                data_path=verification_path,
                field_mapping=verification_mapping,
                cursor_param=cursor_param or None,
                baseline_cursor_values=baseline_cursor_values,
            ) if verification_contract_ready else {
                "accepted": False,
                "reason": "verification_contract_missing",
                "body_count": sum(
                    1 for request in frozen_requests if request.get("response_body")
                ),
                "new_request_state_count": 0,
                "new_unique_row_count": 0,
            }
            record = {
                "kind": "isolated_accessibility_candidate",
                **descriptor,
                "ok": bool(click_result.get("ok")),
                "new_family_request_count": len(frozen_requests),
                "captured_body_count": int(evidence_evaluation.get("body_count", 0) or 0),
                "new_request_state_count": int(
                    evidence_evaluation.get("new_request_state_count", 0) or 0
                ),
                "new_unique_row_count": int(
                    evidence_evaluation.get("new_unique_row_count", 0) or 0
                ),
                "new_cursor_state_count": int(
                    evidence_evaluation.get("new_cursor_state_count", 0) or 0
                ),
                "acceptance_reason": evidence_evaluation.get("reason"),
                "new_request_indices": sorted(new_indices)[:30],
                "error": str(click_result.get("error") or "")[:240],
            }
            accessibility_transactions.append(record)
            if evidence_evaluation.get("accepted"):
                success_indices = set(new_indices)
                success_requests = freeze_request_evidence(frozen_requests)
                success_evaluation = dict(evidence_evaluation)
                success_candidate = descriptor
                record["accepted"] = True
                break
            restored = False
            if selected_ref and selected_ref != current_ref:
                restored_result = _mcp().call_tool("browser_click", {"target": selected_ref})
                restored = bool(restored_result.get("ok"))
                if restored:
                    time.sleep(pause_ms / 1000)
            record["rollback"] = {"selection_restored": restored}
            if not restored and page_url:
                reload_result = _mcp().call_tool("browser_navigate", {"url": page_url})
                record["rollback"]["reloaded"] = bool(reload_result.get("ok"))
                if reload_result.get("ok"):
                    time.sleep(min(6.0, pause_ms * 2 / 1000))
                    setup_registry()

    new_requests = freeze_request_evidence(success_requests)
    final_network = {
        "ok": True,
        "requests": new_requests,
        "total": len(new_requests),
        "matched_total": len(new_requests),
        "filter": network_pattern,
        "atomic_capture": True,
        "response_bodies_attempted": len(new_requests),
        "response_bodies_succeeded": sum(
            1 for request in new_requests if request.get("response_body")
        ),
        "response_bodies_failed": sum(
            1 for request in new_requests if not request.get("response_body")
        ),
    }
    exploration = {
        "anchor_found": first_setup.get("anchor_found"),
        "anchor": first_setup.get("anchor"),
        "shadow_roots": first_setup.get("shadow_roots", 0),
        "candidate_count": first_setup.get("candidate_count", len(candidates)),
        "candidates": candidates[:20],
        "transactions": transactions,
        "transaction_count": len(transactions),
        "accessibility_transactions": accessibility_transactions,
        "accessibility_transaction_count": len(accessibility_transactions),
        "resource_delta_count": resource_delta_count,
        "accepted_candidate": success_candidate,
        "accepted": bool(success_candidate and success_evaluation.get("accepted")),
        "acceptance_reason": success_evaluation.get("reason") or (
            "verification_contract_missing"
            if not verification_contract_ready
            else "candidate_evidence_exhausted"
        ),
        "verification_contract_ready": verification_contract_ready,
    }
    return {
        "ok": True,
        "strategy": "isolated_request_family_transactions",
        "request_family": family,
        "request_family_pattern": family_pattern,
        "exact_family_matching": bool(family),
        "baseline_request_count": len(initial_indices),
        "new_request_count": len(new_requests),
        "new_request_indices": sorted(success_indices)[:50],
        "new_request_state_count": int(
            success_evaluation.get("new_request_state_count", 0) or 0
        ),
        "new_unique_row_count": int(
            success_evaluation.get("new_unique_row_count", 0) or 0
        ),
        "new_cursor_state_count": int(
            success_evaluation.get("new_cursor_state_count", 0) or 0
        ),
        "accepted": bool(success_candidate and success_evaluation.get("accepted")),
        "acceptance_reason": success_evaluation.get("reason") or (
            "verification_contract_missing"
            if not verification_contract_ready
            else "candidate_evidence_exhausted"
        ),
        "exploration": exploration,
        "network": final_network,
    }


@tool("browser_wait_dynamic")
def browser_wait_dynamic(timeout_ms: int = 5000, text: Optional[str] = None) -> Dict[str, Any]:
    """Wait for dynamic content to load."""
    if text:
        result = _mcp().call_tool("browser_wait_for", {"text": text})
    else:
        result = _mcp().call_tool("browser_wait_for", {"time": timeout_ms / 1000})
    if not result.get("ok"):
        return result
    return {"ok": True, "waited_ms": timeout_ms}


@tool("browser_reload")
def browser_reload() -> Dict[str, Any]:
    """Reload the current page."""
    url_result = browser_evaluate.invoke({"javascript": "() => window.location.href"})
    if not url_result.get("ok"):
        return url_result
    current_url = str(url_result.get("result", ""))
    if not current_url:
        return {"ok": False, "error": "无法获取当前页面 URL"}
    return _mcp().call_tool("browser_navigate", {"url": current_url})


@tool("browser_close")
def browser_close() -> Dict[str, Any]:
    """Close the browser."""
    return _mcp().call_tool("browser_close", {})


@tool("browser_back")
def browser_back() -> Dict[str, Any]:
    """Go back."""
    return _mcp().call_tool("browser_navigate_back", {})


# ===========================================================================
# Custom tools -- no direct MCP equivalent
# ===========================================================================

class DOMProbeArgs(BaseModel):
    max_candidates: int = Field(8, description="Maximum repeated DOM candidates")
    max_samples: int = Field(2, description="HTML/text samples per candidate")


@tool("browser_dom_probe", args_schema=DOMProbeArgs)
def browser_dom_probe(max_candidates: int = 8, max_samples: int = 2) -> Dict[str, Any]:
    """Return compact repeated-list and pagination candidates without full-page HTML."""
    max_candidates = max(1, min(int(max_candidates), 20))
    max_samples = max(1, min(int(max_samples), 3))
    js = r"""() => {
        const maxCandidates = __MAX_CANDIDATES__;
        const maxSamples = __MAX_SAMPLES__;
        const esc = (value) => {
            const text = String(value || '');
            if (window.CSS && CSS.escape) return CSS.escape(text);
            return text.replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
        };
        const visible = (el) => {
            if (!el || !(el instanceof Element)) return false;
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                   Number(style.opacity || 1) > 0.02 && rect.width > 1 && rect.height > 1;
        };
        const allRoots = () => {
            const roots = [document];
            const seen = new Set(roots);
            for (let i = 0; i < roots.length; i++) {
                let nodes = [];
                try { nodes = [...roots[i].querySelectorAll('*')]; } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        roots.push(node.shadowRoot);
                    }
                }
            }
            return roots;
        };
        const deepQuery = (selector) => {
            const out = [];
            for (const root of allRoots()) {
                try { out.push(...root.querySelectorAll(selector)); } catch (_) {}
            }
            return out;
        };
        const stableClasses = (el) => [...(el.classList || [])]
            .filter(name => name && name.length < 50 && !/^\d+$/.test(name))
            .slice(0, 3);
        const simpleSelector = (el) => {
            const tag = el.tagName.toLowerCase();
            if (el.id) {
                const byId = '#' + esc(el.id);
                try { if (deepQuery(byId).length === 1) return byId; } catch (_) {}
            }
            const classes = stableClasses(el);
            if (classes.length) {
                const byClass = tag + classes.map(name => '.' + esc(name)).join('');
                try { if (deepQuery(byClass).length >= 2) return byClass; } catch (_) {}
            }
            const parent = el.parentElement;
            if (parent) {
                let parentSelector = '';
                if (parent.id) parentSelector = '#' + esc(parent.id);
                else {
                    const parentClasses = stableClasses(parent);
                    if (parentClasses.length) {
                        parentSelector = parent.tagName.toLowerCase() +
                            parentClasses.map(name => '.' + esc(name)).join('');
                    }
                }
                if (parentSelector) {
                    const nested = parentSelector + ' > ' + tag +
                        classes.map(name => '.' + esc(name)).join('');
                    try { if (deepQuery(nested).length >= 2) return nested; } catch (_) {}
                }
            }
            return tag;
        };

        const selectorSet = new Set();
        const elements = deepQuery(
            'li,article,tr,section,[class*="item" i],[class*="card" i],' +
            '[class*="result" i],[class*="list" i] > *,[class*="grid" i] > *'
        );
        for (const el of [...elements].slice(0, 5000)) {
            const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!visible(el) || text.length < 20) continue;
            selectorSet.add(simpleSelector(el));
        }

        const candidates = [];
        for (const selector of selectorSet) {
            let nodes;
            try { nodes = deepQuery(selector).filter(visible); }
            catch (_) { continue; }
            if (nodes.length < 3 || nodes.length > 500) continue;
            const sampleNodes = nodes.slice(0, maxSamples);
            const sampleTexts = sampleNodes.map(el =>
                (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500));
            const sampleHtml = sampleNodes.map(el => el.outerHTML.slice(0, 1600));
            const avgTextChars = sampleTexts.length
                ? sampleTexts.reduce((sum, value) => sum + value.length, 0) / sampleTexts.length
                : 0;
            const semantic = /(item|card|result|product|article|movie|grid|list)/i.test(selector) ? 12 : 0;
            const structural = /(^|\s|>)(li|article|tr)([.#\s>]|$)/i.test(selector) ? 6 : 0;
            const score = Math.min(nodes.length, 60) * (0.5 + Math.min(avgTextChars / 180, 2)) +
                semantic + structural;
            candidates.push({
                selector,
                count: nodes.length,
                avg_text_chars: Math.round(avgTextChars),
                score: Math.round(score * 100) / 100,
                sample_texts: sampleTexts,
                sample_html: sampleHtml,
            });
        }
        candidates.sort((a, b) => b.score - a.score);

        const pagination = deepQuery('a[href],button')
            .filter(visible)
            .map(el => ({
                text: (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
                href: el.href || '',
                rel: el.getAttribute('rel') || '',
                selector: simpleSelector(el),
            }))
            .filter(item => /^(next|more|下一页|下页|加载更多|更多|›|»)|next/i.test(
                item.text + ' ' + item.rel + ' ' + item.selector
            ))
            .slice(0, 12);

        return JSON.stringify({
            page: {
                url: location.href,
                title: document.title || '',
                ready_state: document.readyState,
                body_text_chars: (document.body?.innerText || '').length,
                shadow_roots: Math.max(0, allRoots().length - 1),
            },
            repeated_candidates: candidates.slice(0, maxCandidates),
            pagination_candidates: pagination,
        });
    }"""
    js = js.replace("__MAX_CANDIDATES__", str(max_candidates))
    js = js.replace("__MAX_SAMPLES__", str(max_samples))
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    parsed = _parse_mcp_result(result.get("text", "{}"))
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return {"ok": False, "error": "DOM probe 返回内容无法解析", "raw": parsed[:1000]}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "DOM probe 返回类型无效"}
    return {"ok": True, **parsed}

class VerifySelectorItem(BaseModel):
    name: str = Field(..., description="Field name")
    selector: str = Field(..., description="CSS selector")
    attribute: str = Field("text", description="Attribute: text/innerText/href/src")


class VerifySelectorsArgs(BaseModel):
    selectors: List[VerifySelectorItem] = Field(..., description="Selectors to verify")
    max_samples: int = Field(5, description="Max samples per selector")


@tool("browser_verify_selectors", args_schema=VerifySelectorsArgs)
def browser_verify_selectors(
    selectors: List[Dict[str, Any]],
    max_samples: int = 5,
) -> Dict[str, Any]:
    """
    Batch-verify CSS selectors via browser_evaluate.
    This is the core tool from original browser_tools.py that MCP lacks.
    """
    # Ensure selectors are plain dicts (LangChain may pass Pydantic objects)
    selector_dicts = []
    for s in selectors:
        if hasattr(s, 'model_dump'):
            selector_dicts.append(s.model_dump())
        elif hasattr(s, 'dict'):
            selector_dicts.append(s.dict())
        elif isinstance(s, dict):
            selector_dicts.append(s)
        else:
            selector_dicts.append({"name": str(s), "selector": "", "attribute": "text"})
    selectors_json = json.dumps(selector_dicts, ensure_ascii=False)
    js = (
        "async () => {"
        f"const selectors={selectors_json};"
        f"const maxSamples={max_samples};"
        "const results=[];"
        "const allRoots=()=>{const roots=[document],seen=new Set(roots);"
        "for(let i=0;i<roots.length;i++){let nodes=[];try{nodes=[...roots[i].querySelectorAll('*')];}catch(_){}"
        "for(const node of nodes){if(node.shadowRoot&&!seen.has(node.shadowRoot)){seen.add(node.shadowRoot);roots.push(node.shadowRoot);}}}return roots;};"
        "const deepQuery=(selector)=>{const out=[];for(const root of allRoots()){try{out.push(...root.querySelectorAll(selector));}catch(_){}}return out;};"
        "for(const item of selectors){"
        "const{name,selector,attribute='text'}=item;"
        "if(!selector){results.push({name,selector,match_count:0,error:'empty_selector'});continue;}"
        "try{"
        "const elements=deepQuery(selector);"
        "const count=elements.length;"
        "const samples=[];let emptyCount=0;"
        "for(let i=0;i<Math.min(count,maxSamples);i++){"
        "const el=elements[i];"
        "let value;"
        "if(attribute==='text'||attribute==='innerText'){value=el.innerText;}"
        "else{value=el.getAttribute(attribute);}"
        "value=(value||'').trim();"
        "if(value){samples.push(value.substring(0,300));}else{emptyCount++;}"
        "}"
        "results.push({name,selector,attribute,match_count:count,empty_count:emptyCount,sample_values:samples});"
        "}catch(exc){results.push({name,selector,match_count:0,error:String(exc)});}"
        "}"
        "return JSON.stringify(results);}"
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    raw_text = result.get("text", "[]")
    parsed = _parse_mcp_result(raw_text)
    if isinstance(parsed, str):
        try:
            verified = json.loads(parsed)
        except json.JSONDecodeError:
            verified = []
    elif isinstance(parsed, list):
        verified = parsed
    else:
        verified = []
    return {"ok": True, "results": verified, "verified_count": len(verified)}


@tool("browser_frames")
def browser_frames(include_iframe_elements: bool = True) -> Dict[str, Any]:
    """Get page iframe list."""
    js = (
        "() => JSON.stringify("
        "[...document.querySelectorAll('iframe')].map((f,i)=>({"
        "index:i,src:f.src||'',name:f.name||'',id:f.id||'',"
        "width:f.offsetWidth,height:f.offsetHeight})))"
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    parsed = _parse_mcp_result(result.get("text", "[]"))
    if isinstance(parsed, str):
        try:
            frames = json.loads(parsed)
        except json.JSONDecodeError:
            frames = []
    else:
        frames = parsed if isinstance(parsed, list) else []
    return {"ok": True, "frames": frames, "count": len(frames)}


class UseFrameArgs(BaseModel):
    frame_index: int = Field(0, description="iframe index")


@tool("browser_use_frame", args_schema=UseFrameArgs)
def browser_use_frame(frame_index: int = 0) -> Dict[str, Any]:
    """Switch to a specific iframe."""
    js = (
        "() => {"
        "const frames=document.querySelectorAll('iframe');"
        f"if({frame_index}>=frames.length)return JSON.stringify({{error:'frame_not_found'}});"
        f"const frame=frames[{frame_index}];"
        "return JSON.stringify({switched:true,src:frame.src,name:frame.name});}"
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    return {"ok": True, "frame_index": frame_index, "detail": result.get("text", "")}


AUTH_DIR = Path(
    os.getenv(
        "BROWSER_AUTH_STATE_DIR",
        os.getenv(
            "AUTH_STATE_DIR",
            str(Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace")) / "browser_auth_states"),
        ),
    )
).expanduser()
AUTH_DIR.mkdir(parents=True, exist_ok=True)


def _safe_session_name(session_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(session_name or "default")).strip("._")
    return cleaned or "default"


def _auth_state_path(session_name: str, state_path: Optional[str] = None) -> Path:
    if state_path:
        return Path(state_path).expanduser().resolve()
    return (AUTH_DIR / f"{_safe_session_name(session_name)}.json").resolve()


def _validate_auth_state_file(path: Path) -> Dict[str, Any]:
    try:
        if not path.is_file():
            return {"ok": False, "error": f"登录态文件不存在: {path}"}
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = inspect_storage_state(payload, now_unix=time.time())
        if result.get("ok"):
            return result
        messages = {
            "invalid_storage_state_schema": "登录态文件格式无效",
            "empty_storage_state": "登录态为空，未检测到 cookie 或 localStorage",
            "storage_state_expired": "登录态中的持久 cookie 已全部过期",
        }
        return {
            **result,
            "error": messages.get(
                str(result.get("error_code") or ""), "登录态文件格式无效"
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": f"登录态文件校验失败: {exc}"}


def _save_auth_state(path: Path) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    code = (
        "async (page) => {"
        f"await page.context().storageState({{path: {json.dumps(str(temp_path))}, indexedDB: true}});"
        "return {saved: true};"
        "}"
    )
    try:
        result = _mcp().call_tool("browser_run_code_unsafe", {"code": code})
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "保存登录态失败")}
        validation = _validate_auth_state_file(temp_path)
        if not validation.get("ok"):
            return validation
        temp_path.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return {
            "ok": True,
            "state_path": str(path),
            "cookie_count": validation.get("cookie_count", 0),
            "origin_count": validation.get("origin_count", 0),
        }
    except Exception as exc:
        return {"ok": False, "error": f"保存登录态失败: {exc}"}
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _login_probe(target_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    js = r"""() => {
        const visible = (el) => {
            if (!el || !(el instanceof Element)) return false;
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' &&
                   Number(s.opacity || 1) > 0.02 && r.width > 1 && r.height > 1;
        };
        const text = (document.body?.innerText || '').slice(0, 120000);
        const lower = text.toLowerCase();
        const targetRoot = document.querySelector('main, article, [role="main"]');
        const targetContentChars = (targetRoot?.innerText || '').trim().length;
        const requestedFields = __TARGET_FIELDS__;
        const targetFieldMatches = requestedFields.filter(value => {
            const normalized = String(value || '').trim().toLowerCase();
            return normalized.length > 1 && lower.includes(normalized);
        }).slice(0, 30);
        const passwordInputs = [...document.querySelectorAll('input[type="password"]')].filter(visible).length;
        const hardGate = /(?:log\s*in|sign\s*in|登录|登陆|注册).{0,50}(?:continue|view|read|access|unlock|查看|浏览|继续|解锁)|(?:content|page|article|内容|页面|文章).{0,50}(?:login|required|登录后可见|需要登录|仅会员可见)/is.test(text);
        const authOverlays = [...document.querySelectorAll('body *')].filter(el => {
            if (!visible(el)) return false;
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            const coverage = (r.width * r.height) / Math.max(innerWidth * innerHeight, 1);
            const hint = `${el.id} ${el.className} ${el.getAttribute('role') || ''} ${(el.innerText || '').slice(0, 300)}`;
            return ['fixed','sticky'].includes(s.position) && coverage > 0.25 &&
                   /(auth|login|signin|登录|扫码|会员|sign\s*in|log\s*in)/i.test(hint);
        }).length;
        const authenticatedHints = (lower.match(/(?:log\s*out|sign\s*out|my\s+account|profile|退出登录|个人中心|我的账号|账号设置)/g) || []).length;
        const challengeHints = (lower.match(/(?:captcha|verification\s*code|two.factor|mfa|验证码|安全验证|二次验证|扫码登录)/g) || []).length;
        return JSON.stringify({
            href: location.href,
            title: document.title || '',
            body_text_chars: text.length,
            target_content_chars: targetContentChars,
            password_inputs: passwordInputs,
            hard_gate: hardGate,
            auth_overlays: authOverlays,
            authenticated_hints: authenticatedHints,
            challenge_hints: challengeHints,
            target_field_matches: targetFieldMatches
        });
    }""".replace(
        "__TARGET_FIELDS__",
        json.dumps(
            [str(value).strip()[:120] for value in (target_fields or []) if str(value).strip()][:30],
            ensure_ascii=False,
        ),
    )
    result = _mcp().call_tool("browser_evaluate", {"function": js})
    if not result.get("ok"):
        return result
    parsed = _parse_mcp_result(result.get("text", result))
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {"ok": True, **parsed}


def _auth_probe_location_allowed(probe: Dict[str, Any], target_url: str) -> bool:
    try:
        current_host = (urlparse(str(probe.get("href") or "")).hostname or "").lower()
    except Exception:
        current_host = ""
    return host_allowed(current_host, build_verification_contract(target_url))


class AuthProbeArgs(BaseModel):
    target_url: Optional[str] = Field(None, description="Optional target URL used only for factual URL comparison")
    target_fields: List[str] = Field(default_factory=list, description="Requested fields used for target-page evidence matching")


@tool("browser_auth_probe", args_schema=AuthProbeArgs)
def browser_auth_probe(
    target_url: Optional[str] = None,
    target_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return raw post-login page facts for AI review; never declares authentication success."""
    probe = _login_probe(target_fields)
    if not probe.get("ok"):
        return probe
    current_url = str(probe.get("href") or "")
    same_target = None
    if target_url:
        try:
            current = urlparse(current_url)
            target = urlparse(str(target_url))
            same_target = bool(
                current.hostname and target.hostname
                and current.hostname.lower() == target.hostname.lower()
                and (current.path or "").rstrip("/") == (target.path or "").rstrip("/")
            )
        except Exception:
            same_target = None
    return {
        **probe,
        "target_url_match": same_target,
        "auth_location_allowed": _auth_probe_location_allowed(probe, target_url) if target_url else None,
        "verification_state": "unverified",
        "decision_authority": "pi-agent-core-ai",
    }


def _headful_browser_available() -> bool:
    if str(os.getenv("BROWSER_ALLOW_HEADFUL_WITHOUT_DISPLAY", "false")).lower() in {"1", "true", "yes", "on"}:
        return True
    system = platform.system()
    if system in {"Windows", "Darwin"}:
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


class AuthStateArgs(BaseModel):
    session_name: str = Field("default", description="Saved auth session name")
    state_path: Optional[str] = Field(None, description="Explicit storage-state JSON path")


@tool("browser_save_auth_state", args_schema=AuthStateArgs)
def browser_save_auth_state(session_name: str = "default", state_path: Optional[str] = None) -> Dict[str, Any]:
    """Persist cookies and local storage from the active browser context."""
    path = _auth_state_path(session_name, state_path)
    result = _save_auth_state(path)
    if result.get("ok"):
        result["session_name"] = _safe_session_name(session_name)
    return result


@tool("browser_load_auth_state", args_schema=AuthStateArgs)
def browser_load_auth_state(session_name: str = "default", state_path: Optional[str] = None) -> Dict[str, Any]:
    """Restart the headless MCP browser with a saved auth state."""
    path = _auth_state_path(session_name, state_path)
    validation = _validate_auth_state_file(path)
    if not validation.get("ok"):
        return validation
    configured = configure_browser_mcp(str(path), headless=True, restart=True)
    if not configured.get("ok"):
        return configured
    return {
        "ok": True,
        "session_name": _safe_session_name(session_name),
        "state_path": str(path),
        "storage_state_loaded": True,
        "state_diagnostics": {
            key: validation.get(key) for key in (
                "cookie_count", "origin_count", "session_cookie_count",
                "persistent_cookie_count", "expired_cookie_count",
                "live_cookie_count", "stale_hint", "checked_at_unix",
            )
        },
    }


class ManualLoginArgs(AuthStateArgs):
    url: str = Field(..., description="URL that requires authentication")
    target_fields: List[str] = Field(default_factory=list, description="Requested fields for deterministic post-login verification")


@tool("browser_manual_login", args_schema=ManualLoginArgs)
def browser_manual_login(
    url: str,
    target_fields: Optional[List[str]] = None,
    session_name: str = "default",
    state_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a visible browser, require confirmation, save state, then restore headless mode."""
    if str(os.getenv("BROWSER_MANUAL_LOGIN_ENABLED", "true")).lower() in {"0", "false", "no", "off"}:
        return {"ok": False, "error_code": "manual_login_disabled", "error": "人工登录已被配置禁用"}
    if not _headful_browser_available():
        return {
            "ok": False,
            "error_code": "interactive_browser_unavailable",
            "error": "当前运行环境没有可用图形界面，无法打开人工登录窗口",
        }

    safe_session = _safe_session_name(session_name)
    path = _auth_state_path(safe_session, state_path)
    existing_state = str(path) if _validate_auth_state_file(path).get("ok") else None

    configured = configure_browser_mcp(existing_state, headless=False, restart=True)
    if not configured.get("ok"):
        return configured

    try:
        navigate = _mcp().call_tool("browser_navigate", {"url": url})
        if not navigate.get("ok"):
            return {
                "ok": False,
                "error_code": "login_browser_open_failed",
                "error": navigate.get("error", "无法打开登录页面"),
            }

        pre_login_probe = _login_probe(target_fields)
        if pre_login_probe.get("ok") and not _auth_probe_location_allowed(pre_login_probe, url):
            return {
                "ok": False,
                "error_code": "untrusted_auth_location",
                "error": "Authentication redirected outside the configured trust boundary.",
                "pre_login_probe": pre_login_probe,
            }
        safe_url = sanitize_url(url)
        message = f"请在已打开的浏览器中完成 {safe_url} 的登录、验证码或 MFA。"
        log_event(logger, "auth.manual_login", level="WARNING", status="required", agent="browser", action="manual_login", message=message)
        print(f"\n🔐 {message}", flush=True)

        def _persist_confirmed_login(
            probe: Optional[Dict[str, Any]] = None,
            confirmed_by_user: bool = True,
        ) -> Dict[str, Any]:
            probe = probe or {}
            if probe.get("ok") and not _auth_probe_location_allowed(probe, url):
                return {
                    "ok": False,
                    "error_code": "untrusted_auth_location",
                    "error": "Authentication completed on an untrusted host; state was not saved.",
                    "confirmation_probe": probe,
                    "pre_login_probe": pre_login_probe,
                }
            saved = _save_auth_state(path)
            if not saved.get("ok"):
                return {
                    "ok": False,
                    "error_code": "auth_state_save_failed",
                    "error": saved.get("error", "登录完成但保存登录态失败"),
                }
            headless = configure_browser_mcp(str(path), headless=True, restart=True)
            if not headless.get("ok"):
                return headless
            navigate_after = _mcp().call_tool("browser_navigate", {"url": url})
            post_login_probe = _login_probe(target_fields) if navigate_after.get("ok") else {
                "ok": False,
                "error": navigate_after.get("error", "post_login_target_open_failed"),
            }
            log_event(
                logger, "auth.manual_login", status="confirmed", agent="browser",
                action="manual_login", session=safe_session,
                confirmed_by_user=confirmed_by_user, storage_state="saved",
                verification_state="provisional",
            )
            return {
                "ok": True,
                "session_name": safe_session,
                "state_path": str(path),
                "resolved_url": probe.get("href", url),
                "challenge_detected": int(probe.get("challenge_hints", 0) or 0) > 0,
                "confirmed_by_user": confirmed_by_user,
                "verification_required": True,
                "verification_state": "provisional",
                "confirmation_probe": probe,
                "pre_login_probe": pre_login_probe,
                "post_login_probe": post_login_probe,
                "redirect_chain": list(dict.fromkeys(
                    str(value).strip() for value in (
                        pre_login_probe.get("href"),
                        probe.get("href"),
                        post_login_probe.get("href"),
                    ) if str(value or "").strip()
                )),
            }

        allow_non_tty = str(os.getenv("BROWSER_ALLOW_NON_TTY_CONFIRMATION", "false")).lower() in {
            "1", "true", "yes", "on",
        }
        stdin_ready = bool(sys.stdin and not sys.stdin.closed)
        is_tty = bool(stdin_ready and getattr(sys.stdin, "isatty", lambda: False)())
        if not stdin_ready or (not is_tty and not allow_non_tty):
            return {
                "ok": False,
                "error_code": "manual_confirmation_unavailable",
                "error": "当前运行方式没有可交互控制台，无法由用户确认登录完成",
            }
        print("登录完成后，请回到当前控制台确认；系统在确认前不会保存或继续任务。", flush=True)
        try:
            answer = input("确认已完成登录并继续？请输入 y，取消请输入 n [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return {
                "ok": False,
                "error_code": "manual_confirmation_cancelled",
                "error": "用户未确认登录完成",
            }
        if answer not in {"y", "yes", "是", "确认", "continue"}:
            return {
                "ok": False,
                "error_code": "manual_confirmation_cancelled",
                "error": "用户取消了登录确认",
            }
        try:
            confirmed_probe = _login_probe(target_fields)
            if not confirmed_probe.get("ok"):
                return {
                    "ok": False,
                    "error_code": "login_browser_closed",
                    "error": confirmed_probe.get("error", "登录窗口已关闭或浏览器连接中断"),
                }
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "login_browser_closed",
                "error": f"确认后无法读取登录窗口: {exc}",
            }
        return _persist_confirmed_login(confirmed_probe, confirmed_by_user=True)
    except Exception as exc:
        return {"ok": False, "error_code": "manual_login_failed", "error": f"人工登录失败: {exc}"}
    finally:
        # Successful login already switched to the newly saved state. On every
        # other path, close the visible browser and restore the previous state.
        if not (_MCP_CONFIG.get("headless") and _MCP_CONFIG.get("storage_state_path") == str(path)):
            configure_browser_mcp(existing_state, headless=True, restart=True)


@tool("browser_clear_auth_state", args_schema=AuthStateArgs)
def browser_clear_auth_state(session_name: str = "default", state_path: Optional[str] = None) -> Dict[str, Any]:
    """Delete one saved auth state and detach it from the active MCP context."""
    path = _auth_state_path(session_name, state_path)
    try:
        active_path = _MCP_CONFIG.get("storage_state_path")
        if active_path and Path(active_path) == path:
            configure_browser_mcp(None, headless=True, restart=True)
        existed = path.exists()
        if existed:
            path.unlink()
        metadata_deleted = AuthSessionStore(AUTH_DIR).remove(session_name)
        return {
            "ok": True,
            "session_name": _safe_session_name(session_name),
            "deleted": existed,
            "metadata_deleted": metadata_deleted,
        }
    except Exception as exc:
        return {"ok": False, "error": f"删除登录态失败: {exc}"}


@tool("browser_list_auth_states")
def browser_list_auth_states() -> Dict[str, Any]:
    """List saved auth session names without exposing cookies or tokens."""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    session_store = AuthSessionStore(AUTH_DIR)
    for path in sorted(AUTH_DIR.glob("*.json")):
        validation = _validate_auth_state_file(path)
        lifecycle = session_store.load_metadata(path.stem)
        sessions.append({
            "session_name": path.stem,
            "valid": bool(validation.get("ok")),
            "modified_at": path.stat().st_mtime,
            "lifecycle_status": lifecycle.get("status", "untracked"),
        })
    return {"ok": True, "sessions": sessions, "count": len(sessions)}
