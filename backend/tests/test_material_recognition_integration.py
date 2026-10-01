import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile

from backend.api import task_preparation as api
from backend.db import assignment_repository, workflow_repository
from backend.domain.errors import RecognitionError
from backend.services import question_sources
from backend.services.stage_provider_routing import StageProviderRoute
from backend.storage.local import LocalStorage
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
from backend.tests.test_recognition_reader import llm
from backend.tests.test_recognition_v2_artifacts import setup
from backend.tests.test_task_background_workflows import _seed_task


@pytest.mark.asyncio
@pytest.mark.parametrize("role,purpose", [("rubric", "rubric"), ("programming_tests", "test_cases"), ("reference_answer", "reference")])
async def test_material_image_shares_harness_but_keeps_purpose_and_opt_in(tmp_path, monkeypatch, role, purpose):
    _agent, request, data, results = setup(tmp_path)
    provider, _ = llm(text="Literal -2, including the stated source error.")
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)],
                               pick_vision=lambda preferred=None: provider)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    kwargs = dict(owner_id=request.source.owner_id, task_id=request.source.business_id, content=data,
                  filename="source.png", route=route, registry=registry, stored_file_id=request.source.stored_file_id,
                  purpose=purpose)
    if role != "reference_answer":
        with pytest.raises(RecognitionError, match="material_ocr_confirmation_required"):
            await question_sources.read_question_source(**kwargs, allow_vision=False)
        provider.ainvoke_vision.assert_not_awaited()
    result = await question_sources.read_question_source(**kwargs, allow_vision=True)
    assert "-2" in result.text and result.recognition["requires_review"]
    assert ("expected output" if purpose == "test_cases" else purpose) in provider.ainvoke_vision.call_args.args[0]
    assert api._source_role_ocr_purpose(role) == purpose


@pytest.mark.asyncio
async def test_material_preflight_freezes_parser_and_never_applies_or_runs(tmp_path, monkeypatch):
    owner, task = _seed_task(with_question=True)
    storage = LocalStorage(tmp_path)
    monkeypatch.setattr(api, "get_storage", lambda: storage)
    monkeypatch.setattr(question_sources, "get_storage", lambda: storage)
    registry = _RecoveryRegistry()
    parser = AsyncMock(side_effect=AssertionError("preflight cannot parse or execute"))
    monkeypatch.setattr(api, "parse_material_import_to_candidates", parser)
    response = await api.preflight_material_import(
        task_id=task, file=UploadFile(file=io.BytesIO(b"1. Original teacher criterion: 20%"), filename="rubric.txt"),
        library_material_id=None, targets='["criterion"]', structure_mode="organized", extraction_hint="",
        save_to_library=False, current=SimpleNamespace(id=owner), registry=registry,
    )
    assert response["status"] == "ready" and response["recognition"] is None
    source = workflow_repository.get_operation(response["source_token"], owner_id=owner)
    assert source.payload["parser_provider_id"] == "test-provider"
    route = StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider)
    api._validate_material_parser_route(source.payload, route, registry, owner)
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        api._validate_material_parser_route(source.payload, route, _RecoveryRegistry("changed"), owner)
    parser.assert_not_awaited()
    assert assignment_repository.list_questions(task, teacher_id=owner)[0].criterion == ""
    job, _ = workflow_repository.create_operation(assignment_id=task, owner_id=owner, operation_type="material_import",
                                                  input_hash=source.input_hash, payload=source.payload)
    preview = api.material_import_source_content(task, job.id, current=SimpleNamespace(id=owner))
    assert b"Original teacher criterion" in preview.body and preview.headers["cache-control"] == "private, no-store"
    other_owner, other_task = _seed_task()
    assert api.material_import_source_content(other_task, job.id, current=SimpleNamespace(id=other_owner)).status_code == 404


@pytest.mark.asyncio
async def test_compatibility_auxiliary_upload_only_starts_review_plan(monkeypatch):
    preflight = AsyncMock(return_value={"source_token": "source"})
    start = AsyncMock(return_value={"status": "started", "job_id": "candidate-plan"})
    monkeypatch.setattr(api, "preflight_material_import", preflight)
    monkeypatch.setattr(api, "start_material_import", start)
    mutate = Mock(side_effect=AssertionError("no direct mutation"))
    monkeypatch.setattr(api.task_facade, "update_problem", mutate)
    result = await api._apply_auxiliary_upload(task_id="task", file=None, current=SimpleNamespace(id="owner"),
                                              registry=None, target="test_cases")
    assert result["job_id"] == "candidate-plan"
    assert preflight.call_args.kwargs["enable_material_ocr"] is False
    assert json.loads(preflight.call_args.kwargs["targets"]) == ["test_cases"]
    mutate.assert_not_called()


@pytest.mark.parametrize("confidence,match,recognition", [
    (0.4, "exact", None), (0.9, "possible", None),
    (0.9, "exact", {"confidence": "low", "coverage": {}}),
])
def test_unsafe_test_candidates_never_mutate_or_consume_plan(monkeypatch, confidence, match, recognition):
    candidate = dict(candidate_id="c", q_id="q1", target="test_cases", test_cases=[dict(input="1", expected_output="2")],
                     confidence=confidence, match_status=match)
    job = SimpleNamespace(id="job", assignment_id="task", operation_type="material_import", status="ready", attempt=1,
                          expires_at=None, payload=dict(candidates=[candidate], recognition=recognition))
    monkeypatch.setattr(api.workflow_repository, "get_operation", lambda *a, **k: job)
    monkeypatch.setattr(api.task_facade, "get_task", lambda **k: dict(problem_data={"q1": {"type": "programming"}}))
    monkeypatch.setattr(api, "is_programming_question_type", lambda value: True)
    mutate = Mock(side_effect=AssertionError("unsafe candidates cannot apply"))
    fail = Mock(side_effect=AssertionError("review rejection must retain the plan"))
    monkeypatch.setattr(api.task_facade, "apply_question_patches_atomic", mutate)
    monkeypatch.setattr(api.task_facade, "_fail_operation", fail)
    response = api.apply_material_import("task", "job", api.ApplyMaterialImportRequest(
        accepted_candidate_ids=["c"], expected_workflow_revision=0), current=SimpleNamespace(id="owner"))
    assert json.loads(response.body)["error"]["code"] == "material_recognition_review_required"
    mutate.assert_not_called()
    fail.assert_not_called()


def test_mixed_materials_use_strictest_role_and_opt_in_is_hashed():
    assert api._material_import_source_role(["criterion", "reference_answer"]) == "rubric"
    assert api._material_import_source_role(["criterion", "test_cases"]) == "programming_tests"
    assert api._source_fingerprint({"enable_material_ocr": True}) != api._source_fingerprint({"enable_material_ocr": False})
