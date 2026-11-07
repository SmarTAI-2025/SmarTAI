import pytest
import httpx
from google.genai.errors import ClientError
from langchain_google_genai.chat_models import _handle_client_error

from backend.agents.question_preparation_agent import _SubmissionSafeProvider, provider_submission_is_uncertain
from backend.services.background_errors import classify_background_error


def google_region_error():
    error = ClientError(400, {'error': {'code': 400, 'status': 'FAILED_PRECONDITION',
                                     'message': 'User location is not supported for the API use.'}})
    try:
        _handle_client_error(error, {'model': 'synthetic-model'})
    except Exception as wrapped:
        return wrapped
    raise AssertionError('SDK must reject the request')


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
async def test_google_rejected_request_keeps_region_recovery_in_question_preparation(wrapped):
    error = google_region_error()
    if not wrapped:
        error = error.__cause__

    class Provider:
        async def ainvoke(self, messages):
            raise error

    with pytest.raises(Exception) as caught:
        await _SubmissionSafeProvider(Provider()).ainvoke([])
    assert not provider_submission_is_uncertain(caught.value)
    assert classify_background_error(caught.value, 'question_preparation_failed') == 'provider_region_unsupported'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['implicit_old_rejection', 'explicit_unknown', 'read_timeout'])
async def test_rejection_evidence_cannot_clear_unknown_or_unrelated_attempts(kind):
    error = RuntimeError('synthetic request failure')
    if kind == 'implicit_old_rejection':
        error.__context__ = google_region_error()
        error.__suppress_context__ = True
    elif kind == 'explicit_unknown':
        error.__cause__ = google_region_error()
        error.submission_may_exist = True
    else:
        error.__cause__ = httpx.ReadTimeout('synthetic timeout')

    class Provider:
        async def ainvoke(self, messages):
            raise error

    with pytest.raises(Exception) as caught:
        await _SubmissionSafeProvider(Provider()).ainvoke([])
    assert provider_submission_is_uncertain(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('suppress', [True, False])
async def test_previous_unknown_attempt_does_not_mark_current_google_rejection_uncertain(suppress):
    previous = RuntimeError('different request')
    previous.submission_may_exist = True

    class Provider:
        async def ainvoke(self, messages):
            try:
                raise previous
            except RuntimeError:
                error = ClientError(400, {'error': {'code': 400, 'status': 'FAILED_PRECONDITION',
                                                  'message': 'User location is not supported for the API use.'}})
                if suppress:
                    raise error from None
                raise error

    with pytest.raises(Exception) as caught:
        await _SubmissionSafeProvider(Provider()).ainvoke([])
    assert not provider_submission_is_uncertain(caught.value)
    assert classify_background_error(caught.value, 'question_preparation_failed') == 'provider_region_unsupported'
