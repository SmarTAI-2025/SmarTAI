"""Skill boundary for the optional History query model enhancement."""
from __future__ import annotations

from typing import Type, TypeVar

from pydantic import BaseModel

from backend.llm.providers import BaseProvider
from backend.progress.tracker import ProgressReporter
from backend.tools.history_query import call_history_query_provider


T = TypeVar("T", bound=BaseModel)


class HistoryQuerySkill:
    """Translate unresolved History text into a restricted structured result.

    Provider injection follows the project's Agent -> Skill -> Tool convention;
    the skill never reaches into the global expert registry.
    """

    def __init__(self, provider: BaseProvider):
        self.provider = provider

    async def interpret(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: Type[T],
        reporter: ProgressReporter,
    ) -> T:
        await reporter._emit_message("HistoryQuerySkill: interpreting unresolved text")
        return await call_history_query_provider(
            provider=self.provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=output_model,
            reporter=reporter,
        )
