"""
Knowledge retrieval tool (RAG over external knowledge base).

Per docs §4.2.2, the original design called for PGVector for storage. The
current task-scoped MVP uses an in-memory implementation in
`backend/rag/store.InMemoryTaskRetriever`, registered at app startup
(`backend/main.py`) — see ARCHITECTURE_AND_DEPLOYMENT.md for the full design.

Skills call `retrieve(query, k=5, scope=task_id)` and get back a list of
relevant chunks. If no KB has been uploaded for that task (or no retriever
is configured), returns an empty list gracefully so skills still grade
using only the LLM's own knowledge.

Note on the `scope` parameter:
  - Pre-RAG callers (legacy / tests) can omit it; they get [].
  - The active grading pipeline always threads `task_id` from the API entry
    point (`backend/api/tasks.py::_run_grade`) down through
    `grade_batch → grade_student → multi_expert → GradingSkill.task_id` and
    skills pass it as `scope=self.task_id` at retrieve time.
"""
from __future__ import annotations

import logging
from typing import List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeChunk:
    """A retrieved piece of reference material."""
    content: str
    source: str
    score: float  # relevance score 0-1


class KnowledgeRetriever:
    """
    Base class. Real implementation in backend/rag/store.py subclasses this.

    NoOpRetriever (default) returns [] — skills handle this gracefully.
    """

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        scope: Optional[str] = None,
    ) -> List[KnowledgeChunk]:
        return []


class NoOpRetriever(KnowledgeRetriever):
    """Default when no knowledge base is configured."""

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        scope: Optional[str] = None,
    ) -> List[KnowledgeChunk]:
        logger.debug(
            f"NoOpRetriever.retrieve({query[:50]!r}, k={k}, scope={scope!r}) "
            f"— no KB configured"
        )
        return []


# Module-level singleton (swap via set_retriever when RAG is wired up)
_retriever: KnowledgeRetriever = NoOpRetriever()


def get_retriever() -> KnowledgeRetriever:
    return _retriever


def set_retriever(retriever: KnowledgeRetriever) -> None:
    """Called once at app startup from backend/main.py if KB is configured."""
    global _retriever
    _retriever = retriever
    logger.info(f"Knowledge retriever set to {type(retriever).__name__}")


async def retrieve(
    query: str,
    k: int = 5,
    *,
    scope: Optional[str] = None,
) -> List[KnowledgeChunk]:
    """Convenience function that delegates to the active retriever."""
    return await _retriever.retrieve(query, k, scope=scope)
