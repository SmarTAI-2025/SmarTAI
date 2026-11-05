"""A partial award is not a discrepancy. Verify its cited grounds at most once."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.multi_expert import run_multi_expert
from backend.domain.grading_review import programming_review_reason, teacher_resolved_programming_score
from backend.models import TestCase
from backend.services import programming_runs as runs
from backend.services.programming_partial_review import REVIEW_REASON, review_inputs, review_partial_score
from backend.tests.test_programming_scoring_contract import fixed_provider, response
from backend.tests.test_programming_skill import _make_answer, _make_problem
from backend.tests.test_task_grading_retry import case  # noqa: F401


def award(first=None, second=None):
    steps = [
        dict(step_no=1, max_points=5, score=3, is_correct=False, desc="The first criterion received a deduction."),
        dict(step_no=2, max_points=5, score=3, is_correct=False, desc="The second criterion received a deduction."),
    ]
    for step, basis in zip(steps, (first, second)):
        if basis is not None:
            step["deduction_basis"] = basis
    return response(dict(max_score=10, steps=steps, confidence=.9))


def materials(rubric="Assess input parsing and output formatting independently."):
    cases = [TestCase(input=str(n), expected_output=str(n if n < 4 else 0)) for n in range(5)]
    problem = _make_problem(stem="Read an integer and print its value.", test_cases=cases)
    problem.criterion = rubric
    return problem, _make_answer("print(input())")


def registry(*providers):
    return SimpleNamespace(list_available=lambda: providers, pick_default=lambda: providers[0])


async def grade(reg, problem, answer, language="en"):
    return await run_multi_expert(problem=problem, answer=answer, student_id="s", registry=reg,
                                  language=language, aggregation_method="weighted_average")


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [
    (None, None),  # 4/5 teacher tests, 6/10 award, no score-rate rule.
    ({"failed_teacher_cases":[5], "rubric_quote":"input parsing"},
     {"failed_teacher_cases":[5], "rubric_quote":"output formatting"}),
    ({"failed_teacher_cases":[5]}, None),
    ({"failed_teacher_cases":["one"]}, None),
    ({"rubric_quote":"input parsing", "source_quote":"print(input())"}, None),
])
async def test_ordinary_partial_or_independent_rubric_item_needs_no_extra_request(first, second):
    problem, answer = materials()
    model = fixed_provider(award(first, second))
    result = await grade(registry(model), problem, answer)
    model.ainvoke.assert_awaited_once()
    facts = result.expert_results[0].programming_evidence
    assert (facts.teacher_tests.passed, facts.teacher_tests.total, result.score) == (4, 5, 6)
    assert facts.partial_check is None and REVIEW_REASON not in result.review_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,first,second,rule,confirmed", [
    ("passed_case_cited_as_failed", {"failed_teacher_cases":[1]}, None, "Accuracy.", True),
    ("passed_case_cited_as_failed", {"failed_teacher_cases":[1,99]}, None, "Accuracy.", True),
    ("shared_failure", {"failed_teacher_cases":[5], "rubric_quote":"output format"},
     {"failed_teacher_cases":[5], "rubric_quote":"format"}, "Check the output format.", False),
    ("source_quote_missing", {"source_quote":"return abs(value)"}, None, "Accuracy.", True),
    ("rubric_quote_missing", {"rubric_quote":"Recursion is mandatory."}, None, "Accuracy.", True),
    ("shared_failure", {"failed_teacher_cases":[5]}, {"failed_teacher_cases":[5]}, "Each criterion may independently use the same observed error.", False),
    ("per_case_deduction_exceeded", {"failed_teacher_cases":[5]}, {"failed_teacher_cases":[5]},
     "Deduct 2 points once per failed teacher test case.", True),
])
async def test_confirmation_requires_independent_witness(kind, first, second, rule, confirmed):
    problem, answer = materials(rule)
    provider = fixed_provider(award(first, second), response({"confirmed":[1]}))
    corr = await grade(registry(provider), problem, answer, language="zh")
    assert provider.ainvoke.await_count == 2 and corr.score == 6
    vote, = corr.expert_results
    evidence = vote.programming_evidence
    assert [s.score for s in vote.steps] == [3, 3] and evidence.teacher_tests.passed == 4
    assert evidence.partial_check["issues"][0]["kind"] == kind
    assert evidence.partial_check["status"] == ("confirmed" if confirmed else "unconfirmed")
    assert (REVIEW_REASON in corr.review_reasons) == confirmed
    assert (programming_review_reason([], [vote.model_dump()]) == REVIEW_REASON) == confirmed
    if confirmed:
        assert "扣分依据待复核" in vote.comment and "扣分依据待复核" in corr.comment
        assert not teacher_resolved_programming_score(6, 6, 10, False)
        assert teacher_resolved_programming_score(6, 6, 10, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,status", [
    (TimeoutError("provider timeout"), "unavailable"),
    (response({"confirmed":[42]}), "unavailable"),
    (response({"confirmed":[]}), "unconfirmed"),
])
async def test_optional_call_failure_or_unresolved_claim_keeps_original_score(reply, status):
    problem, answer = materials()
    provider = fixed_provider(award({"rubric_quote":"unquoted method"}), reply)
    corr = await grade(registry(provider), problem, answer)
    assert provider.ainvoke.await_count == 2
    facts = corr.expert_results[0].programming_evidence
    assert corr.score == 6 and facts.score_status == "final"
    assert facts.teacher_tests.passed == 4 and REVIEW_REASON not in corr.review_reasons
    assert facts.partial_check["status"] == status


@pytest.mark.asyncio
async def test_one_auxiliary_request_across_experts_concurrent_projection_and_failed_only_recovery(monkeypatch):
    events = {}
    cp = SimpleNamespace(run_id="first", worker_id="worker", manifest={}, questions={}, revisions={"s":"rev"})
    monkeypatch.setattr(runs, "current_checkpoints", lambda: cp)
    monkeypatch.setattr(runs.grading_repository, "get_run", lambda rid: SimpleNamespace(id=rid,teacher_id="t",assignment_id="a"))
    monkeypatch.setattr(runs.grading_repository, "list_events", lambda rid, **_: events.get(rid, []))
    def store(run_id, *, message, payload, **_):
        events.setdefault(run_id, []).append({"message":message,"payload":deepcopy(payload)})
    monkeypatch.setattr(runs.grading_repository, "record_event", store)
    problem, answer = materials()
    first = fixed_provider(award(), response({"confirmed":[1]}), name="alpha")
    second = fixed_provider(award({"failed_teacher_cases":[2]}), name="beta")
    reg = registry(first, second)
    with runs.programming_run(reg, None) as state:
        corr = await grade(reg, problem, answer)
        _, votes, _ = review_inputs(corr, "s")
        assert votes == [corr.expert_results[1]] and REVIEW_REASON in corr.review_reasons
        options = dict(problem=problem, answer=answer,student_id="s",language="en",reporter=None)
        await asyncio.gather(*(review_partial_score(corr, state=state, **options) for _ in range(3)))
        assert first.ainvoke.await_count == 2 and second.ainvoke.await_count == 1
    records = [i for i in events["first"] if i["message"] == runs.AUXILIARY]
    assert [r["payload"]["value"]["status"] for r in records] == ["started", "done"]

    for interrupted in (False, True):
        if interrupted:
            events["first"].remove(records[-1])
        cp.run_id = "retry"
        cp.manifest = {"programming_artifacts":runs.artifacts_for_retry("first", {}, "t")}
        never = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("must not redispatch")))
        with runs.programming_run(registry(never), None) as retried:
            copied = corr.model_copy(deep=True)
            copied.review_reasons = []
            check = copied.expert_results[1].programming_evidence.partial_check
            check["status"] = "suspected"
            await review_partial_score(copied, state=retried, **options)
            assert check["status"] == "confirmed" and REVIEW_REASON in copied.review_reasons
            assert check["model_check_status"] == ("interrupted" if interrupted else "completed")
            assert check["issues"][0]["verification_source"] == "teacher_execution"
            never.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_deduction_conflict_blocks_release_until_the_teacher_resolves_it(monkeypatch):
    from uuid import uuid4
    from backend.config import settings
    from backend.db import assignment_repository, course_repository, grading_repository, submission_repository
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain.errors import ResultNotReleasable
    from backend.services import grading_runs, task_facade
    monkeypatch.setattr(settings, "multi_sample_n", 1)
    suffix = uuid4().hex
    teacher, student = "t"+suffix, "s"+suffix
    with session_scope() as session:
        for user, role in [(teacher,"teacher"),(student,"student")]:
            session.add(UserRecord(id=user, username=user, role=role, password_hash="fixed", is_active=True))
    tid = task_facade.create_task(owner_id=teacher, name="Partial", semester_id=None,
                                  course_id=None, idempotency_key=suffix)["task_id"]
    problem, answer = materials()
    assignment = assignment_repository.get_assignment(tid, actor_id=teacher)
    course_repository.enroll(course_id=assignment.course_id, student_id=student)
    q = assignment_repository.add_question(tid, teacher_id=teacher, q_id="q1", order_index=0, type="编程题",
        stem=problem.stem, criterion=problem.criterion, test_cases=[c.model_dump(exclude_unset=True) for c in problem.test_cases])
    assignment_repository.publish(assignment_id=tid, teacher_id=teacher, expected_version=1)
    sub = submission_repository.create_submission(assignment_id=tid, student_id=student)
    submission_repository.add_revision(submission_id=sub.id, student_id=student, source="online",
        answers=[{"question_id":q.id,"q_id":"q1","type":"编程题","content":answer.content}])
    provider = fixed_provider(award({"failed_teacher_cases":[1]}), response({"confirmed":[1]}))
    reg = registry(provider)
    reg.count = lambda: 1
    run = grading_runs.create_run(teacher_id=teacher, assignment_id=tid)
    await grading_runs.process_run(run_id=run.id, worker_id="worker", registry=reg)
    saved, = grading_repository.list_results_for_run(run.id)
    assert saved.ai_score == 6 and saved.result_status == "needs_review" and REVIEW_REASON in saved.initial_review_reasons
    assert saved.ai_expert_results[0]["programming_evidence"]["partial_check"]["status"] == "confirmed"
    grading_repository.add_teacher_review(saved.id, teacher_id=teacher, new_score=6, new_comment="Note only", confirm=False)
    with pytest.raises(ResultNotReleasable, match="programming_review_required"):
        grading_runs.release(teacher_id=teacher, run_id=run.id)
    assert not grading_runs.student_results(student_id=student, assignment_id=tid)
    grading_repository.add_teacher_review(saved.id, teacher_id=teacher, new_score=7, new_comment="Award method credit", confirm=True)
    grading_runs.release(teacher_id=teacher, run_id=run.id)
    released, = grading_runs.student_results(student_id=student, assignment_id=tid)
    assert released.effective_score == 7 and released.ai_score == 6
    assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True], ids=["before_call", "after_call"])
async def test_stop_between_verification_and_question_write_restores_exact_result(case, verified):
    from sqlalchemy import select
    from backend.db import grading_repository
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.services import task_facade
    from backend.tests.test_grading_checkpoints import prepare, start, stop
    with session_scope() as session:
        q = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q1"))
        q.type = "programming"
    run_id = start(case, "stop-after-one-partial-review")["job_id"]
    cp, _, revisions, setup = prepare(case, run_id)
    model_id = setup.setup["selected_provider_ids"][0]
    provider = fixed_provider(award({"failed_teacher_cases":[1]}), response({"confirmed":[1]}), name=model_id)
    problem, answer = materials()
    if verified:
        corr = await cp.run(run_multi_expert(problem=problem, answer=answer, registry=registry(provider),
            student_id=revisions[0][1], multi_sample_n=1, language="zh"))
        assert REVIEW_REASON in corr.review_reasons
    else:
        # The teacher can stop after the vote checkpoint but before the extra
        # request starts. Preserve a proven test contradiction without dispatch.
        from backend.skills.programming import ProgrammingSkill
        vote = await cp.run(ProgrammingSkill(provider, language="zh").grade(problem, answer, student_id=revisions[0][1]))
        cp.save_expert(revisions[0][1], "q1", model_id, 0, vote)
    assert not grading_repository.list_results_for_run(run_id)
    stop(case, run_id)
    result = next(item for item in grading_repository.list_results_for_run(run_id) if item.q_id == "q1")
    assert result.ai_score == 6 and REVIEW_REASON in result.initial_review_reasons
    stored = result.ai_expert_results[0]["programming_evidence"]["partial_check"]
    assert stored["status"] == "confirmed" and stored["issues"][0]["verification_source"] == "teacher_execution"
    assert stored["model_check_status"] == ("completed" if verified else "not_requested")
    assert task_facade._correction_review_blocks_finalization(result)
    assert provider.ainvoke.await_count == (2 if verified else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("description,extra", [
    ("Teacher case 2 failed.", True), ("教师用例 2 未通过。", True),
    ("If case 2 failed, the result would change.", False), ("Did case 2 fail?", False),
    ("Case 2 failed to show that the solution is wrong.", False),
])
async def test_explicit_case_statement_is_checked_without_structured_references(description, extra):
    import json
    problem, answer = materials()
    draft = json.loads(award().content)
    draft["steps"][0]["desc"] = description
    model = fixed_provider(response(draft), response({"confirmed":[1]}))
    corr = await grade(registry(model), problem, answer)
    assert corr.score == 6 and model.ainvoke.await_count == 1 + int(extra)
    assert (REVIEW_REASON in corr.review_reasons) == extra


@pytest.mark.asyncio
async def test_cancelled_verification_is_not_dispatched_again():
    problem, answer = materials()
    started, wait_forever = asyncio.Event(), asyncio.Event()
    initial = award({"source_quote":"never present in submitted source"})
    async def replies(messages, **kwargs):
        if kwargs.get("max_output_tokens") == 160:
            started.set()
            await wait_forever.wait()
        return initial
    model = SimpleNamespace(provider_id="model", ainvoke=AsyncMock(side_effect=replies))
    reg = registry(model)
    with runs.programming_run(reg, None) as state:
        first = asyncio.create_task(grade(reg, problem, answer))
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        [event] = [v for v in state.records.values() if v["kind"] == runs.AUXILIARY]
        assert event["value"]["status"] == "started"
        retried = await grade(reg, problem, answer)
        assert retried.score == 6 and REVIEW_REASON not in retried.review_reasons
        assert retried.expert_results[0].programming_evidence.partial_check["status"] == "unavailable"
        assert sum(c.kwargs.get("max_output_tokens") == 160 for c in model.ainvoke.await_args_list) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("reply,model_status,assessment", [
    (response({"confirmed":[]}), "completed", "not_confirmed"),
    (SimpleNamespace(content='{"confirmed":[1'), "unavailable", "unknown"),
    (TimeoutError(), "unavailable", "unknown"),
])
@pytest.mark.parametrize("cap", [False, True], ids=["actual_teacher_pass", "closed_teacher_cap"])
async def test_local_proof_survives_inconclusive_review_and_prompt_contains_full_witness(reply, model_status, assessment, cap):
    import json
    rule = "每个未通过的教师用例只扣 2 分。" if cap else "Assess output accuracy and method."
    problem, answer = materials(rule)
    basis = {"failed_teacher_cases":[5 if cap else 1], "rubric_quote":rule}
    provider = fixed_provider(award(basis, basis if cap else None), reply)
    corr = await grade(registry(provider), problem, answer)
    assert corr.score == 6 and REVIEW_REASON in corr.review_reasons and provider.ainvoke.await_count == 2
    check = corr.expert_results[0].programming_evidence.partial_check
    issue, = check["issues"]
    assert issue["confirmed"] and check["status"] == "confirmed"
    assert check["model_check_status"] == model_status and issue["model_assessment"] == assessment
    assert issue["verification_source"] == ("teacher_rubric_and_awards" if cap else "teacher_execution")
    prompt = json.loads(provider.ainvoke.await_args.args[0][1].content)
    witness, = prompt["issues"]
    assert prompt["rubric"] == rule and witness["observed_cases"] == [
        {"number": 5 if cap else 1, "source":"teacher", "passed": not cap}]
    assert witness["awards"][0] == {"step":1, "available":5, "earned":3,"deducted":"2.0","claimed_rubric_quote":rule}
    if cap:
        assert witness["awards"][1]["deducted"] == "2.0"
        assert witness["governing_rule"] == rule and Decimal(witness["deducted"]) == 4 and Decimal(witness["allowed"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,accepted", [
    ('```json\n{"confirmed":[1]}\n```', True),
    ('```json\n{"confirmed":[1,]}\n```', False),
    ('{"confirmed":[1', False), ('{"confirmed":[]} {"confirmed":[1]}', False),
    ('{"confirmed":[],"confirmed":[1]}', False),
])
async def test_fenced_json_has_one_local_parse_and_corrupt_semantic_verdict_never_confirms(raw, accepted):
    problem, answer = materials()
    model = fixed_provider(award({"source_quote":"return -99"}), SimpleNamespace(content=raw))
    corr = await grade(registry(model), problem, answer)
    assert model.ainvoke.await_count == 2 and corr.score == 6
    assert (REVIEW_REASON in corr.review_reasons) == accepted
    check = corr.expert_results[0].programming_evidence.partial_check
    assert check["issues"][0]["verification_source"] == ("frozen_source_and_model_review" if accepted else None)
    assert check["model_check_status"] == ("completed" if accepted else "unavailable")
