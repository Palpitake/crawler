from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from .config import RagConfig
from .normalizer import build_memory_query, canonicalize_fields, memory_key, normalize_url, route_hash, route_template
from .repository import MySQLRagRepository


def migrate_jsonl(path: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    config = RagConfig.from_env()
    repository = None if dry_run else MySQLRagRepository(config)
    stats = {"read": 0, "valid": 0, "written": 0, "invalid": 0}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stats["read"] += 1
        try:
            value = json.loads(line)
        except Exception:
            stats["invalid"] += 1
            continue
        url = normalize_url(str(value.get("url") or ""))
        route = route_template(url)
        domain = str(value.get("domain") or "")
        fields = canonicalize_fields(value.get("target_fields") or value.get("fields") or [])
        task_type = str(value.get("task_type") or ("comments" if any(field in {"content", "author_name", "created_at", "like_count"} for field in fields) else "generic_collection"))
        key = memory_key("legacy", domain, route, task_type, value.get("data_source"), ",".join(sorted(fields)))
        row = {
            "memory_key_hex": key.hex(), "memory_type": "strategy", "status": "stale",
            "source_kind": "historical", "domain": domain, "route_template": route,
            "route_hash_hex": route_hash(route).hex(), "task_type": task_type,
            "entity_type": "record", "collection_type": "records",
            "data_source": str(value.get("data_source") or "unknown"),
            "summary": f"Migrated legacy strategy for {domain} {task_type}",
            "searchable_text": " ".join([domain, route, task_type, " ".join(fields)]),
            "facts": {
                "target_url": url, "canonical_fields": fields,
                "selectors": value.get("selectors") or {},
                "endpoint_hints": value.get("api_endpoints") or [],
                "pagination_facts": value.get("pagination") or {},
                "interaction_plan": value.get("interaction_plan") or [],
                "quality": "historical_unverified",
            },
            "metrics": {"legacy_success_count": value.get("success_count") or 0},
            "reliability_score": min(float(value.get("confidence") or 0.5), 0.6),
            "confidence_score": min(float(value.get("confidence") or 0.5), 0.6),
            "successful_runs": 1 if value.get("success") else 0,
            "failed_runs": 0, "complete_runs": 0,
            "partial_runs": 1 if value.get("success") else 0,
            "last_verified_at": None, "last_failed_at": None,
            "fresh_until": None, "schema_version": config.schema_version,
            "agent_build": "legacy-jsonl-migration",
        }
        stats["valid"] += 1
        if not dry_run and repository is not None:
            repository.commit_state({"memories": [row], "endpoints": [], "failure": None, "execution": None, "usage": []})
            stats["written"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy crawler_rag.jsonl into MySQL")
    parser.add_argument("path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate_jsonl(args.path, dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
