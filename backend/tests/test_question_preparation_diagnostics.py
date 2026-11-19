from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import ValidationError
from backend.progress.recognition_activity import public_progress
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.tests.test_incremental_question_preparation import setup, row, generated, persisted
from backend.tests.test_question_preparation_recovery import _claim, _RecoveryRegistry


@pytest.mark.asyncio
async def test_three_questions_first_failed_rubric_then_retry_uses_saved_fields(monkeypatch):
    owner, task, operation, pipeline = await setup(monkeypatch)
    disputed = row("1.10")
    # Actual stored pre-fix issue shape, without any source/student content.
    disputed["preparation_issues"] = [dict(issue_id="formula", q_id="q2", field="stem",
        code="recognition_partial", severity="blocking", status="open",
        details=dict(incomplete=True, reason="conflicting_printed_formula"))]
    extract = AsyncMock(return_value=inc._rows([row("1.8"), disputed, row("1.12")]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    calls = []
    retrying = False

    async def complete(data, targets, *args, **kwargs):
        number = data["q1"]["number"]
        calls.append((number, [target["target"] for target in targets]))
        if not retrying and number == "1.8" and [target["target"] for target in targets] == ["criterion"]:
            # A provider protocol error still uses the explicit retry view.
            # Exhausted but usable per-field drafts enter question review directly.
            raise ValidationError("The provider returned an invalid response envelope.", code="provider_response_invalid")
        return [generated(t["target"], "Valid saved material.") for t in targets
                if retrying or number != "1.8" or t["target"] != "criterion"]

    monkeypatch.setattr(inc, "generate_missing_question_materials", complete)
    with pytest.raises(ValidationError) as failure:
        await pipeline.run()
    assert failure.value.code == "provider_response_invalid"
    saved, rows = persisted(owner, operation)
    assert saved.checkpoint["preparation_counts"] == dict(questions=3, stems=3, reference_answers=3, rubrics=2)
    progress = public_progress((await pipeline.reporter.snapshot()).model_dump(mode="json"), terminal=True)
    assert progress["total_questions"] == 3
    assert len(progress["completed_question_ids"]) == 2
    review = {r["number"]: r["issues"] for r in progress["preparation_review"]}
    assert [i["field"] for i in review["1.8"]] == ["rubric"]
    assert "1.10" not in review  # Advisory formula review stays on the review page.

    task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
        failure.value.code, expected_lease_token=operation.lease_token, operation_progress=progress)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    result = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=None, current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    next_op = _claim(owner, result["job_id"], "diagnostic-retry")
    inputs = api._rehydrate_question_preparation_inputs(next_op)
    retrying = True
    calls.clear()
    restored = inc.IncrementalQuestionPreparation(operation=next_op, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(next_op.operation_id), score_policy=inputs["score_policy"],
        read_source=pipeline.read_source)
    await restored.run()
    assert calls == [("1.8", ["criterion"])]
    extract.assert_awaited_once()
    progress = await restored.reporter.snapshot()
    assert progress.total_questions == len(progress.completed_question_ids) == 3
    assert not progress.failed_question_ids
    assert progress.preparation_review_summary == dict(questions=1, blocking=0, advisory=1)


def test_public_progress_preserves_field_diagnostics_without_model_content():
    snapshot = dict(preparation_review=[dict(q_id="q1", number="1.8", secret="private", issues=[dict(
        issue_id="rule", field="rubric", code="generation_failed", status="open", severity="blocking",
        details=dict(reason="invalid_structured_rubric", invalid_fields=["part_rubric.parts[0].alternative", "secret"],
            rubric_review_draft={"fields": [{"value": "private answer"}]}, raw_response="private"))])])
    result = public_progress(snapshot, terminal=True)["preparation_review"]
    assert result[0]["issues"][0]["details"] == dict(reason="invalid_structured_rubric",
        invalid_fields=["part_rubric.parts[0].alternative"])
    assert "private" not in str(result)


def test_bounded_progress_excludes_advisory_issues_before_twenty_question_limit():
    from backend.services.question_preparation_diagnostics import bounded_preparation_review
    yellow = dict(severity="warning", code="recognition_needs_review", field="stem", status="open")
    red = dict(severity="blocking", code="generation_failed", field="rubric", status="open")
    rows = bounded_preparation_review([
        *[dict(q_id=f"yellow-{i}", issues=[yellow]) for i in range(25)],
        *[dict(q_id=f"red-{i}", issues=[red, yellow]) for i in range(25)],
    ])
    assert len(rows) == 20 and all(row["q_id"].startswith("red-") for row in rows)
    assert all(row["issues"] == [red] for row in rows)


def test_formula_warning_is_not_a_backend_progress_blocker():
    issue = dict(code="recognition_partial", field="stem", status="open", severity="blocking",
                 details=dict(incomplete=True, reason="conflicting_printed_formula"))
    effective = task_facade._effective_question_issues({"preparation_issues": [issue]}, "概念题")
    assert effective[0]["severity"] == "warning"
    assert not task_facade.question_issue_blocks_progress(effective[0], {})


def test_formula_warning_cannot_hide_a_genuinely_missing_page():
    from backend.services.question_preparation_diagnostics import normalize_formula_issue
    issue = dict(code="recognition_needs_review", field="stem", status="open", severity="warning",
        details=dict(reason="conflicting_printed_formula", coverage={"failed_pages": [2]}))
    assert normalize_formula_issue(issue)["severity"] == "blocking"
    effective = task_facade._effective_question_issues({"preparation_issues": [issue]}, "概念题")
    assert effective[0]["severity"] == "blocking"


@pytest.mark.asyncio
async def test_upstream_failure_is_bound_to_the_question_and_persisted_without_raw_body(monkeypatch):
    from backend.agents.question_preparation_agent import _SubmissionSafeProvider
    from backend.tests.test_provider_failure_diagnostics import gemini_unavailable
    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row("1.8")])))
    guarded = _SubmissionSafeProvider(SimpleNamespace(ainvoke=AsyncMock(side_effect=gemini_unavailable())))
    async def unavailable(*args, **kwargs):
        await guarded.ainvoke([])
    monkeypatch.setattr(inc, "generate_missing_question_materials", unavailable)
    with pytest.raises(RuntimeError):
        await pipeline.run()
    stored, _ = persisted(owner, operation)
    issues = public_progress(stored.checkpoint, terminal=True)["preparation_review"][0]["issues"]
    cause = [item for item in issues if item["details"].get("provider_error_code")]
    assert len(cause) == 1 and cause[0]["field"] == "rubric"
    assert cause[0]["details"]["provider_http_status"] == 503
    assert "PRIVATE" not in str(issues)


