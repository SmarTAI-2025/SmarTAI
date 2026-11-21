"""Conflicts preserve partial extraction without publishing executable rules."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.domain.errors import ValidationError
from backend.services.rubric_drafts import compile_parts_draft, saved_draft, draft_fingerprint
from backend.services.rubric_review import review_extraction
from backend.tests.test_teacher_frozen_allocations import make_row, proposed
from backend.tests.test_question_material_format_repair import Provider


def test_invalid_sibling_keeps_valid_fields_as_review_only():
    row = make_row("列式3分、计算结果7分")
    raw = proposed()
    raw["parts"][0]["steps"][1]["total_pts"] = -1
    raw["secret"] = "must not be copied"
    extracted = review_extraction({"part_rubric": raw}, row)
    fields = {field["path"]: field["value"] for field in extracted["fields"]}
    assert fields["part_rubric.parts[0].steps[0].total_pts"] == 3
    assert "part_rubric.parts[0].steps[1].total_pts" not in fields
    assert "part_rubric.parts[0].steps[1].total_pts" in extracted["invalid_fields"]
    assert "secret" not in json.dumps(extracted)
    assert extracted["status"] == "needs_review"
    assert saved_draft({**row, "rubric_draft": extracted}) is None


def test_whole_invalid_step_is_locatable_without_losing_valid_sibling():
    row = make_row("列式3分、计算结果7分")
    raw = proposed()
    raw["parts"][0]["steps"][1] = None
    extracted = review_extraction({"part_rubric": raw}, row)
    assert "part_rubric.parts[0].steps[1]" in extracted["invalid_fields"]
    assert {f["path"]: f["value"] for f in extracted["fields"]}["part_rubric.parts[0].steps[0].total_pts"] == 3
    assert saved_draft({**row, "rubric_draft": extracted}) is None


@pytest.mark.parametrize("route", ["steps", "deductions", "mixed"])
def test_missing_values_are_checked_only_in_applicable_scoring_items(route):
    raw = proposed()
    part = raw["parts"][0]
    if route == "deductions":
        part.pop("steps")
    if route in {"deductions", "mixed"}:
        part["deductions"] = [{"id": "d1", "criterion": "Each independent error"}]
    if route in {"steps", "mixed"}:
        part["steps"][1].pop("total_pts")
    extracted = review_extraction({"part_rubric": raw}, make_row("评分标准"))
    invalid = extracted["invalid_fields"]
    assert not any("follow_through" in path for path in invalid)
    assert ("part_rubric.parts[0].steps[1].total_pts" in invalid) == (route != "deductions")
    assert ("part_rubric.parts[0].deductions[0].deduct_pts" in invalid) == (route != "steps")
    fields = {f["path"]: f["value"] for f in extracted["fields"]}
    assert fields["part_rubric.parts[0].total_pts"] == 10


@pytest.mark.asyncio
async def test_generated_invalid_step_retains_values_for_review_without_executable_rules():
    from backend.agents.ingest_agent import generate_missing_question_materials
    raw = proposed()
    raw["parts"][0]["steps"][1]["total_pts"] = -1
    response = {"candidates": [{"target_id": "q1:criterion", "rubric_definition": {"part_rubric": raw}}]}
    review, feedback = {}, {}
    result = await generate_missing_question_materials({"q1": make_row("")},
        [{"target_id": "q1:criterion", "q_id": "q1", "target": "criterion"}], 6,
        Provider([json.dumps(response)]), allow_partial=True, repair_feedback=feedback, review_feedback=review)
    assert result == [] and "q1:criterion" in feedback
    extracted = review["q1:criterion"]
    assert "part_rubric.parts[0].steps[1].total_pts" in extracted["invalid_fields"]
    assert {f["path"]: f["value"] for f in extracted["fields"]}["part_rubric.parts[0].steps[0].total_pts"] == 3


@pytest.mark.asyncio
async def test_generated_partial_values_survive_exhausted_retry_and_publish_for_review(monkeypatch):
    from backend.tests.test_incremental_question_preparation import setup, inc, persisted, generated, api
    from backend.services import task_facade
    owner, task, operation, pipeline = await setup(monkeypatch)
    original = make_row("")
    original["reference_answer"] = ""
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([original])))
    raw = proposed()
    raw["parts"][0]["steps"][1].pop("total_pts")
    async def generate(data, targets, *_args, **kwargs):
        kwargs["review_feedback"]["q1:criterion"] = review_extraction({"part_rubric": raw}, data["q1"])
        return [generated("reference_answer", "A correct answer.")] if not data["q1"]["reference_answer"] else []
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(operation)
    finished, rows = persisted(owner, operation)
    assert finished.status == "done"
    issue = next(i for i in rows["q1"]["preparation_issues"] if i.get("details", {}).get("rubric_review_draft"))
    assert issue["details"]["rubric_review_draft"]["input_fingerprint"] == draft_fingerprint(rows["q1"])
    assert saved_draft(rows["q1"]) is None
    view = task_facade.get_task(task_id=task, owner_id=owner)
    assert view["status"] == "problems_ready"
    saved = view["problem_data"]["q1"]
    assert any(i.get("details", {}).get("rubric_review_draft") == issue["details"]["rubric_review_draft"]
               for i in saved["preparation_issues"])
    assert saved["reference_answer"] == "A correct answer."


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_model", [False, True])
async def test_retry_of_teacher_rubric_recompiles_and_clears_old_review_issue(monkeypatch, switch_model):
    from backend.agents import incremental_question_preparation as inc
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.services import rubric_drafts, task_facade
    from backend.tests.test_incremental_question_preparation import setup, persisted, mock_supplied_rule_interpretation
    from backend.tests.test_question_preparation_recovery import _claim, _RecoveryRegistry

    owner, task, first, _ = await setup(monkeypatch)
    teacher = make_row("A correct explanation earns full credit.")
    extract = AsyncMock(return_value=inc._rows([teacher]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    raw = proposed()
    raw["parts"][0]["steps"][1].pop("total_pts")
    async def invalid(problem, *_args, **_kwargs):
        raise ValidationError("Incomplete rules.", code="invalid_structured_rubric",
            details={"rubric_review_draft": review_extraction({"part_rubric": raw}, problem)})
    monkeypatch.setattr(rubric_drafts, "compile_parts_draft", invalid)
    await api.run_durable_question_preparation(first)
    failed, partial = persisted(owner, first)
    assert failed.status == "error"
    assert any(i.get("details", {}).get("rubric_review_draft") for i in partial["q1"]["preparation_issues"])
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    registry = _RecoveryRegistry("new-model" if switch_model else "test-model")
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    retry = await api.retry_question_preparation(task_id=task, job_id=first.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision,
            use_current_configuration=switch_model), background_tasks=None, current=SimpleNamespace(id=owner), registry=registry)
    mock_supplied_rule_interpretation(monkeypatch)
    compile_rule = AsyncMock(wraps=rubric_drafts.compile_parts_draft)
    monkeypatch.setattr(rubric_drafts, "compile_parts_draft", compile_rule)
    retried = _claim(owner, retry["job_id"], "rubric-retry")
    await api.run_durable_question_preparation(retried)
    done, rows = persisted(owner, retried)
    assert done.status == "done", done.error_code
    assert not done.checkpoint["incremental_incomplete_units"]
    extract.assert_awaited_once()
    compile_rule.assert_awaited_once()
    assert saved_draft(rows["q1"]) and rows["q1"]["criterion"] == teacher["criterion"]
    assert not any(i.get("details", {}).get("rubric_review_draft") for i in rows["q1"]["preparation_issues"])
    view = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert view["rubric_confirmation_required"] is False
    assert not any(i["severity"] == "blocking" and i["status"] == "open" for i in view["preparation_issues"])


def test_first_manual_rubric_must_compile_before_confirmation_and_next_stage(monkeypatch):
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    args = _persist_method_question()
    def record():
        return assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    before = record()
    valid = saved_draft(problem_from_question(before))
    presentation = dict(before.source["presentation"])
    presentation.pop("rubric_draft")
    presentation["review_status"] = "needs_review"
    assignment_repository.update_question(args["task_id"], teacher_id=args["owner_id"], q_id="q1",
        expected_version=before.version, criterion="", source={"presentation": presentation})
    def unavailable(*_):
        raise ValidationError("Model unavailable", code="provider_auth_failed")
    monkeypatch.setattr(task_facade, "_compile_review_edit", unavailable)
    # A draft save does not depend on model availability.
    task_facade.update_problem(**args, patch={"criterion": before.criterion})
    draft = record()
    assert draft.criterion == before.criterion
    view = task_facade.get_task(task_id=args["task_id"], owner_id=args["owner_id"])
    assert view["problem_data"]["q1"]["rubric_confirmation_required"] is True
    assert task_facade._question_review_blockers([draft]) == ["question_review_blocked"]
    with pytest.raises(ValidationError) as failure:
        task_facade.update_problem(**args, patch={"review_status": "confirmed"})
    assert failure.value.code == "provider_auth_failed"
    assert record().version == draft.version
    assert record().source == draft.source
    calls = []
    def compile_current(_owner, edit, _run):
        calls.append(edit)
        assert edit.problem.criterion == before.criterion
        return valid
    monkeypatch.setattr(task_facade, "_compile_review_edit", compile_current)
    result = task_facade.update_problem(**args, patch={"review_status": "confirmed"})
    assert result["problem"]["review_status"] == "confirmed" and len(calls) == 1
    assert result["problem"]["rubric_confirmation_required"] is False
    assert saved_draft(problem_from_question(record())) is not None
    assert task_facade._question_review_blockers([record()]) == []
    task_facade.update_problem(**args, patch={"review_status": "confirmed"})
    assert len(calls) == 1  # A second confirmation reuses the current valid data.


@pytest.mark.asyncio
async def test_conflicting_budgets_retain_extracted_values_after_bounded_repairs():
    row = make_row("列式3分、计算结果8分")  # Teacher allocations exceed 10.
    raw = proposed(points=(3, 8))
    provider = Provider([json.dumps({"part_rubric": raw})] * 3)
    with pytest.raises(ValidationError) as failure:
        await compile_parts_draft(row, provider)
    assert failure.value.code == "invalid_structured_rubric"
    extracted = failure.value.details["rubric_review_draft"]
    assert extracted["input_fingerprint"] == draft_fingerprint(row)
    fields = {field["path"]: field["value"] for field in extracted["fields"]}
    assert fields["part_rubric.parts[0].steps[0].total_pts"] == 3
    assert fields["part_rubric.parts[0].steps[1].total_pts"] == 8
    assert len(provider.calls) <= 3 and saved_draft(row) is None


@pytest.mark.parametrize("repair_kind", ["math", "programming"])
@pytest.mark.asyncio
async def test_conflict_does_not_stop_later_questions_and_survives_manual_handoff(monkeypatch, repair_kind):
    from backend.tests.test_incremental_question_preparation import setup, inc, api, persisted
    from backend.db import workflow_repository, assignment_repository
    from backend.services.task_execution import stop_task_run
    from backend.services import task_facade, rubric_drafts
    owner, task, operation, pipeline = await setup(monkeypatch)
    rows = [make_row("列式3分、计算结果8分"), make_row("列式3分、计算结果7分")]
    rows[1].update(q_id="q2", number="2")
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows(rows)))
    await pipeline.run()
    pipeline.concurrency = 1  # The conflicting first item must not stop this queue.
    seen = []
    original = rubric_drafts.compile_parts_draft
    async def compile_rule(row, *_args, **kwargs):
        seen.append(row["number"])
        raw = proposed(points=(3, 8) if row["number"] == "1" else (3, 7))
        return await original(row, Provider([json.dumps({"part_rubric": raw})] * 3), **kwargs)
    monkeypatch.setattr(rubric_drafts, "compile_parts_draft", compile_rule)
    with pytest.raises(ValidationError):
        await pipeline.freeze_supplied_rules(pipeline.project())
    assert seen == ["1", "2"]
    _, stored = persisted(owner, operation)
    assert saved_draft(stored["q2"]) is not None
    assert saved_draft(stored["q1"]) is None
    issue = next(i for i in stored["q1"]["preparation_issues"] if i.get("details", {}).get("rubric_review_draft"))
    assert issue["severity"] == "blocking" and issue["status"] == "open"
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                  expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert result["status"] == "ready_for_review"
    before = assignment_repository.list_questions(task, teacher_id=owner)[0]
    args = dict(task_id=task, owner_id=owner, q_id="q1")
    with pytest.raises(ValidationError, match="before confirming"):
        task_facade.update_problem(**args, patch={"review_status": "confirmed", "preparation_issues": []})
    if repair_kind == "programming":
        task_facade.update_problem(**args, patch={"type": "编程题"})
        task_facade.update_problem(**args, patch={"stem": "输入一个整数，输出它的平方。"})
        task_facade.update_problem(**args, patch={"reference_answer": "读取输入并输出其平方。"})
        task_facade.update_problem(**args, patch={"criterion": "输出正确得满分。",
            "programming_scoring": {"test_points": 0, "input_rules": ""}})
        result = task_facade.update_problem(**args, patch={"review_status": "confirmed"})
        assert result["problem"]["review_status"] == "confirmed"
        from backend.services.math_grading_context import problem_from_question
        sibling = assignment_repository.list_questions(task, teacher_id=owner)[1]
        assert saved_draft(problem_from_question(sibling)) == saved_draft(stored["q2"])
        return
    pending = task_facade._save_problem_edit(**args, patch={"criterion": rows[1]["criterion"]}, expected_revision=None)
    assert isinstance(pending, task_facade._PendingRubricEdit)
    # Editing the text alone cannot clear the conflict. A verified ledger is required.
    after = assignment_repository.list_questions(task, teacher_id=owner)[0]
    assert (after.criterion, after.version, after.source) == (before.criterion, before.version, before.source)
    fixed = await original(pending.problem, Provider([json.dumps({"part_rubric": proposed()})]))
    result = task_facade._save_problem_edit(**args, patch={"criterion": rows[1]["criterion"]},
        expected_revision=pending.revision, question_version=pending.question_version, compiled=fixed)
    assert result["problem"]["criterion"] == rows[1]["criterion"]
    assert not any(i["status"] == "open" and i["severity"] == "blocking"
                   for i in result["problem"]["preparation_issues"])


@pytest.mark.parametrize("status,message,code", [
    (401, "Invalid API key", "provider_auth_failed"),
    (403, "Permission denied", "provider_auth_failed"),
    (429, "insufficient_quota", "provider_quota_exceeded"),
    (429, "daily quota exhausted", "provider_daily_quota_exceeded"),
])
def test_review_compilation_preserves_actionable_provider_failure(monkeypatch, status, message, code):
    from backend.services import task_facade, rubric_drafts
    from backend.tools.structured_llm import PermanentLLMError
    class ProviderFailure(Exception):
        status_code = status
    async def fail(*_args, **_kwargs):
        try:
            raise ProviderFailure(message)
        except ProviderFailure as exc:
            raise PermanentLLMError("Provider rejected request") from exc
    monkeypatch.setattr(rubric_drafts, "compile_parts_draft", fail)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: object())
    monkeypatch.setattr(task_facade, "resolve_stage_provider_route", lambda **_: SimpleNamespace(provider=object()))
    pending = task_facade._PendingRubricEdit(make_row("列式3分、计算结果7分"), None, 1, 1, "fixture", "zh", None)
    with pytest.raises(ValidationError) as caught:
        task_facade._compile_review_edit("fixture", pending, None)
    assert caught.value.code == code
    assert message not in str(caught.value)


@pytest.mark.parametrize("field", ["stem", "criterion"])
def test_clearing_a_required_field_cannot_resolve_pending_rules(field):
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.rubric_review import review_issue
    args = _persist_method_question()
    question = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    presentation = dict(question.source["presentation"])
    presentation.pop("rubric_draft")
    presentation["preparation_issues"] = [review_issue("q1", review_extraction({"part_rubric": proposed()}, make_row("评分标准")))]
    assignment_repository.update_question(args["task_id"], teacher_id=args["owner_id"], q_id="q1",
        expected_version=question.version, source={"presentation": presentation})
    result = task_facade.update_problem(**args, patch={field: ""})
    assert any(issue["status"] == "open" and issue.get("details", {}).get("reason") == "invalid_structured_rubric"
               for issue in result["problem"]["preparation_issues"])


@pytest.mark.parametrize("kind", ["编程题", "programming"])
def test_changing_grading_engine_retires_only_math_review(monkeypatch, kind):
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.rubric_review import review_issue
    args = _persist_method_question()
    before = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    presentation = dict(before.source["presentation"])
    presentation.pop("rubric_draft")
    presentation["preparation_issues"] = [review_issue("q1", review_extraction({"part_rubric": proposed()}, make_row("待核对标准")))]
    assignment_repository.update_question(args["task_id"], teacher_id=args["owner_id"], q_id="q1",
        expected_version=before.version, source={"presentation": presentation})
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *_: pytest.fail("A programming edit must not compile a math rubric"))
    updated = task_facade.update_problem(**args, patch={"type": kind, "stem": "输入一个整数，输出其平方。",
        "reference_answer": "读取整数并输出平方。", "criterion": "程序输出正确得满分。",
        "programming_scoring": {"test_points": 0, "input_rules": ""}})
    assert not any(i["status"] == "open" and i["severity"] == "blocking" for i in updated["problem"]["preparation_issues"])
    confirmed = task_facade.update_problem(**args, patch={"review_status": "confirmed"})
    assert confirmed["problem"]["review_status"] == "confirmed"


def test_new_programming_checks_survive_retiring_a_math_error():
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.rubric_review import review_issue
    args = _persist_method_question()
    before = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    presentation = dict(before.source["presentation"])
    presentation.pop("rubric_draft")
    presentation["preparation_issues"] = [review_issue("q1", review_extraction({"part_rubric": proposed()}, make_row("待核对标准")))]
    assignment_repository.update_question(args["task_id"], teacher_id=args["owner_id"], q_id="q1",
        expected_version=before.version, source={"presentation": presentation})
    with pytest.raises(ValidationError) as failure:
        task_facade.update_problem(**args, patch={"type": "编程题", "stem": "输入一个整数，输出其平方。",
            "criterion": "程序输出正确得满分。", "programming_scoring": {"test_points": 0, "input_rules": ""},
            "test_cases": [
                {"input": "2", "expected_output": "4"}, {"input": "2", "expected_output": "5"}]})
    assert failure.value.code == "invalid_programming_test_cases"
    after = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    assert after.type == before.type


@pytest.mark.asyncio
async def test_recovered_rubric_uses_normal_save_validation_then_confirmation_reuses_frozen_rules(monkeypatch):
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.services.rubric_review import review_issue
    args = _persist_method_question()
    def record():
        return assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    before = record()
    previous = saved_draft(problem_from_question(before))
    raw = previous.plan.part_rubric.model_dump(mode="json")
    presentation = dict(before.source["presentation"])
    presentation.pop("rubric_draft")
    presentation["review_status"] = "needs_review"
    source = {**problem_from_question(before).model_dump(mode="json"), "criterion": ""}
    presentation["preparation_issues"] = [review_issue("q1", review_extraction({"part_rubric": raw}, source))]
    assignment_repository.update_question(args["task_id"], teacher_id=args["owner_id"], q_id="q1",
        expected_version=before.version, criterion="", source={"presentation": presentation})
    pending = task_facade._save_problem_edit(**args, patch={"criterion": before.criterion}, expected_revision=None)
    assert isinstance(pending, task_facade._PendingRubricEdit)
    assert record().criterion == "" and saved_draft(problem_from_question(record())) is None
    compiled = await compile_parts_draft(pending.problem, Provider([json.dumps({"part_rubric": raw})]))
    saved = task_facade._save_problem_edit(**args, patch={"criterion": before.criterion},
        expected_revision=pending.revision, question_version=pending.question_version, compiled=compiled)
    assert saved["problem"]["criterion"] == before.criterion
    assert not any(i["status"] == "open" and i["severity"] == "blocking" for i in saved["problem"]["preparation_issues"])
    frozen = saved_draft(problem_from_question(record()))
    assert frozen.plan.part_rubric.parts == compiled.plan.part_rubric.parts
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *_: pytest.fail("Confirmation must reuse the saved rules"))
    confirmed = task_facade.update_problem(**args, patch={"review_status": "confirmed"})
    assert confirmed["problem"]["review_status"] == "confirmed"
    assert task_facade._question_review_blockers([record()]) == []
    assert saved_draft(problem_from_question(record())).plan.part_rubric.parts == frozen.plan.part_rubric.parts
