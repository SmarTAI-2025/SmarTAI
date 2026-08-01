"""Restricted provider tool for History natural-language query interpretation."""
from __future__ import annotations

from typing import Type, TypeVar

from pydantic import BaseModel

from backend.llm.providers import BaseProvider
from backend.progress.tracker import ProgressReporter
from backend.tools.structured_llm import structured_llm_call


T = TypeVar("T", bound=BaseModel)


async def call_history_query_provider(
    *,
    provider: BaseProvider,
    system_prompt: str,
    user_prompt: str,
    output_model: Type[T],
    reporter: ProgressReporter,
) -> T:
    """Call a provider through the shared structured-output/retry boundary.

    The agent owns candidate whitelisting and result sanitisation; this tool is
    deliberately limited to one structured call and emits its own observable
    substep events.
    """

    await reporter._emit_message(
        f"HistoryQueryTool: calling {provider.provider_id}", "info",
    )
    parsed, _raw = await structured_llm_call(
        provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_model=output_model,
    )
    await reporter._emit_message("HistoryQueryTool: structured result received", "info")
    return parsed
