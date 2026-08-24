from __future__ import annotations

import io
from types import SimpleNamespace

import pymupdf as fitz
import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from backend.agents.ingest_agent import SubmissionSourceParseResult
from backend.api import task_preparation, tasks
from backend.api.tasks import UpdateGradingSetupRequest
from backend.config import settings
from backend.db import (
    assignment_repository,
    grading_repository,
    source_outcome_repository,
    workflow_repository,
)
from backend.db.ocr_provider_repository import (
    upsert_baidu_unlimited_ocr_credential,
)
from backend.domain.errors import ValidationError
from backend.models import QuestionScorePolicy, TaskGradingSetup
from backend.services import task_facade
from backend.services.stage_provider_routing import (
    assert_grading_routes_supported,
    baidu_ocr_route_id,
    list_stage_provider_options,
    resolve_stage_provider_route,
)
from backend.services.workflow_worker import LeasedOperation
from backend.skills.ocr_ingest import OCRResult
from backend.tests.test_task_background_workflows import (
    _BackgroundTasks,
    _seed_task,
)
from backend.tests.test_workflow_facade_integrity import (
    _seed_figma_grading_task,
)
from backend.tools.file_processing import inspect_baidu_ocr_upload


_PIXMAP_1X1 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1, 1), False)
_PIXMAP_1X1.clear_with(255)
PNG_1X1 = _PIXMAP_1X1.tobytes("png")
_PIXMAP_1X1 = None


class _StageRegistry:
    def __init__(self) -> None:
        self.providers = {
            "llm-question": SimpleNamespace(
                provider_id="llm-question",
                supports_vision=False,
            ),
            "llm-grading": SimpleNamespace(
                provider_id="llm-grading",
                supports_vision=False,
            ),
        }

    def pick_default(self):
        return self.providers["llm-question"]

    def pick_default_id(self):
        return "llm-question"

    def pick_vision(self, preferred=None):
        del preferred
        return None

    def get(self, provider_id):
        return self.providers.get(provider_id)

    def uses_shared_pool(self):
        return True

    def list_configs(self):
        return [
            {
                "provider_id": provider_id,
                "provider_type": "openai",
                "model": provider_id,
                "enabled": True,
                "is_default": provider_id == "llm-question",
                "is_shared": False,
                "scope": "owner",
                "max_concurrent": 1,
                "rpm": 0,
            }
            for provider_id in self.providers
        ]


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeDocumentOCRSkill:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.client = _FakeClient()
        self.calls: list[tuple[bytes, str, str]] = []

    async def recognize_document(
        self,
        file_data: bytes,
        file_name: str,
        purpose: str,
    ) -> OCRResult:
        self.calls.append((file_data, file_name, purpose))
        return OCRResult(
            text=self.markdown,
            provider="baidu_unlimited_ocr",
        )


def _store_ocr_route(owner_id: str) -> tuple[str, str]:
    metadata = upsert_baidu_unlimited_ocr_credential(
        owner_id,
        api_key="fake-baidu-ak",
        secret_key="fake-baidu-sk",
        master_key=settings.provider_encryption_key,
    )
    return metadata.id, baidu_ocr_route_id(metadata.id)


def test_stage_options_keep_baidu_owner_scoped_and_outside_llm_registry():
    owner_id, _task_id = _seed_task()
    other_owner_id, _other_task_id = _seed_task()
    credential_id, route_id = _store_ocr_route(owner_id)
    registry = _StageRegistry()

    options = list_stage_provider_options(owner_id, registry)
    ocr_option = next(item for item in options if item["provider_id"] == route_id)
    assert ocr_option["provider_kind"] == "ocr"
    assert ocr_option["credential_id"] == credential_id
    assert ocr_option["scope"] == "owner"
    assert ocr_option["is_shared"] is False
    assert "fake-baidu-ak" not in repr(options)
    assert "fake-baidu-sk" not in repr(options)
    assert route_id not in registry.providers

    route = resolve_stage_provider_route(
        owner_id=owner_id,
        registry=registry,
        requested_route_id=route_id,
    )
    assert route.credential_id == credential_id
    with pytest.raises(ValidationError) as wrong_owner:
        resolve_stage_provider_route(
            owner_id=other_owner_id,
            registry=registry,
            requested_route_id=route_id,
        )
    assert wrong_owner.value.code == "ocr_credential_not_found"


