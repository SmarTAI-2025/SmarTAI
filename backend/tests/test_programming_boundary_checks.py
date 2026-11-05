"""Extra observations must preserve teacher credit, source provenance and release policy."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.agents.multi_expert import _project_programming_result
from backend.domain.grading_review import programming_review_reason
from backend.models import Correction, ProblemInfo, StudentAnswerInfo, TestCase as Case
from backend.services.programming_runs import programming_run
from backend.skills import programming
from backend.tests.test_programming_scoring_contract import award, fixed_provider, response

REFERENCE = "def double(n): return 2*n"
SOURCE = "def double(n): return 0 if n == -1 else 2*n"
EXTRA = {"args": [-1], "expected": "-2"}


def setup(*, independent=True, mixed=False):
    cases = [Case(io_mode="function", function_name="double", function_args=[3], expected_return="6")]
    if mixed:
        cases.append(Case(io_mode="function", function_name="double", function_args=[-1],
                          expected_return="-2", source="llm_generated"))
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="实现 double(n)，返回整数的两倍。",
                          max_score=10, criterion="教师用例全部通过得10分。", solution_code=REFERENCE, test_cases=cases)
    if not independent:
        problem._math_rubric_source = {"presentation":{"ai_completion_provenance":{"solution_code":{"source":"ai"}}}}
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=SOURCE)
    return problem, answer


@pytest.mark.asyncio
@pytest.mark.parametrize("score,required", [(10, False), (8, True)])
@pytest.mark.parametrize("mixed", [False, True])
async def test_matched_reference_boundary_is_visible_without_changing_teacher_credit(monkeypatch, score, required, mixed):
    problem, answer = setup(mixed=mixed)
    execute = AsyncMock(wraps=programming.run_sandbox)
    monkeypatch.setattr(programming, "run_sandbox", execute)
    reply = response({**award(score), "boundary_case": EXTRA})
    provider = fixed_provider(reply, reply)
    with programming_run(None, None):
        outcomes = await asyncio.gather(*(programming.ProgrammingSkill(provider, language="zh").grade(problem, answer, student_id="s") for _ in range(2)))
    assert provider.ainvoke.await_count == 2
    assert execute.await_count == (2 if mixed else 4)
    for vote in outcomes:
        note = vote.programming_evidence.boundary_note
        assert note and "-1" in note and "-2" in note and note in vote.comment
        assert vote.score == score and vote.programming_evidence.teacher_tests.passed == 1
        correction = Correction(number="1", q_id="q1", type="编程题", confidence=.95, score=score, max_score=10, comment=vote.comment,
                                steps=vote.steps, expert_results=[vote])
        combined = _project_programming_result(correction, "zh")
        assert combined.comment.count(note) == 1 and "programming_extra_boundary" in combined.review_reasons
        assert bool(programming_review_reason(combined.review_reasons, [vote.model_dump()])) == required
        assert combined.requires_human_review is required


@pytest.mark.asyncio
@pytest.mark.parametrize("proposed,independent,count", [
    ({"args":[-1],"expected":"123"}, True, 3),   # wrong model expectation
    ({"args":[3],"expected":"123"}, True, 2),    # existing teacher input with a different expectation
    ({"stdin":"-1","expected":"-2"}, True, 2),  # different execution interface
    ({"args":[-1],"expected":"-2","code":"print(9)"}, True, 2),
    (EXTRA, False, 1),                          # another model's reference is not independent
])
async def test_unverified_or_duplicate_extra_input_cannot_accuse_the_student(monkeypatch, proposed, independent, count):
    problem, answer = setup(independent=independent)
    execute = AsyncMock(wraps=programming.run_sandbox)
    monkeypatch.setattr(programming, "run_sandbox", execute)
    with programming_run(None, None):
        vote = await programming.ProgrammingSkill(fixed_provider(response({**award(10),"boundary_case":proposed}))).grade(problem, answer, student_id="s")
    assert execute.await_count == count
    assert vote.score == 10 and not vote.error_kind and not vote.programming_evidence.boundary_note


@pytest.mark.asyncio
async def test_advisory_note_reaches_teacher_and_released_student_result(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.config import settings
    from backend.db import assignment_repository, course_repository, grading_repository, submission_repository
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.services import grading_runs, task_facade

    monkeypatch.setattr(settings, "multi_sample_n", 1)
    suffix = uuid4().hex
    teacher, student = f"boundary-owner-{suffix}", f"boundary-student-{suffix}"
    with session_scope() as session:
        for uid, role in [(teacher, "teacher"), (student, "student")]:
            session.add(UserRecord(id=uid, username=uid, password_hash="h", role=role, is_active=True))
    task = task_facade.create_task(owner_id=teacher, name="Boundary check", semester_id=None,
                                  course_id=None, idempotency_key=suffix)
    assignment = assignment_repository.get_assignment(task["task_id"], actor_id=teacher)
    course_repository.enroll(course_id=assignment.course_id, student_id=student)
    question, _ = setup()
    q = assignment_repository.add_question(assignment.id, teacher_id=teacher, q_id="q1", order_index=0,
            type=question.type, stem=question.stem, criterion=question.criterion,
            reference_answer=f"```python\n{REFERENCE}\n```", test_cases=[case.model_dump() for case in question.test_cases])
    assignment_repository.publish(assignment_id=assignment.id, teacher_id=teacher, expected_version=1)
    submission = submission_repository.create_submission(assignment_id=assignment.id, student_id=student)
    submission_repository.add_revision(submission_id=submission.id, student_id=student, source="online",
        answers=[{"question_id":q.id,"q_id":"q1","type":q.type,"content":SOURCE}])
    provider = fixed_provider(response({**award(10), "boundary_case": EXTRA}))
    registry = SimpleNamespace(count=lambda:1, list_available=lambda:[provider], pick_default=lambda:provider)
    run = grading_runs.create_run(teacher_id=teacher, assignment_id=assignment.id)
    await grading_runs.process_run(run_id=run.id, worker_id="boundary-worker", registry=registry)
    row, = grading_repository.list_results_for_run(run.id)
    fact = row.ai_expert_results[0]["programming_evidence"]
    assert row.ai_score == 10 and fact["score_status"] == "final"
    assert "programming_extra_boundary" in row.initial_review_reasons
    assert fact["boundary_note"] in task_facade._serialize_correction(row)["comment"]
    finalization = task_facade.finalization(task_id=assignment.id, owner_id=teacher)
    assert finalization["ready_for_confirmation"] and finalization["required_review_count"] == 0
    grading_runs.release(teacher_id=teacher, run_id=run.id)
    published, = grading_runs.student_results(student_id=student, assignment_id=assignment.id)
    assert published.effective_score == 10 and fact["boundary_note"] in published.effective_comment


@pytest.mark.asyncio
async def test_generated_argument_shape_is_repaired_before_the_class_suite_is_frozen():
    problem = ProblemInfo(q_id="list", number="1", type="编程题", stem="实现 def count_items(values): 返回列表长度。",
                          criterion="结果正确。", max_score=10)
    invalid = [{"function_name":"count_items", "function_args":[2,5], "expected_return":"2"}]
    valid = [{"function_name":"count_items", "function_args":[[2,5]], "expected_return":"2"}]
    provider = fixed_provider(response(invalid), response(valid))
    cases, error = await programming._generate_test_cases(provider, problem)
    assert error is None and provider.ainvoke.await_count == 2
    assert len(cases) == 1 and cases[0].function_args == [[2,5]] and cases[0].expected_return == "2"
    assert cases[0].source == "llm_generated"


@pytest.mark.asyncio
@pytest.mark.parametrize("stem,proposal,reference,student,expected", [
    ("实现 def keep(s): 返回原字符串。", {"args": [" x! "]}, "def keep(s): return s", "def keep(s): return s.strip()", repr(" x! ")),
    ("Read stdin and print it exactly.", {"input": " x! \n"}, "import sys\nsys.stdout.write(sys.stdin.read())", "print(input().strip())", " x! \n"),
])
async def test_fresh_generated_inputs_obtain_expected_values_from_teacher_reference_once(monkeypatch, stem, proposal, reference, student, expected):
    from backend.services.programming_runs import evidence_for_answer

    problem = ProblemInfo(q_id="q", number="1", type="编程题", stem=stem, criterion="Preserve whitespace.",
                          max_score=10, solution_code=reference)
    provider = fixed_provider(response({"inputs":[proposal]}))
    execute = AsyncMock(wraps=programming.run_sandbox)
    with programming_run(None, None):
        for sid, code in [("a", reference), ("b", student)]:
            answer = StudentAnswerInfo(q_id="q", number="1", type="编程题", content=code)
            ev = await evidence_for_answer(problem=problem,answer=answer,student_id=sid,task_id="t",provider=provider,
                reporter=None,generate=programming._generate_test_cases,execute=execute)
            assert ev.status == "executed", ev.reason
            assert ev.cases[0].source == "llm_generated"
            case = ev.cases[0]
            assert (case.expected_return if case.function_name else case.expected_output) == expected
            if case.function_name:
                assert bool(ev.report.passed_count) == (sid == "a")
    assert provider.ainvoke.await_count == 1 and execute.await_count == 3
