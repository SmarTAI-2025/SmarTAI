"""Uploaded material survives independent arrival, failure, and ambiguous identity."""
import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import RecognitionError, ValidationError
from backend.models import QuestionScorePolicy
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.question_preparation_artifacts import QUESTIONS_EXTRACTED_STAGE, read_base_preparation_artifact
from backend.tests.test_incremental_question_preparation import generated, persisted, row, setup
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
from backend.tests.test_task_background_workflows import _seed_task


async def material_setup(monkeypatch, materials):
    """Publish real source records and a fenced operation; no fake persistence."""
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    tokens = []
    for role, text in [("problem", "Question text"), *materials]:
        source = await api.preflight_problem_source(task_id=task,
            file=None if role == "problem" else UploadFile(io.BytesIO(text.encode()), filename=role + ".txt",
                headers=Headers({"content-type": "text/plain"})),
            inline_text=text if role == "problem" else None,
            library_material_id=None, stored_file_id=None, structure_mode="organized", role=role,
            extraction_hint="", save_to_library=False, recognition_provider_id="test-provider",
            current=SimpleNamespace(id=owner), registry=registry)
        assert isinstance(source, dict), getattr(source, "body", source)
        tokens.append(source["source_token"])
    queued = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=tokens, expected_workflow_revision=0,
            score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10),
            recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None,
        allow_prepared_source_reuse=False)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    operation = _claim(owner, queued["job_id"], "material-test")
    inputs = api._rehydrate_question_preparation_inputs(operation)

    async def read(source, **kwargs):
        return source

    pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(operation.operation_id), score_policy=inputs["score_policy"], read_source=read)
    return owner, task, operation, pipeline


def read_unit(owner, operation, file_id):
    return read_base_preparation_artifact(file_id, owner_id=owner, task_id=operation.assignment_id,
        operation_id=operation.operation_id, attempt=operation.attempt, input_hash=operation.input_hash,
        provider_record_id="test-provider", stage=QUESTIONS_EXTRACTED_STAGE).payload.problem_data


def parsed_material(number, text):
    return inc._MaterialItems(items=[inc._MaterialItem(number=number, text_value=text)]), None


