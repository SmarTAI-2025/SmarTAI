"""SDK error identifiers must not mask an observed HTTP response timeout."""
import httpx
import pytest
from openai import APIStatusError

from backend.llm.providers import ProviderRequestError, _is_overload_failure
from backend.llm.request_timeouts import is_response_timeout
from backend.tools import structured_llm as llm


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 504])
async def test_sdk_timeout_code_retries_without_endpoint_overload(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://fixture.invalid"))
    error = APIStatusError("synthetic deadline", response=response,
                           body={"code": "request_timeout", "type": "timeout_error"})
    assert error.code == "request_timeout"
    assert is_response_timeout(error)
    assert not _is_overload_failure(error)
    class Provider:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            raise error
    provider = Provider()
    with pytest.raises(llm.ResponseTimeoutError):
        await llm.ainvoke_with_rate_retry(provider, [])
    assert provider.calls == 3


@pytest.mark.parametrize("code", ["provider_capacity_unavailable", "grading_wall_time_limit_reached",
                                  "provider_submit_uncertain"])
def test_application_stop_code_keeps_priority_over_transport_timeout(code):
    error = ProviderRequestError(code, status_code=504)
    error.__cause__ = httpx.ReadTimeout("synthetic old timeout")
    assert not is_response_timeout(error)
