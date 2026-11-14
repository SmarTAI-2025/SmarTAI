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


@pytest.mark.asyncio
async def test_conflict_does_not_stop_later_questions_and_survives_manual_handoff(monkeypatch):
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
