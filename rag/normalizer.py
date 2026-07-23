from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import MemoryQuery

SENSITIVE_OR_TRACKING_KEYS = {
    "token", "access_token", "auth", "authorization", "key", "apikey", "api_key",
    "secret", "signature", "sign", "xsec_token", "pcdk", "spmtag", "spm",
    "session", "sid", "ticket", "code", "timestamp", "ts", "nonce", "traceid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
}

FIELD_ALIASES = {
    "content": {"评论内容", "评价内容", "内容", "正文", "content", "message", "text", "comment"},
    "author_name": {"评论用户", "用户昵称", "作者", "发布者", "nickname", "uname", "author", "user_name", "username"},
    "created_at": {"评论时间", "评价时间", "发布时间", "时间", "creationtime", "ctime", "created_at", "publish_time"},
    "like_count": {"点赞数", "点赞", "有用数", "usefulvotecount", "like", "likes", "like_count"},
    "reply_count": {"回复数", "子回复数", "reply_count", "replies"},
    "rating": {"评分", "星级", "score", "rating"},
    "title": {"标题", "title", "name"},
    "url": {"链接", "地址", "url", "link"},
}

_ALIAS_TO_CANONICAL = {
    alias.lower().strip(): canonical
    for canonical, aliases in FIELD_ALIASES.items()
    for alias in aliases
}


def canonicalize_fields(fields: Iterable[Any], extra_aliases: Optional[Dict[str, str]] = None) -> List[str]:
    aliases = dict(_ALIAS_TO_CANONICAL)
    for alias, canonical in (extra_aliases or {}).items():
        aliases[str(alias).lower().strip()] = str(canonical).strip()
    result: List[str] = []
    for raw in fields or []:
        text = str(raw or "").strip()
        if not text:
            continue
        canonical = aliases.get(text.lower(), _slug(text))
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def normalize_url(url: str, *, keep_semantic_query: bool = False) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
        if not parsed.scheme or not parsed.netloc:
            return str(url or "")
        pairs: List[Tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            sensitive = (
                lowered in SENSITIVE_OR_TRACKING_KEYS
                or any(token in lowered for token in ("token", "secret", "signature", "session", "ticket", "nonce", "trace"))
                or len(value) > 64
            )
            if sensitive:
                continue
            if keep_semantic_query and value:
                pairs.append((key, value))
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(pairs, doseq=True), ""))
    except Exception:
        return str(url or "")


def route_template(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path or "/"
    except Exception:
        path = "/"
    segments = path.split("/")
    normalized: List[str] = []
    for segment in segments:
        if not segment:
            normalized.append("")
            continue
        stem, suffix = _split_suffix(segment)
        if _looks_like_identifier(stem):
            stem = "{id}"
        normalized.append(stem + suffix)
    value = "/".join(normalized) or "/"
    return re.sub(r"/{2,}", "/", value)


def route_hash(value: str) -> bytes:
    return hashlib.sha256(str(value or "").encode("utf-8")).digest()


def infer_task_type(request: str, fields: Iterable[Any], target_url: str = "") -> Tuple[str, str, str]:
    text = " ".join([str(request or ""), " ".join(str(v) for v in (fields or [])), str(target_url or "")]).lower()
    if re.search(r"评论|评价|回复|comment|review|reply", text):
        entity = "product" if re.search(r"商品|product|item\.", text) else "content"
        return ("product_reviews" if entity == "product" else "comments", entity, "reviews")
    if re.search(r"视频|video", text):
        return "videos", "video", "videos"
    if re.search(r"新闻|news|article", text):
        return "articles", "article", "articles"
    if re.search(r"商品|product|sku", text):
        return "products", "product", "products"
    return "generic_collection", "record", "records"


def infer_scope(request: str, max_items: Optional[int]) -> str:
    if max_items:
        return "first_n"
    if re.search(r"全部|所有|全量|all\b", str(request or ""), re.I):
        return "all"
    return "unspecified"


def normalize_endpoint_template(url: str) -> Tuple[str, str]:
    clean = normalize_url(url, keep_semantic_query=True)
    parsed = urlparse(clean)
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if re.fullmatch(r"\d{5,}", value or "") or _looks_like_identifier(value):
            value = "{value}"
        pairs.append((key, value))
    template = urlunparse((parsed.scheme, parsed.netloc, route_template(clean), "", urlencode(pairs, doseq=True), ""))
    family_parts = [parsed.netloc.lower(), route_template(clean)]
    function_id = next((value for key, value in pairs if key.lower() in {"functionid", "function_id", "action", "method"}), "")
    if function_id:
        family_parts.append(function_id)
    return template, "|".join(family_parts)


def build_memory_query(state: Dict[str, Any], extra_aliases: Optional[Dict[str, str]] = None) -> MemoryQuery:
    target_url = str(state.get("target_url") or "")
    clean_url = normalize_url(target_url)
    parsed = urlparse(clean_url)
    route = route_template(clean_url)
    fields = canonicalize_fields(state.get("target_fields") or [], extra_aliases)
    request = str(state.get("user_request") or "")
    task_type, entity_type, collection_type = infer_task_type(request, fields, clean_url)
    auth = state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {}
    root_error = str(
        (state.get("error_info") or {}).get("root_error_type")
        if isinstance(state.get("error_info"), dict) else ""
    )
    query_text = " ".join(filter(None, [task_type, collection_type, " ".join(fields), request[:500], parsed.netloc]))
    return MemoryQuery(
        target_url=clean_url,
        domain=(parsed.hostname or parsed.netloc or "").lower(),
        route_template=route,
        route_hash=route_hash(route),
        task_type=task_type,
        entity_type=entity_type,
        collection_type=collection_type,
        canonical_fields=fields,
        scope_type=infer_scope(request, state.get("max_items")),
        max_items=_safe_int(state.get("max_items")),
        preferred_source=str(state.get("data_source") or "unknown"),
        authentication_state=str(auth.get("state") or state.get("auth_status") or "unknown"),
        verification_state=str(auth.get("verification_state") or "unverified"),
        current_root_error=root_error,
        query_text=query_text,
        environment_fingerprint=environment_fingerprint(state),
    )


def environment_fingerprint(state: Dict[str, Any]) -> bytes:
    browser = state.get("browser_pipeline_info") if isinstance(state.get("browser_pipeline_info"), dict) else {}
    auth = state.get("auth_facts") if isinstance(state.get("auth_facts"), dict) else {}
    payload = {
        "browser": browser.get("browser") or browser.get("runtime") or "chromium",
        "authentication_state": auth.get("state") or "unknown",
        "auth_epoch": auth.get("auth_epoch") or 0,
        "session_name": state.get("session_name") or "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).digest()


def memory_key(*parts: Any) -> bytes:
    normalized = "\x1f".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).digest()


def _split_suffix(segment: str) -> Tuple[str, str]:
    match = re.match(r"^(.*?)(\.[a-zA-Z0-9]{1,8})$", segment)
    return (match.group(1), match.group(2)) if match else (segment, "")


def _looks_like_identifier(value: str) -> bool:
    text = str(value or "")
    return bool(
        re.fullmatch(r"\d{5,}", text)
        or re.fullmatch(r"[0-9a-fA-F]{12,}", text)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,}", text)
        or re.fullmatch(r"BV[A-Za-z0-9]{8,}", text)
        or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}", text)
    )


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return text or hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:12]


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in {None, ""} else None
    except Exception:
        return None
