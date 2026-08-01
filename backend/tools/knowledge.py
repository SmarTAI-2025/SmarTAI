"""
Knowledge retrieval tool (RAG over external knowledge base).

The application registers a combined persistent and in-memory retriever at
startup. Persistent documents are selected per assignment; the in-memory
retriever remains available to the unchanged grading algorithm.

Skills call `retrieve(query, k=5, scope=task_id)` and get back a list of
relevant chunks. The adapter passes the assignment ID through the algorithm's
existing `task_id` parameter. If no KB has been selected for that assignment
(or no retriever is configured), returns an empty list gracefully so skills
still grade using only the LLM's own knowledge.

Note on the `scope` parameter:
  - Callers without assignment context can omit it; they get [].
  - The active grading pipeline threads the assignment ID from the normalized
    grading adapter down through the existing `task_id` parameter:
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