@pytest.mark.asyncio
async def test_question_ocr_success_reuses_artifact_and_freezes_three_stage_routes(
    monkeypatch,
):
    owner_id, task_id = _seed_task()
    credential_id, route_id = _store_ocr_route(owner_id)
    registry = _StageRegistry()
    question_skill = _FakeDocumentOCRSkill(
        "# 1. Explain dependency injection\n\nUse constructor injection."
    )
    factory_calls: list[tuple[str, str, str | None]] = []

    def fake_factory(request_owner, route):
        factory_calls.append((request_owner, route.route_id, route.credential_id))
        return question_skill

    monkeypatch.setattr(
        task_preparation,
        "build_owner_baidu_ocr_skill",
        fake_factory,
    )

    async def preflight_once():
        return await task_preparation.preflight_problem_source(
            task_id=task_id,
            file=UploadFile(
                file=io.BytesIO(PNG_1X1),
                filename="questions.png",
                headers=Headers({"content-type": "image/png"}),
            ),
            library_material_id=None,
            stored_file_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            recognition_provider_id=route_id,
            current=SimpleNamespace(id=owner_id),
            registry=registry,
        )

    prepared = await preflight_once()
    replayed = await preflight_once()
    assert replayed["source_token"] == prepared["source_token"]
    assert factory_calls == [(owner_id, route_id, credential_id)]
    assert question_skill.calls == [(PNG_1X1, "questions.png", "problems")]
    assert question_skill.client.closed is True

    background = _BackgroundTasks()
    started = await task_preparation.start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[prepared["source_token"]],
            expected_workflow_revision=prepared["workflow_revision"],
            score_policy=QuestionScorePolicy(),
            recognition_provider_id=route_id,
        ),
        background_tasks=background,
        current=SimpleNamespace(id=owner_id),
        registry=registry,
    )
    assert started["recognition_provider_id"] == route_id
    assert len(background.calls) == 1
    function, args, kwargs = background.calls[0]
    await function(*args, **kwargs)

    question_job = workflow_repository.get_operation(
        started["job_id"],
        owner_id=owner_id,
    )
    questions = assignment_repository.list_questions(
        task_id,
        teacher_id=owner_id,
    )
    assert question_job.status == "done"
    assert len(questions) == 1
    assert "Explain dependency injection" in questions[0].stem

    async def fake_submission_parse(sources, _problems, _provider, **_kwargs):
        source = sources[0]
        return [SubmissionSourceParseResult(
            source_id=source.source_id,
            stored_file_id=source.stored_file_id,
            filename=source.filename,
            status="parsed",
            student={
                "stu_id": "S001",
                "stu_name": "Student One",
                "stu_ans": [{
                    "q_id": "q1",
                    "number": "1",
                    "type": "short",
                    "content": "answer",
                    "flag": [],
                }],
                "source_filename": source.filename,
                "source_id": source.source_id,
                "stored_file_id": source.stored_file_id,
                "identity_match_method": "filename",
                "identity_status": "matched",
            },
            student_candidate="S001",
            matched_answer_count=1,
            unknown_question_ids=(),
            stable_error_code=None,
            failure_phase=None,
            retryable=False,
        )]

    monkeypatch.setattr(
        task_facade,
        "parse_student_answer_sources",
        fake_submission_parse,
    )
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: registry,
    )
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="S001_Student.txt",
        content=b"answer",
        content_type="text/plain",
        registry=registry,
        recognition_provider_id="llm-question",
    )
    claimed = workflow_repository.claim_operation(
        queued["job_id"],
        owner_id=owner_id,
        worker_id="stage-route-worker",
        lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(
            claimed,
            worker_id="stage-route-worker",
            lease_seconds=60,
        )
    )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    grading_setup = TaskGradingSetup(
        selected_provider_ids=["llm-grading"],
        primary_provider_id="llm-grading",
        knowledge_scope="none",
    )
    saved = tasks.save_grading_setup(
        task_id=task_id,
        request=UpdateGradingSetupRequest(
            expected_workflow_revision=workflow.workflow_revision,
            grading_setup=grading_setup.model_dump(mode="json"),
        ),
        current=SimpleNamespace(id=owner_id),
        registry=registry,
    )
    assert saved["status"] == "saved"
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert workflow.question_recognition_provider_id == route_id
    assert workflow.submission_recognition_provider_id == "llm-question"
    assert workflow.grading_setup["selected_provider_ids"] == ["llm-grading"]


