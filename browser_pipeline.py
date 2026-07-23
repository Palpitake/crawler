"""Production Browser Agent entrypoint.

The Browser Agent is implemented exclusively with pi-agent-core in
``browser_agent_pipeline.py``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


WORKSPACE_ROOT = Path(os.getenv("AGENT_WORKSPACE", "./crawler_workspace"))
AUTH_DIR = Path(
    os.getenv(
        "BROWSER_AUTH_STATE_DIR",
        str(WORKSPACE_ROOT / "browser_auth_states"),
    )
)
_BROWSER_PIPELINE_LOCK = threading.Lock()
BROWSER_PIPELINE_BUILD = "2026.07.22-browser-pi-agent-core-native-v13.5-auth-protocol"


def run_browser_pipeline(
    target_url: str,
    target_fields: List[str],
    session_name: Optional[str] = None,
    session_confirmed: bool = False,
    rag_hits: Optional[List[Dict[str, Any]]] = None,
    pipeline_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the sole production Browser implementation through pi-agent-core."""
    from browser_agent_pipeline import run_browser_agent_pipeline

    with _BROWSER_PIPELINE_LOCK:
        return run_browser_agent_pipeline(
            target_url=target_url,
            target_fields=target_fields,
            session_name=session_name,
            session_confirmed=session_confirmed,
            rag_hits=rag_hits,
            pipeline_config=dict(pipeline_config or {}),
        )
