"""Whole-request limits for synchronous work behind known browser waits.

These do not apply to durable job launch acknowledgments or polling. Expiry
cancels cooperative work; it does not imply that prior writes were rolled back.
"""
from __future__ import annotations

import asyncio
from functools import wraps
from inspect import signature

from fastapi import HTTPException

from backend.llm.request_timeouts import cancellable_provider_scope

# Browser waits include separate cleanup headroom; do not reduce processing
# time to accommodate finalizers. See docs/operations/WORKFLOW_TIMEOUTS.md.
ASK_REQUEST_SECONDS = 290
MATERIAL_PREFLIGHT_SECONDS = 950
QUESTION_ANALYSIS_SECONDS = 25
EXPERT_PROBE_SECONDS = 60


def request_deadline(seconds: float):
    def decorate(function):
        @wraps(function)
        async def bounded(*args, **kwargs):
            deadline = asyncio.timeout(seconds)
            try:
                with cancellable_provider_scope():
                    async with deadline:
                        result = await function(*args, **kwargs)
            except (Exception, asyncio.CancelledError) as exc:
                # Preserve inner errors; a cleanup error after our own expiry
                # must still be reported as the browser request deadline.
                if not deadline.expired():
                    raise
                raise HTTPException(504, detail={"code": "request_timeout"}) from exc
            if deadline.expired():
                # A swallowed cancellation must not become a successful reply.
                raise HTTPException(504, detail={"code": "request_timeout"})
            return result

        # FastAPI must resolve postponed annotations in the original module,
        # not this decorator's globals, while preserving dependency parameters.
        bounded.__signature__ = signature(function, eval_str=True)
        return bounded
    return decorate