@pytest.mark.asyncio
async def test_question_ocr_uncertain_submit_is_not_repeated(monkeypatch):
    owner_id, task_id = _seed_task()
    _credential_id, route_id = _store_ocr_route(owner_id)
    registry = _StageRegistry()

    class UncertainSkill(_FakeDocumentOCRSkill):
        async def recognize_document(self, file_data, file_name, purpose):
            self.calls.append((file_data, file_name, purpose))
            raise RuntimeError("provider_timeout")

    skill = UncertainSkill("")
    factory_calls = 0

    def fake_factory(_owner, _route):
        nonlocal factory_calls
        factory_calls += 1
        return skill

    monkeypatch.setattr(
        task_preparation,
        "build_owner_baidu_ocr_skill",
        fake_factory,
    )

    async def submit():
        return await task_preparation.preflight_problem_source(
            task_id=task_id,
            file=UploadFile(
                file=io.BytesIO(PNG_1X1),
                filename="uncertain.png",
                headers=Headers({"content-type": "image/png"}),
            ),
            library_material_id=None,
            stored_file_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            recognition_provider_id=route_id,
            current=SimpleNamespace(id=owner_id),
            registry=registry,
        )

    with pytest.raises(HTTPException) as first:
        await submit()
    assert first.value.detail["code"] == "provider_submit_uncertain"
    with pytest.raises(HTTPException) as replay:
        await submit()
    assert replay.value.detail["code"] == "provider_submit_uncertain"
    assert factory_calls == 1
    assert len(skill.calls) == 1


@pytest.mark.asyncio
async def test_submission_ocr_uses_original_bytes_and_preserves_source_outcome(
    monkeypatch,
):
    owner_id, task_id = _seed_task(with_question=True)
    credential_id, route_id = _store_ocr_route(owner_id)
    registry = _StageRegistry()
    skill = _FakeDocumentOCRSkill(
        "学号: S002\n姓名: Li\n1. Handwritten answer"
    )
    factory_calls: list[tuple[str, str, str | None]] = []

    def fake_factory(request_owner, route):
        factory_calls.append((request_owner, route.route_id, route.credential_id))
        return skill

    monkeypatch.setattr(task_facade, "build_owner_baidu_ocr_skill", fake_factory)
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: registry,
    )
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="S002_Li.png",
        content=PNG_1X1,
        content_type="image/png",
        registry=registry,
        recognition_provider_id=route_id,
    )
    claimed = workflow_repository.claim_operation(
        queued["job_id"],
        owner_id=owner_id,
        worker_id="ocr-success-worker",
        lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(
            claimed,
            worker_id="ocr-success-worker",
            lease_seconds=60,
        )
    )

    operation = workflow_repository.get_operation(
        queued["job_id"],
        owner_id=owner_id,
    )
    source = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )[0]
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert operation.status == "done"
    assert factory_calls == [(owner_id, route_id, credential_id)]
    assert skill.calls == [(PNG_1X1, "S002_Li.png", "submissions")]
    assert skill.client.closed is True
    assert workflow.submission_recognition_provider_id == route_id
    assert outcome is not None
    assert outcome.status == "parsed"
    assert outcome.stable_error_code is None
    assert outcome.failure_phase is None
    assert outcome.retryable is False