@pytest.mark.asyncio
@pytest.mark.parametrize("function_mode", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
async def test_test_case_fragments_merge_without_loss_in_saved_and_manual_views(monkeypatch, function_mode, conflict):
    from backend.models import TestCase
    from backend.services.task_execution import stop_task_run

    owner, task, operation, pipeline = await material_setup(monkeypatch, [("programming_tests", "Test cases")])
    problem, tests = [source[0] for source in pipeline.sources]
    def case(value, result):
        return TestCase(**(dict(function_name="solve", function_args=[value], expected_return=result, io_mode="function")
            if function_mode else dict(input=value, expected_output=result))).model_dump()
    first, second = case("1", "2"), case("2", "4")
    extra = case("1", "3") if conflict else case("3", "6")
    def material(number, cases):
        return inc._rows([dict(number=number, test_cases=cases, material_provenance={"test_cases": dict(
            source_token=tests.source_token, source_filename=tests.filename, source_kind="upload",
            authority="uploaded_material", review_status="pending", scope="question")})])

    # Parts finish out of order. Each result is persisted with the real store.
    await pipeline.finish(pipeline._chunk_key(tests, "later", 1, ""), material("1", [second, extra]))
    await pipeline.finish(pipeline._chunk_key(tests, "earlier", 0, ""), material("1", [first, second]))
    await pipeline.finish(pipeline._chunk_key(tests, "other", 2, ""), material("2", [case("9", "18")]))
    await pipeline.finish(pipeline._chunk_key(problem, "questions", 0, ""), inc._rows([
        row(index, criterion="Teacher rubric.", reference_answer="Teacher answer.") for index in (1, 2)]))
    _, saved = persisted(owner, operation)
    assert saved["q1"]["test_cases"] == [first, second, extra]
    assert saved["q2"]["test_cases"] == [case("9", "18")]
    assert any(issue["code"] == "source_conflict" and issue["field"] == "programming_tests"
               for issue in saved["q1"]["preparation_issues"]) is conflict

    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    forbidden = AsyncMock(side_effect=AssertionError("Manual test-case merging must never call AI"))
    monkeypatch.setattr(inc, "structured_llm_call", forbidden)
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert published["q1"]["test_cases"] == saved["q1"]["test_cases"]
    assert published["q2"]["test_cases"] == saved["q2"]["test_cases"]
    assert any(issue.get("details", {}).get("reason") == "test_case_output_conflict"
               for issue in published["q1"]["preparation_issues"]) is conflict
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", [False, True])
async def test_repeated_test_case_groups_are_complete_unless_pages_are_missing(monkeypatch, gap):
    from backend.models import TestCase
    pages = [dict(page_number=page, text=text) for page, text in zip(
        (1, 3, 4) if gap else (1, 2, 3),
        ['Question 1\n{"input":"1","expected_output":"2"}',
         'Question 1\n{"input":"2","expected_output":"4"}',
         'Question 2\n{"input":"9","expected_output":"18"}'])]
    text = "\n\n".join(page["text"] for page in pages)
    owner, _, operation, pipeline = await material_setup(monkeypatch, [("programming_tests", text)])
    async def read(source, *, on_pages):
        if source[0].role == "programming_tests":
            for page in pages:
                await on_pages([page])
            if gap:
                return source[0], text, {**source[2], "source_error": "recognition_response_invalid",
                    "recognition": {"coverage": {"failed_pages": [2], "processed_pages": [1, 3, 4]}}}
        return source
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(i, criterion="Teacher rubric.", reference_answer="Teacher answer.") for i in (1, 2)])))
    async def material(*args, **kwargs):
        prompt = kwargs["user_prompt"]
        return inc._MaterialItems(items=[inc._MaterialItem(number=number,
            test_cases=[TestCase(input=value, expected_output=output)])
            for number, value, output in [("1", "1", "2"), ("1", "2", "4"), ("2", "9", "18")]
            if f'"input":"{value}"' in prompt]), None
    monkeypatch.setattr(inc, "structured_llm_call", material)
    generation = AsyncMock(side_effect=AssertionError("Supplied material must not be generated"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generation)
    if gap:
        with pytest.raises(RecognitionError):
            await pipeline.run()
    else:
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert [case["input"] for case in rows["q1"]["test_cases"]] == ["1", "2"]
    assert [case["input"] for case in rows["q2"]["test_cases"]] == ["9"]
    assert any(issue["code"] == "recognition_partial" and issue["field"] == "programming_tests"
        for row in rows.values() for issue in row["preparation_issues"]) is gap
    generation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other", [
    ("problem", "stem", "criterion"),
    ("rubric", "criterion", "reference_answer"),
    ("reference_answer", "reference_answer", "criterion"),
])
@pytest.mark.parametrize("boundary", ["gap", "repeated"])
async def test_source_retry_repartitions_saved_units_and_clears_resolved_boundary_errors(monkeypatch, role, target, other, boundary):
    old = "Previously read fragment."
    complete = old + " Newly recovered continuation."
    second = "Independent second item."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [] if role == "problem" else [(role, old)])
    token = next(source[0].source_token for source in pipeline.sources if source[0].role == role)
    retry = False

    async def read(source, *, on_pages):
        draft, text, payload = source
        if draft.role != role:
            return source
        if retry:
            pages = [dict(page_number=1, text="Question 1\n" + complete),
                     dict(page_number=2, text="Question 2\n" + second)]
            await on_pages(pages)
            return draft, "\n\n".join(page["text"] for page in pages), {**payload, "source_error": None}
        if boundary == "repeated":
            # A different intervening question makes this a genuinely repeated
            # identity, unlike a running label on a continuation page.
            pages = [dict(page_number=1, text="Question 1\n" + old + "\nQuestion 99\nUnrelated item."),
                     dict(page_number=2, text="Question 1\n" + old)]
            await on_pages(pages)
            return draft, "\n\n".join(page["text"] for page in pages), payload
        return draft, old, {**payload, "source_error": "recognition_response_invalid"}

    async def extract(text, *args, **kwargs):
        if role != "problem":
            return inc._rows([{**row(index), other: "Supplied other field."} for index in (1, 2)])
        number = 2 if second in text else 1
        value = second if number == 2 else complete if complete in text else old
        return inc._rows([row(number, stem=value, criterion="Teacher rubric.", reference_answer="Teacher answer.")])

    async def material(*args, **kwargs):
        text = kwargs["user_prompt"]
        number = 2 if second in text else 1
        value = second if number == 2 else complete if complete in text else old
        return parsed_material(str(number), value)

    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "structured_llm_call", material)
    forbidden = AsyncMock(side_effect=AssertionError("Uploaded fields must be reused"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    with pytest.raises((RecognitionError, ValidationError)):
        await pipeline.run()
    before, partial = persisted(owner, operation)
    assert any(issue["code"] == "recognition_partial" for issue in partial["q1"]["preparation_issues"])
    old_keys = {key for key in before.checkpoint["incremental_unit_artifacts"] if key.split(":")[1:2] == [token]}
    assert old_keys
    retry = True
    pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=list(pipeline.sources),
        provider=_RecoveryRegistry().provider, provider_id="test-provider",
        reporter=ProgressReporter(operation.operation_id), score_policy=pipeline.policy, read_source=read)
    result = await pipeline.run()
    after, stored = persisted(owner, operation)
    assert len(result) == len(stored) == 2
    assert [stored[q][target] for q in ("q1", "q2")] == [complete, second]
    assert not any(issue["code"] in {"recognition_partial", "source_conflict"}
                   for item in stored.values() for issue in item["preparation_issues"])
    assert not old_keys.intersection(after.checkpoint["incremental_unit_artifacts"])
    assert not after.checkpoint["incremental_source_invalidations"].get(token)
    assert not after.checkpoint["incremental_errors"]
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other", [
    ("problem", "stem", "criterion"),
    ("rubric", "criterion", "reference_answer"),
    ("reference_answer", "reference_answer", "criterion"),
])
@pytest.mark.parametrize("same_text", [False, True])
async def test_completed_source_partition_replaces_partial_version_even_at_manual_handoff(monkeypatch, role, target, other, same_text):
    from backend.services.task_execution import stop_task_run
    old_text = "Previously read fragment."
    new_text = old_text if same_text else "Previously read fragment. Newly recovered continuation."
    owner, task, operation, pipeline = await material_setup(monkeypatch, [] if role == "problem" else [(role, old_text)])
    affected = next(source for source in pipeline.sources if source[0].role == role)
    draft = affected[0]
    old_key = pipeline._chunk_key(draft, old_text, 0, "", False)
    new_key = pipeline._chunk_key(draft, new_text, 0, "", True)
    assert old_key != new_key  # Completing a boundary changes even identical text.
    if role != "problem":
        question = pipeline.sources[0][0]
        await pipeline.finish(pipeline._chunk_key(question, "Question", 0, ""),
            inc._rows([{**row(1), other: "Supplied other field."}]))
    async def data(text, partial):
        if role == "problem":
            result = inc._rows([row(1, stem=text, criterion="Teacher rubric.", reference_answer="Teacher answer.")])
        else:
            monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("1", text)))
            result, error = await pipeline._material(draft, text)
            assert error is None
        if partial:
            result["q1"]["preparation_issues"] = [inc._issue("q1", inc._issue_field(target),
                "recognition_partial", "blocking", [draft.source_token])]
        return result
    await pipeline.finish(old_key, await data(old_text, True))
    await pipeline.finish(new_key, await data(new_text, False))
    # Simulate the exact stop window: the new partition is persisted, but no
    # replacement aggregate has been written yet. Manual must project units.
    await pipeline._checkpoint(incremental_view_from_units=True, incremental_source_plans={
        draft.source_token: dict(keys=[new_key], source_complete=True)})
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    forbidden = AsyncMock(side_effect=AssertionError("Version selection must be deterministic"))
    monkeypatch.setattr(inc, "structured_llm_call", forbidden)
    monkeypatch.setattr(inc, "_extract_problems_call", forbidden)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert len(published) == 1 and published["q1"][target] == new_text
    assert not any(issue["code"] in {"recognition_partial", "source_conflict"}
                   for issue in published["q1"]["preparation_issues"])
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other,field", [
    ("reference_answer", "reference_answer", "criterion", "answer"),
    ("rubric", "criterion", "reference_answer", "rubric"),
])
@pytest.mark.parametrize("boundary", ["gap", "repeated"])
async def test_partial_teacher_material_keeps_field_warning_in_manual_matrix(monkeypatch, role, target, other, field, boundary):
    from backend.services.task_execution import stop_task_run
    literal = "Literal teacher material."
    owner, task, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    async def read(source, *, on_pages):
        draft, text, payload = source
        if draft.role == role:
            if boundary == "gap":
                return draft, text, {**payload, "source_error": "recognition_response_invalid",
                    "recognition": {"coverage": {"processed_pages": [1, 3], "failed_pages": [2]}}}
            await on_pages([dict(page_number=i, text=f"Question {number}\n{literal}")
                            for i, number in enumerate([1, 2, 1], 1)])
        return source
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        {**row(1), other: "Already supplied."}])))
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("1", literal)))
    generate = AsyncMock(side_effect=AssertionError("Partial teacher material must remain explicit"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises((RecognitionError, ValidationError)):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert rows["q1"][target] == literal
    assert any(issue["field"] == field and issue["code"] == "recognition_partial"
               for issue in rows["q1"]["preparation_issues"])
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert published[target] == literal
    assert any(issue["field"] == field and issue["code"] == "recognition_partial"
               for issue in published["preparation_issues"])
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
async def test_uploaded_material_is_saved_before_question_arrives(monkeypatch, role, target, other):
    literal = "Teacher supplied material for question 1."
    owner, task, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    material_saved, release_question = asyncio.Event(), asyncio.Event()
    material_source = next(source[0] for source in pipeline.sources if source[0].role == role)
    original_finish = pipeline.finish

    async def finish(key, *args, **kwargs):
        await original_finish(key, *args, **kwargs)
        if key.startswith("material:"):
            material_saved.set()

    async def read(source, **kwargs):
        if source[0].role == "problem":
            await release_question.wait()
        return source

    pipeline.finish, pipeline.read_source = finish, read
    extract = AsyncMock(return_value=inc._rows([{**row(1), other: "Previously supplied field."}]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("1", literal)))
    generate = AsyncMock(side_effect=AssertionError("Complete teacher materials must not be generated"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(material_saved.wait(), 3)
        saved, rows = persisted(owner, operation)
        extract.assert_not_awaited()
        assert len(rows) == 1 and rows["q1"]["stem"] == ""
        assert rows["q1"]["type"] == ""  # Unknown is not an invented "Other" classification.
        assert rows["q1"][target] == literal
        provenance = rows["q1"]["material_provenance"][target]
        assert provenance["source_token"] == material_source.source_token
        assert provenance["source_kind"] == "upload"
        material_key = next(key for key in saved.checkpoint["incremental_unit_artifacts"] if key.startswith("material:"))
        material_file = saved.checkpoint["incremental_unit_artifacts"][material_key]
        assert read_unit(owner, operation, material_file)["q1"][target] == literal
        release_question.set()
        result = await asyncio.wait_for(runner, 3)
        assert len(result) == 1 and result["q1"]["stem"] == "Explain item 1."
        assert result["q1"][target] == literal and result["q1"][other] == "Previously supplied field."
        final, _ = persisted(owner, operation)
        assert final.checkpoint["incremental_unit_artifacts"][material_key] == material_file
        assert final.checkpoint["preparation_counts"] == dict(questions=1, stems=1, rubrics=1, reference_answers=1)
        generate.assert_not_awaited()
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reading", "extracting", "nonliteral"])
async def test_failed_uploaded_rubric_never_generates_a_replacement_but_other_fields_continue(monkeypatch, failure):
    rubric, answer = "Original teacher rubric.", "Original teacher answer for question 1."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [("rubric", rubric), ("reference_answer", answer)])
    rubric_source = next(source[0] for source in pipeline.sources if source[0].role == "rubric")

    async def read(source, **kwargs):
        if source[0].role == "rubric" and failure == "reading":
            raise RecognitionError("ocr_incomplete")
        return source

    async def material(*args, **kwargs):
        if rubric in kwargs["user_prompt"]:
            if failure == "extracting":
                raise ValidationError("Malformed teacher material", code="provider_response_invalid")
            return parsed_material("1", "An invented replacement rubric.")
        return parsed_material("1", answer)

    pipeline.read_source = read
    monkeypatch.setattr(inc, "structured_llm_call", material)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(1), row(2)])))
    generation_calls = []

    async def generate(data, targets, *args, **kwargs):
        generation_calls.append((data["q1"]["number"], [target["target"] for target in targets]))
        return [generated(target["target"], "Generated answer for question 2.") for target in targets]

    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises((RecognitionError, ValidationError)):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert [value["number"] for value in rows.values()] == ["1", "2"]
    assert all(value["stem"] and not value["criterion"] for value in rows.values())
    assert rows["q1"]["reference_answer"] == answer
    assert rows["q2"]["reference_answer"] == "Generated answer for question 2."
    assert generation_calls == [("2", ["reference_answer"])]
    assert rubric_source.source_token in pipeline.source_failures
    assert not saved.checkpoint["incremental_inflight_units"]
    original = workflow_repository.get_operation(rubric_source.source_token, owner_id=owner)
    assert original.payload["text"] == rubric


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
async def test_unlabelled_material_matches_only_a_single_known_question(monkeypatch, count, role, target, other):
    literal = "Literal unlabelled teacher material."
    owner, task, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    monkeypatch.setattr(inc, "structured_llm_call", AsyncMock(return_value=parsed_material("", literal)))
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        {**row(index), other: "Already supplied other field."} for index in range(1, count + 1)])))
    calls = []

    async def generate(data, targets, *args, **kwargs):
        calls.append((data["q1"]["number"], [item["target"] for item in targets]))
        return [generated(item["target"], "AI draft awaiting review.") for item in targets]

    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    if count == 1:
        result = await pipeline.run()
        assert len(result) == 1 and result["q1"][target] == literal
        assert not calls
    else:
        with pytest.raises(ValidationError) as failed:
            await pipeline.run()
        assert failed.value.code == "question_targets_incomplete"
        _, result = persisted(owner, operation)
        unassigned = [value for value in result.values() if not value["number"]]
        assert len(unassigned) == 1 and unassigned[0][target] == literal
        assert unassigned[0]["stem"] == ""
        assigned = [value for value in result.values() if value["number"]]
        assert len(assigned) == 2 and all(value[target] != literal for value in assigned)
        from backend.services.task_execution import stop_task_run
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                      expected_revision=workflow.workflow_revision)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        response = await api.manually_complete_question_preparation(task, operation.operation_id,
            api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
            current=SimpleNamespace(id=owner))
        assert response["status"] == "ready_for_review"
        published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
        unassigned = [value for value in published.values() if not value["number"]]
        assert len(unassigned) == 1 and unassigned[0][target] == literal
    saved, _ = persisted(owner, operation)
    material_id = next(file_id for key, file_id in saved.checkpoint["incremental_unit_artifacts"].items()
                       if key.startswith("material:"))
    original = read_unit(owner, operation, material_id)["q1"]
    assert original["number"] == "" and original[target] == literal


