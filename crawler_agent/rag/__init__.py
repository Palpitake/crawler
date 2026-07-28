"""Structured crawler memory backed by MySQL.

The public surface is intentionally small so Supervisor, Browser and Code do
not depend on storage details.  MySQL failures are fail-open by default: RAG is
an optimisation and must never make a crawl unavailable.
"""
from .service import RagService, get_rag_service

__all__ = ["RagService", "get_rag_service"]
