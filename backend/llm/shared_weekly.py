"""Official Responses transport for weekly billing; no daily fallback.

Uses the existing configured client's transport, scheduler and response deadline.
Only the three managed OpenAI presets enter here. BYOK and Demo keep their paths.
"""
import asyncio
import time

from backend.config import settings
from backend.llm.providers import (
    LLMResponse, _request_definitely_rejected,
    _wait_provider_local, _is_overload_failure, _safe_finish_reason,
)
from backend.llm.request_timeouts import current_response_scope
from backend.services.model_quota import ModelQuotaError
from backend.services.shared_weekly_budget import PRICES, reserve, finish


def response_input(messages):
    """Translate the existing text/vision messages once for count and generation."""
    items = []
    roles = {"system": "system", "human": "user", "ai": "assistant"}
    for message in messages:
        role = roles.get(message.type)
        if role is None:
            raise ModelQuotaError("shared_pool_billing_unavailable")
        if isinstance(message.content, str):
            content = message.content
        elif isinstance(message.content, list):
            content = []
            for block in message.content:
                if not isinstance(block, dict):
                    raise ModelQuotaError("shared_pool_billing_unavailable")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    content.append({"type": "input_text", "text": block["text"]})
                elif block.get("type") == "image_url" and role == "user":
                    image = block.get("image_url")
                    if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                        raise ModelQuotaError("shared_pool_billing_unavailable")
                    content.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
                else:
                    raise ModelQuotaError("shared_pool_billing_unavailable")
        else:
            raise ModelQuotaError("shared_pool_billing_unavailable")
        items.append({"role": role, "content": content})
    return items


async def _reserve_when_available(owner, model, inputs, maximum):
    deadline = time.monotonic() + settings.llm_timeout
    while True:
        try:
            return reserve(owner, model, inputs, max_output_tokens=maximum)
        except ModelQuotaError as exc:
            if str(exc) != "shared_pool_budget_pending" or time.monotonic() >= deadline:
                raise
            # Other in-flight responses release unused reservations. A pending
            # hold must not be presented as an already-spent weekly allowance.
            await asyncio.sleep(0.1)


async def invoke_weekly(provider, owner, messages, *, max_output_tokens=None):
    if (provider.provider_type != "openai" or provider.model not in PRICES or
            (provider.config.base_url or "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1"):
        raise ModelQuotaError("shared_pool_billing_unavailable")
    maximum = 128000 if max_output_tokens is None else max_output_tokens
    payload = {"model": provider.model, "input": response_input(messages)}
    chat = await _wait_provider_local(provider._get_client())
    scope = current_response_scope()
    client = chat.root_async_client.with_options(max_retries=0, timeout=scope.seconds if scope else settings.llm_timeout)
    await provider._endpoint_breaker().before_call()
    async with provider._call_capacity(messages):
        count = await _wait_provider_local(client.responses.input_tokens.count(**payload))
        charge_id, maximum = await _wait_provider_local(_reserve_when_available(owner, provider.model, count.input_tokens, maximum))
        submitted = False
        response_id = None
        started = time.perf_counter()
        try:
            async def call():
                nonlocal submitted, started
                started = time.perf_counter()
                submitted = True
                return await client.responses.create(**payload, max_output_tokens=maximum,
                    service_tier="default", store=False, prompt_cache_options={"ttl": "30m"})
            response = await provider._invoke_response(call)
            response_id = response.id
            # Price identity must be explicit. No inference from a requested
            # model if the upstream actually served something else.
            if response.model != provider.model or response.service_tier != "default":
                raise ModelQuotaError("shared_pool_usage_unavailable")
            usage = response.usage.model_dump() if response.usage is not None else None
            finish(owner, charge_id, usage=usage, response_id=response.id)
            if usage is None:
                raise ModelQuotaError("shared_pool_usage_unavailable")
            duration = (time.perf_counter() - started) * 1000
            provider._endpoint_breaker().record_success()
            provider._record_duration(duration)
            status = response.status
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            refused = any(getattr(part, "type", None) == "refusal"
                for item in response.output for part in (getattr(item, "content", None) or []))
            return LLMResponse(content=response.output_text, provider=provider.provider_id,
                model=response.model, duration_ms=duration, input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                finish_reason="refused" if refused else "stop" if status == "completed" else _safe_finish_reason(reason or status),
                refusal_code="provider_content_rejected" if refused else None)
        except BaseException as exc:
            finish(owner, charge_id, response_id=response_id,
                   rejected=not submitted or _request_definitely_rejected(exc))
            if _is_overload_failure(exc):
                provider._endpoint_breaker().record_failure()
            raise