@pytest.mark.parametrize("field,code,severity,details,required", [
    ("answer", "recognition_partial", "blocking", {"incomplete": True}, False),
    ("rubric", "source_conflict", "warning", {}, True),
    ("programming_tests", "source_conflict", "warning", {"reason": "test_case_output_conflict"}, True),
    ("stem", "recognition_partial", "warning", {"coverage": {"failed_pages": [2]}}, True),
    ("programming_tests", "generation_failed", "blocking", {}, False),
])
def test_progress_classifies_issues_with_the_existing_review_policy(field, code, severity, details, required):
    from backend.services.question_preparation_diagnostics import preparation_review, bounded_preparation_review
    issue = dict(issue_id="i1", field=field, code=code, severity=severity, details=details, status="open")
    presentation = dict(type="programming", preparation_issues=[issue])
    gate = any(task_facade.question_issue_blocks_progress(i, presentation)
               for i in task_facade._effective_question_issues(presentation, presentation["type"]))
    assert gate is required
    review = preparation_review({"q1": {"number": "1.2.3", **presentation}})
    assert any(i["severity"] == "blocking" for r in review for i in r["issues"]) is required
    public = public_progress({"preparation_review": bounded_preparation_review(review)}, terminal=True)
    assert bool(public["preparation_review"]) is required


def test_optional_answers_do_not_displace_conflicts_from_the_twenty_question_preview():
    from backend.services.question_preparation_diagnostics import preparation_review, bounded_preparation_review
    packages = {
        **{f"optional-{i}": dict(number=str(i), preparation_issues=[dict(field="answer", code="recognition_partial", severity="blocking", status="open", details={"incomplete": True})]) for i in range(25)},
        **{f"conflict-{i}": dict(number=str(i), preparation_issues=[dict(field="rubric", code="source_conflict", severity="warning", status="open")]) for i in range(25)},
    }
    review = preparation_review(packages)
    assert sum(any(i["severity"] == "blocking" for i in r["issues"]) for r in review) == 25
    preview = bounded_preparation_review(review)
    assert len(preview) == 20 and all(r["q_id"].startswith("conflict-") for r in preview)


@pytest.mark.parametrize("details", ["PRIVATE_BAD_DETAILS", {"required_fields": None}, {"raw_response": "PRIVATE"}])
def test_public_review_normalizes_only_sanitized_persisted_issue_details(details):
    from backend.services.question_preparation_diagnostics import public_preparation_review
    result = public_preparation_review([dict(q_id="q1", number="1", issues=[dict(
        issue_id="conflict", field="rubric", code="source_conflict", severity="warning", status="open", details=details)])])
    assert result[0]["issues"][0]["severity"] == "blocking"
    assert "PRIVATE" not in str(result)
def test_public_question_review_retains_field_specific_reason_after_readback():
    from backend.services.question_preparation_diagnostics import preparation_review, public_preparation_review
    issue = dict(issue_id="q1-tests", q_id="q1", field="rubric", code="generation_failed",
                 status="open", severity="blocking",
                 details={"reason": "programming_test_award_unset", "message": "PRIVATE_DETAIL"})
    parsed = preparation_review({"q1": {"number": "1", "preparation_issues": [
        issue, {**issue, "issue_id": "q1-parser", "field": "programming_tests",
                "code": "material_parser_provider_required", "severity": "warning", "details": {}},
    ]}})
    rows = public_preparation_review(parsed)
    assert rows == parsed
    assert rows[0]["issues"][0]["details"] == {"reason": "programming_test_award_unset"}