@pytest.mark.asyncio
async def test_repeated_later_heading_invalidates_saved_question_before_reader_finishes(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    first_saved, repeated_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    source_token = pipeline.sources[0][0].source_token
    original_finish = pipeline.finish

    async def finish(key, *args, **kwargs):
        await original_finish(key, *args, **kwargs)
        if key.startswith("extract:"):
            first_saved.set()

    async def read(source, *, on_pages):
        await on_pages([dict(page_number=1, text="Question 1\nExplain A."),
                        dict(page_number=2, text="Question 2\nExplain B.")])
        await first_saved.wait()
        await on_pages([dict(page_number=3, text="Question 1\nFurther conditions for A.")])
        repeated_seen.set()
        await release.wait()
        return source

    async def extract(text, *args, **kwargs):
        number = "2" if text.startswith("Question 2") else "1"
        if number == "2":
            await release.wait()
        return inc._rows([row(number, stem=text, criterion="Teacher rubric.", reference_answer="Teacher answer.")])

    pipeline.finish, pipeline.read_source = finish, read
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    forbidden = AsyncMock(side_effect=AssertionError("Teacher materials are complete"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(repeated_seen.wait(), 3)
        saved, rows = persisted(owner, operation)
        assert saved.checkpoint["incremental_source_invalidations"][source_token] == ["1"]
        assert any(issue["code"] == "recognition_partial" for issue in rows["q1"]["preparation_issues"])
        release.set()
        with pytest.raises(ValidationError) as failed:
            await asyncio.wait_for(runner, 3)
        assert failed.value.code == "question_targets_incomplete"
        _, final = persisted(owner, operation)
        assert all(any(issue["code"] == "recognition_partial" for issue in value["preparation_issues"])
                   for value in final.values() if value["number"] == "1")
        forbidden.assert_not_awaited()
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
@pytest.mark.parametrize("invalid", ["schema", "nonliteral"])
async def test_one_invalid_material_item_does_not_discard_other_teacher_items(monkeypatch, role, target, other, invalid):
    from pydantic import ValidationError as SchemaError

    literal = "Verbatim teacher material for question one."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [(role, literal)])
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        {**row(index), other: "Other teacher field."} for index in (1, 2)])))

    async def material(*args, **kwargs):
        # Exercise the declared response envelope: unrelated malformed items
        # must not prevent the valid literal item from reaching its own save.
        return kwargs["output_model"].model_validate({"items": [
            {"number": "1", "text_value": literal},
            {"number": "2", "text_value": {"bad": "object"} if invalid == "schema" else "Invented teacher text."},
        ]}), None

    monkeypatch.setattr(inc, "structured_llm_call", material)
    forbidden = AsyncMock(side_effect=AssertionError("Failed teacher material must not be replaced with generated content"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    with pytest.raises((ValidationError, SchemaError)):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert rows["q1"][target] == literal
    assert not rows["q2"].get(target)
    material_id = next(file_id for key, file_id in saved.checkpoint["incremental_unit_artifacts"].items()
                       if key.startswith("material:"))
    unit = read_unit(owner, operation, material_id)
    assert len(unit) == 1 and unit["q1"][target] == literal
    assert saved.checkpoint["incremental_errors"]
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["same_run", "explicit_retry"])
@pytest.mark.parametrize("role,target,other", [
    ("reference_answer", "reference_answer", "criterion"),
    ("rubric", "criterion", "reference_answer"),
])
async def test_partial_material_repair_preserves_saved_siblings_and_retries_incomplete_unit(monkeypatch, recovery, role, target, other):
    first_literal, second_literal = "Teacher material for first item.", "Teacher material for second item."
    owner, _, operation, pipeline = await material_setup(monkeypatch, [(role, first_literal + "\n" + second_literal)])
    extract = AsyncMock(return_value=inc._rows([
        {**row(index), other: "Previously supplied other field."} for index in (1, 2)]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    repairing, release = asyncio.Event(), asyncio.Event()
    calls = []
    retry_started = False

    async def material(*args, **kwargs):
        calls.append(kwargs["user_prompt"])
        mixed = len(calls) == 1 or (recovery == "explicit_retry" and not retry_started)
        if mixed:
            items = [{"number": "1", "text_value": first_literal},
                     {"number": "2", "text_value": {"invalid": "object"}}]
        else:
            repairing.set()
            await release.wait()
            # Repair only the missing item; the earlier literal must survive.
            items = [{"number": "2", "text_value": second_literal}]
        return kwargs["output_model"].model_validate({"items": items}), None

    monkeypatch.setattr(inc, "structured_llm_call", material)
    forbidden = AsyncMock(side_effect=AssertionError("Teacher material repair must not generate substitute fields"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", forbidden)
    if recovery == "explicit_retry":
        with pytest.raises(ValidationError):
            await pipeline.run()
        before, rows = persisted(owner, operation)
        assert len(calls) == 3
        assert rows["q1"][target] == first_literal and not rows["q2"].get(target)
        assert len(before.checkpoint["incremental_incomplete_units"]) == 1
        assert before.checkpoint["incremental_incomplete_units"][0].startswith("material:")
        assert not before.checkpoint["incremental_inflight_units"]
        retry_started = True
        pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=list(pipeline.sources),
            provider=_RecoveryRegistry().provider, provider_id="test-provider",
            reporter=ProgressReporter(operation.operation_id), score_policy=pipeline.policy,
            read_source=pipeline.read_source)
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(repairing.wait(), 3)
        saved, partial = persisted(owner, operation)
        assert partial["q1"][target] == first_literal
        assert not partial["q2"].get(target)
        material_key = next(key for key in saved.checkpoint["incremental_unit_artifacts"] if key.startswith("material:"))
        partial_file = saved.checkpoint["incremental_unit_artifacts"][material_key]
        assert read_unit(owner, operation, partial_file)["q1"][target] == first_literal
        release.set()
        complete = await asyncio.wait_for(runner, 3)
        assert [complete[q_id][target] for q_id in ("q1", "q2")] == [first_literal, second_literal]
        final, _ = persisted(owner, operation)
        assert not final.checkpoint["incremental_incomplete_units"]
        assert not final.checkpoint["incremental_errors"]
        assert not final.checkpoint["incremental_inflight_units"]
        assert len(calls) == (2 if recovery == "same_run" else 4)
        extract.assert_awaited_once()
        forbidden.assert_not_awaited()
        # The immutable earlier artifact still contains the accepted first item.
        assert read_unit(owner, operation, partial_file)["q1"][target] == first_literal
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
