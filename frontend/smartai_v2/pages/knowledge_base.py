"""Knowledge base — /knowledge-base (P2 stub, awaiting backend RAG)"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import empty_state
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout


@rx.page(route="/knowledge-base", title="Knowledge Base | SmarTAI")
def knowledge_base_page() -> rx.Component:
    return require_auth(
        with_layout(
            "Knowledge Base",
            section_header(
                "Subject knowledge bases",
                "Upload textbooks and references to ground LLM grading in your course material.",
            ),
            empty_state(
                "construction",
                "Coming with RAG (P2)",
                "Backend backend/rag/ is empty; this UI activates once PGVector + ingestion are implemented (see plan §5.2).",
            ),
        ),
        require_role="teacher",
    )
