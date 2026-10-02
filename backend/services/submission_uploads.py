"""Resumable authenticated upload, recognition, then immutable publication."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from starlette.concurrency import run_in_threadpool

from backend.agents.ingest_agent import SubmissionSourceInput, parse_student_answer_sources
from backend.db import assignment_repository, file_repository, submission_repository, workflow_repository
from backend.db import submission_upload_repository
from backend.domain.errors import DomainError, ValidationError
from backend.services.recognition_artifacts import RecognitionArtifactBindingV1
from backend.services.question_sources import read_question_source, recognition_needs_review
from backend.services.stage_provider_routing import StageProviderRoute, stage_provider_configuration_fingerprint
from backend.storage import get_storage
from backend.tools.file_processing import extract_raw_files_from_archive, inspect_upload_content
from backend.progress.tracker import ProgressReporter

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 200_000
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_candidate(storage, stored):
    if stored is None or stored.availability_status != "available" or stored.size_bytes > MAX_CANDIDATE_BYTES:
        raise ValidationError("Recognition evidence is unavailable.", code="recognition_artifact_unavailable")
    with storage.open(stored.storage_key) as stream:
        content = stream.read(MAX_CANDIDATE_BYTES + 1)
    if len(content) != stored.size_bytes or hashlib.sha256(content).hexdigest() != stored.sha256:
        raise ValidationError("Recognition evidence changed.", code="recognition_artifact_invalid")
    return json.loads(content)


async def recognize_submission_upload(*, assignment_id, student_id, actor_id, source,
                                      filename, content, content_type, provider, registry=None):
    from backend.services.submissions import _build_answers, _problem_store

    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValidationError("Upload exceeds the supported byte limit.", code="submission_upload_limit_exceeded")
    await run_in_threadpool(submission_repository.validate_submission_access,
                            assignment_id, student_id=student_id)
    assignment = await run_in_threadpool(assignment_repository.get_assignment_unscoped, assignment_id)
    teacher_id = assignment.teacher_id
    if actor_id not in {teacher_id, student_id}:
        raise ValidationError("Upload actor is not authorized.")
    questions = await run_in_threadpool(assignment_repository.get_questions_by_assignment, assignment_id=assignment_id)
    if not questions:
        raise ValidationError("Assignment has no questions")
    problems = _problem_store(questions)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    fingerprint = stage_provider_configuration_fingerprint(owner_id=actor_id, route=route, registry=registry)
    input_hash = _digest(dict(sha256=hashlib.sha256(content).hexdigest(), filename=filename,
                             student_id=student_id, actor_id=actor_id, source=source,
                             route=fingerprint, questions=problems, version=1))
    await run_in_threadpool(workflow_repository.ensure_workflow, assignment_id=assignment_id, owner_id=teacher_id)
    await run_in_threadpool(submission_repository.create_submission, assignment_id, student_id=student_id)
    operation, _ = await run_in_threadpool(
        workflow_repository.create_operation, assignment_id=assignment_id, owner_id=teacher_id,
        operation_type="submission_upload_v2", input_hash=input_hash, retry_existing=False,
        payload=dict(student_id=student_id, actor_id=actor_id),
    )
    if operation.terminal_summary:
        if operation.status == "completed":
            return await run_in_threadpool(submission_repository.get_revision,
                                            operation.terminal_summary["revision_id"], actor_id=actor_id)
        raise ValidationError("Upload needs review before another attempt.",
                              code=operation.terminal_summary.get("error_code", "submission_parse_failed"))
    worker_id = "submission-upload-" + uuid.uuid4().hex
    operation = await run_in_threadpool(workflow_repository.claim_operation, operation.id,
                                        owner_id=teacher_id, worker_id=worker_id, lease_seconds=960)
    storage = get_storage()
    reporter = ProgressReporter(operation.id)

    async def checkpoint(stage, **updates):
        nonlocal operation
        data = dict(operation.checkpoint or {})
        data.update(updates)
        operation = await run_in_threadpool(workflow_repository.save_operation_checkpoint,
            operation.id, owner_id=teacher_id, expected_attempt=operation.attempt,
            expected_checkpoint_revision=operation.checkpoint_revision,
            expected_lease_token=operation.lease_token, stage=stage, checkpoint=data)

    try:
        async with asyncio.timeout(900):
            revision_id = await run_in_threadpool(submission_upload_repository.stage_revision,
                operation=operation, student_id=student_id, source=source, filename=filename)
            operation = await run_in_threadpool(workflow_repository.get_operation, operation.id, owner_id=teacher_id)
            binding = RecognitionArtifactBindingV1(link="submission_revision", business_id=revision_id)
            fence = dict(fence_operation_id=operation.id, fence_operation_attempt=operation.attempt,
                         fence_lease_token=operation.lease_token)
            candidate_name = "submission-candidate-" + operation.id + ".json"
            candidate = await run_in_threadpool(file_repository.find_latest_linked_file,
                owner_id=teacher_id, kind="submission_parse_candidate_v1",
                original_name_prefix=candidate_name, submission_revision_id=revision_id)
            if candidate is not None:
                answers = await run_in_threadpool(_read_candidate, storage, candidate)
            else:
                if (operation.checkpoint or {}).get("parser_pending"):
                    raise ValidationError("The previous parser submission cannot safely be replayed.",
                                          code="provider_submit_uncertain")
                raw_sources = await run_in_threadpool(extract_raw_files_from_archive,
                    content, filename, content_type=content_type)
                if not raw_sources or len(raw_sources) > 24:
                    raise ValidationError("A single student upload supports up to 24 source files.",
                                          code="submission_upload_limit_exceeded")
                originals = await run_in_threadpool(file_repository.list_files,
                    owner_id=actor_id, submission_revision_id=revision_id)

                async def persist_original(name, data, media_type):
                    digest = hashlib.sha256(data).hexdigest()
                    existing = next((item for item in originals if item.kind == "submission"
                                     and item.original_name == name and item.sha256 == digest
                                     and item.availability_status == "available"), None)
                    if existing is not None:
                        return existing
                    stored = await run_in_threadpool(file_repository.save_file, storage=storage,
                        owner_id=actor_id, kind="submission", original_name=name, content=data,
                        content_type=media_type, submission_revision_id=revision_id, **fence)
                    originals.append(stored)
                    return stored

                await persist_original(filename, content, inspect_upload_content(content, filename, content_type).content_type)
                texts, needs_review = [], False
                for item in raw_sources:
                    if item.pre_error_code or item.content is None:
                        raise ValidationError("One upload source cannot be read.",
                                              code=item.pre_error_code or "submission_source_empty")
                    stored = await persist_original(item.filename, item.content, item.content_type)
                    await reporter.set_current_step("recognizing_submission_source", message="Reading submitted work")
                    result = await read_question_source(owner_id=teacher_id, task_id=assignment_id,
                        content=item.content, filename=item.filename, content_type=item.content_type,
                        route=route, registry=registry, stored_file_id=stored.id, purpose="submissions",
                        reporter=reporter, binding=binding)
                    texts.append(f"[Source file: {item.filename}]\n{result.text}")
                    needs_review = needs_review or bool(
                        (result.recognition or {}).get("requires_review") is True
                        or recognition_needs_review(result.recognition)
                    )
                    if sum(map(len, texts)) > MAX_TRANSCRIPT_CHARS:
                        raise ValidationError("Submission text exceeds the parse budget; it was not truncated.",
                                              code="submission_upload_limit_exceeded")
                await checkpoint("submission_parser_pending", parser_pending=True)
                parsed = (await parse_student_answer_sources(
                    [SubmissionSourceInput(revision_id, originals[0].id, filename, "text/plain", "\n\n".join(texts))],
                    problems, provider, reporter=reporter, single_attempt=True,
                ))[0]
                if parsed.student is None or parsed.unknown_question_ids:
                    raise ValidationError("Answers could not be mapped reliably.",
                                          code=parsed.stable_error_code or "no_matching_answer")
                raw_answers = parsed.student.get("stu_ans") or []
                if needs_review:
                    for answer in raw_answers:
                        answer["flag"] = list(dict.fromkeys([*(answer.get("flag") or []), "recognition_needs_review"]))
                answers = _build_answers({q.q_id: q for q in questions}, raw_answers)
                if len({a["q_id"] for a in answers}) != len(answers):
                    raise ValidationError("Duplicate question mapping.", code="no_matching_answer")
                data = json.dumps(answers, ensure_ascii=False).encode()
                if len(data) > MAX_CANDIDATE_BYTES:
                    raise ValidationError("Parsed answers exceed the evidence budget.", code="submission_upload_limit_exceeded")
                candidate = await run_in_threadpool(file_repository.save_file, storage=storage,
                    owner_id=teacher_id, kind="submission_parse_candidate_v1", original_name=candidate_name,
                    content=data, content_type="application/json", submission_revision_id=revision_id, **fence)
                await checkpoint("submission_candidate_ready", candidate_id=candidate.id)
            current_questions = await run_in_threadpool(assignment_repository.get_questions_by_assignment,
                                                         assignment_id=assignment_id)
            if _digest(_problem_store(current_questions)) != _digest(problems):
                raise ValidationError("Questions changed during recognition.", code="recognition_plan_changed")
            await reporter.set_current_step("publishing_submission", message="Saving recognized answers")
            return await run_in_threadpool(submission_upload_repository.publish_revision,
                operation=operation, student_id=student_id, answers=answers,
                expected_questions=[(question.id, question.version) for question in questions])
    finally:
        try:
            await asyncio.shield(run_in_threadpool(workflow_repository.release_operation,
                operation.id, owner_id=teacher_id, worker_id=worker_id, lease_token=operation.lease_token))
        except DomainError:
            pass
