"""Local safeguards for response-only OCR, independent of model response time."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)
LOCAL_STEP_SECONDS = 10


class _BoundedProgress:
    def __init__(self, reporter):
        self.reporter = reporter

    def __getattr__(self, name):
        member = getattr(self.reporter, name)
        if name not in {"set_current_step", "increment_stage_metrics", "record_provider_failure"}:
            return member

        async def report(*args, **kwargs):
            try:
                async with asyncio.timeout(LOCAL_STEP_SECONDS):
                    return await member(*args, **kwargs)
            except TimeoutError:
                # Telemetry is best effort. A stuck progress sink cannot turn a
                # completed provider response into an unknown model submission.
                logger.warning("Recognition progress update timed out")
        return report


def bounded_recognition_progress(reporter):
    if reporter is None or isinstance(reporter, _BoundedProgress):
        return reporter
    return _BoundedProgress(reporter)
