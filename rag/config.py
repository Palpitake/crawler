from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RagConfig:
    backend: str = "mysql"
    fail_open: bool = True
    dual_write_jsonl: bool = False
    jsonl_path: str = ""

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "crawler_rag"
    mysql_user: str = "crawler_rag_app"
    mysql_password: str = ""
    mysql_charset: str = "utf8mb4"
    mysql_connect_timeout: int = 5
    mysql_read_timeout: int = 10
    mysql_write_timeout: int = 10
    mysql_pool_size: int = 6
    mysql_pool_recycle_seconds: int = 1800

    enable_fulltext: bool = True
    top_k_site: int = 5
    top_k_strategy: int = 8
    top_k_failure: int = 5
    candidate_limit: int = 80
    schema_version: int = 1

    @classmethod
    def from_env(cls, workspace: str | None = None) -> "RagConfig":
        root = Path(workspace or os.getenv("AGENT_WORKSPACE", "./crawler_workspace"))
        return cls(
            backend=os.getenv("RAG_BACKEND", "mysql").strip().lower() or "mysql",
            fail_open=_env_bool("RAG_FAIL_OPEN", True),
            dual_write_jsonl=_env_bool("RAG_DUAL_WRITE_JSONL", False),
            jsonl_path=os.getenv("RAG_JSONL_PATH", str(root / "runtime" / "rag" / "crawler_rag.jsonl")),
            mysql_host=os.getenv("RAG_MYSQL_HOST", "127.0.0.1"),
            mysql_port=_env_int("RAG_MYSQL_PORT", 3306),
            mysql_database=os.getenv("RAG_MYSQL_DATABASE", "crawler_rag"),
            mysql_user=os.getenv("RAG_MYSQL_USER", "crawler_rag_app"),
            mysql_password=os.getenv("RAG_MYSQL_PASSWORD", ""),
            mysql_charset=os.getenv("RAG_MYSQL_CHARSET", "utf8mb4"),
            mysql_connect_timeout=_env_int("RAG_MYSQL_CONNECT_TIMEOUT_SECONDS", 5),
            mysql_read_timeout=_env_int("RAG_MYSQL_READ_TIMEOUT_SECONDS", 10),
            mysql_write_timeout=_env_int("RAG_MYSQL_WRITE_TIMEOUT_SECONDS", 10),
            mysql_pool_size=max(1, _env_int("RAG_MYSQL_POOL_SIZE", 6)),
            mysql_pool_recycle_seconds=max(60, _env_int("RAG_MYSQL_POOL_RECYCLE_SECONDS", 1800)),
            enable_fulltext=_env_bool("RAG_ENABLE_FULLTEXT", True),
            top_k_site=max(1, _env_int("RAG_TOP_K_SITE", 5)),
            top_k_strategy=max(1, _env_int("RAG_TOP_K_STRATEGY", 8)),
            top_k_failure=max(1, _env_int("RAG_TOP_K_FAILURE", 5)),
            candidate_limit=max(10, _env_int("RAG_CANDIDATE_LIMIT", 80)),
            schema_version=max(1, _env_int("RAG_SCHEMA_VERSION", 1)),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default