@pytest.mark.asyncio
async def test_submission_inflight_without_artifact_never_resubmits(monkeypatch):
    owner_id, task_id = _seed_task(with_question=True)
    _credential_id, route_id = _store_ocr_route(owner_id)
    registry = _StageRegistry()
    skill = _FakeDocumentOCRSkill("must not be submitted")
    monkeypatch.setattr(
        task_facade,
        "build_owner_baidu_ocr_skill",
        lambda _owner, _route: skill,
    )
    monkeypatch.setattr(
        task_facade,
        "_registry_for_owner",
        lambda _owner: registry,
    )
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="S003_Lost.png",
        content=PNG_1X1,
        content_type="image/png",
        registry=registry,
        recognition_provider_id=route_id,
    )
    operation = workflow_repository.get_operation(
        queued["job_id"],
        owner_id=owner_id,
    )
    source = source_outcome_repository.list_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )[0]
    workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=operation.checkpoint_revision,
        stage="submission_ocr_submitting",
        checkpoint={
            **dict(operation.checkpoint or {}),
            "ocr_inflight_source_id": source.id,
        },
        artifact_refs=operation.artifact_refs,
    )
    claimed = workflow_repository.claim_operation(
        operation.id,
        owner_id=owner_id,
        worker_id="ocr-uncertain-worker",
        lease_seconds=60,
    )
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(
            claimed,
            worker_id="ocr-uncertain-worker",
            lease_seconds=60,
        )
    )

    failed = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_submit_uncertain"
    assert task_facade._operation_is_retryable(failed) is False
    assert skill.calls == []
    assert outcome is not None
    assert outcome.stable_error_code == "provider_submit_uncertain"
    assert outcome.failure_phase == "ocr"
    assert outcome.retryable is False

    replay = task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename="S003_Lost.png",
        content=PNG_1X1,
        content_type="image/png",
        registry=registry,
        recognition_provider_id=route_id,
    )
    persisted = workflow_repository.get_operation(operation.id, owner_id=owner_id)
    assert replay["status"] == "already_done"
    assert replay["job_id"] == operation.id
    assert persisted.attempt == operation.attempt
    assert skill.calls == []


def test_baidu_can_be_saved_for_grading_but_execution_fails_before_run():
    owner_id = "baidu-grading-boundary-owner"
    assignment, _question, workflow = _seed_figma_grading_task(owner_id)
    _credential_id, route_id = _store_ocr_route(owner_id)
    setup = TaskGradingSetup(
        selected_provider_ids=[route_id],
        primary_provider_id=route_id,
        knowledge_scope="none",
    )
    # Selection is intentionally not capability-filtered.
    tasks._validate_grading_setup(setup, _StageRegistry(), owner_id)
    workflow = workflow_repository.update_workflow(
        assignment.id,
        owner_id=owner_id,
        grading_setup=setup.model_dump(mode="json"),
        grading_setup_fingerprint="ocr-selected",
    )

    with pytest.raises(ValidationError) as unsupported:
        task_facade.start_task_grading(
            task_id=assignment.id,
            owner_id=owner_id,
            expected_workflow_revision=workflow.workflow_revision,
        )
    assert unsupported.value.code == "ocr_provider_grading_not_supported"
    assert grading_repository.list_runs_for_assignment(
        assignment.id,
        actor_id=owner_id,
    ) == []
    with pytest.raises(ValidationError) as direct_boundary:
        assert_grading_routes_supported(setup)
    assert direct_boundary.value.code == "ocr_provider_grading_not_supported"


@pytest.mark.asyncio
async def test_baidu_media_limits_run_in_killable_worker():
    document = fitz.open()
    try:
        for _ in range(501):
            document.new_page()
        oversized_pdf = document.tobytes()
    finally:
        document.close()
    with pytest.raises(HTTPException) as pdf_limit:
        await inspect_baidu_ocr_upload(
            oversized_pdf,
            "too-many-pages.pdf",
            content_type="application/pdf",
        )
    assert pdf_limit.value.detail == {
        "code": "pdf_page_limit_exceeded",
        "max_pages": 500,
    }

    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8193, 1), False)
    pixmap.clear_with(255)
    oversized_image = pixmap.tobytes("png")
    pixmap = None
    with pytest.raises(HTTPException) as image_limit:
        await inspect_baidu_ocr_upload(
            oversized_image,
            "too-wide.png",
            content_type="image/png",
        )
    assert image_limit.value.detail == {
        "code": "ocr_image_dimension_limit_exceeded",
        "max_side": 8192,
    }
