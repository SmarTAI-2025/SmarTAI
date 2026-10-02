import json

import fitz
import pytest

from tools.ocr_benchmark.evaluate_pdf_retrieval import evaluate


@pytest.mark.asyncio
async def test_native_pdf_diagnostic_preserves_pages_and_does_not_export_text(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Diagnostic must not call a provider')

    monkeypatch.setattr('backend.llm.providers.BaseProvider.ainvoke', forbidden)
    monkeypatch.setattr('backend.llm.providers.BaseProvider.ainvoke_vision', forbidden)
    document = fitz.open()
    document.new_page().insert_text((72, 72), 'Exercise 1.1.20 confidential-source-fixture')
    document.new_page().insert_text((72, 72), 'An unrelated page.')
    report = await evaluate(document.tobytes(), [
        dict(id='found', query='Exercise 1.1.20', expected_pages=[1]),
        dict(id='missing', query='Exercise 9.9.99', expected_pages=[]),
    ])
    assert report['source_pages'] == 2
    assert report['provider_calls'] == 0
    assert all(case['all_expected_in_top5'] for case in report['cases'])
    assert 'confidential-source-fixture' not in json.dumps(report)
    assert 'Exercise 1.1.20' not in json.dumps(report)


@pytest.mark.asyncio
async def test_native_pdf_diagnostic_rejects_invalid_expected_pages():
    document = fitz.open()
    document.new_page().insert_text((72, 72), 'Test page.')
    with pytest.raises(ValueError, match='Invalid query'):
        await evaluate(document.tobytes(), [dict(id='bad', query='test', expected_pages=[2])])
