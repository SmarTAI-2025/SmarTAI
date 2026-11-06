"""Only independent, verified in-domain inputs may become new grading evidence."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services.programming_input_domain import input_domain
from backend.services.programming_runs import evidence_for_answer, programming_run
from backend.skills import programming
from backend.tests.test_programming_scoring_contract import award, fixed_provider, response


def problem(stem, *, criterion="Correctness.", **rest):
    return ProblemInfo(q_id="q1", number="1", type="编程题", stem=stem, criterion=criterion, max_score=10, **rest)


def call(args, kwargs=None):
    return TestCase(io_mode="function", function_args=args, function_kwargs=kwargs)


@pytest.mark.parametrize(
    "declaration,accepted,rejected",
    [
        ("the input n is guaranteed to be a strictly positive integer", 1, 0),
        ("n must be a non-negative integer", 0, -1),
        ("参数 n 为负整数", -1, 0),
        ("输入 n 可以为任意整数", -5, 1.25),
        ("参数 n 可为任意字符串", "", 10),
        ("n 是非正整数", 0, 1),
        ("n is an integer in [0, 4)", 0, 4),
        ("n 是整数，n ∈ (-2, 4]", 4, -2),
        ("0 <= n < 7", 0, 7),
        ("n is between -5 and 2 exclusive", -4, 2),
        ("n 只能取 {'open','closed'}", "closed", "pending"),
        ("n is a list of nonnegative integers", [0, 3], [0, -1]),
        ("n is a string", "23", 23),
        ("n is an integer", 1, True),
    ],
)
def test_literal_contracts(declaration, accepted, rejected):
    p = problem(f"Implement def f(n): {declaration}. Return the answer.")
    assert input_domain(p, call([accepted])) == "allowed"
    assert input_domain(p, call([rejected])) == "outside"


def test_parameter_binding_and_conditional_output():
    p = problem(
        "Implement def f(a,b): a and b are integers. -4 <= a < 10. b < a. If b is positive, return 1; otherwise return 0."
    )
    assert input_domain(p, call([], {"b": -2, "a": -1})) == "allowed"
    assert input_domain(p, call([0, -1])) == "allowed"
    assert input_domain(p, call([-1, 0])) == "outside"
    p = problem("实现 def f(n): 参数 n 为整数。若 n 为正整数则返回 1，否则返回 0。")
    assert input_domain(p, call([-3])) == "allowed"
    p = problem("Implement def f(a,b): a and b are integers. If a is positive, b must be even.")
    assert input_domain(p, call([1, 3])) == "unknown"


@pytest.mark.parametrize("output", [
    "Return n on positive integers.",
    "返回正整数 n 本身。",
    "Return n assuming a positive integer.",
    "If n is positive, return n, assuming that n is odd.",
    "If n is guaranteed to be odd, return n.",
    "Return whether n is positive, assuming that n is odd.",
    "Calculate n for the teacher-defined subset.",
])
def test_output_prefix_cannot_hide_an_unparsed_input_restriction(output):
    p = problem("Implement f(n): n is an integer. " + output)
    assert input_domain(p, call([-1])) == "unknown"


def test_stdin_output_suffix_cannot_hide_an_input_restriction():
    p = problem("Read an integer from stdin and print it on positive integers.")
    assert input_domain(p, TestCase(input="-1\n")) == "unknown"


@pytest.mark.parametrize("output", ["Return n > 0.", "Return whether n is positive.",
    "If n > 0, return 1; otherwise return 0."])
def test_complete_output_expressions_do_not_narrow_input_domain(output):
    p = problem("Implement f(n): n is an integer. " + output)
    assert input_domain(p, call([-1])) == "allowed"


@pytest.mark.parametrize("criterion", [
    "输入保证大于零。", "输入限于大于零的整数。", "只考虑正整数。",
    "参数的十进制表示须为回文。", "The input is drawn from a teacher-defined subset.",
    "If n is positive, return n.",
])
def test_unparsed_rubric_constraints_cannot_be_silently_dropped(criterion):
    p = problem("Implement f(n): n is an integer. Return n.", criterion=criterion)
    assert input_domain(p, call([-1])) != "allowed"


def test_test_outcome_only_award_does_not_restrict_the_declared_input_domain():
    p = problem("Read an integer from stdin and print double its value.",
        criterion="All teacher examples correct earns full credit.")
    assert input_domain(p, TestCase(io_mode="stdin", input="-1\n")) == "allowed"
    p.criterion += " Inputs are limited to an instructor-defined subset."
    assert input_domain(p, TestCase(io_mode="stdin", input="-1\n")) == "unknown"


@pytest.mark.parametrize("location", ["stem", "criterion"])
def test_unambiguous_chinese_bound_is_checked_without_broadening_examples(location):
    p = problem("Implement f(n): n is an integer. Return n.")
    setattr(p, location, getattr(p, location) + " 输入保证大于零。")
    assert input_domain(p, call([1])) == "allowed"
    assert input_domain(p, call([0])) == input_domain(p, call([-1])) == "outside"
    # A positive teacher example does not define an otherwise unknown domain.
    p = problem("Implement f(n): Return n.", test_cases=[
        TestCase(function_name="f", function_args=[1], expected_return="1")])
    assert input_domain(p, call([2])) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("location,criterion", [
    ("criterion", "输入保证大于零。"), ("criterion", "输入限于教师约定的正数集合。"),
    ("stem", "返回正整数 n 本身。"), ("stem", "Return n on positive integers."),
])
@pytest.mark.parametrize("other_teacher_failure", [False, True])
async def test_domain_violation_never_reaches_grading_even_with_real_partial_failure(location, criterion, other_teacher_failure):
    p = problem("Implement f(n): n is an integer. Return n." + (criterion if location == "stem" else ""),
        criterion=criterion if location == "criterion" else "Correctness.",
        solution_code="def f(n): return n", test_cases=[
            TestCase(function_name="f", function_args=[1], expected_return="1"),
            TestCase(function_name="f", function_args=[2], expected_return="2"),
            TestCase(function_name="f", function_args=[-1], expected_return="-1", source="llm_generated"),
        ])
    source = "def f(n): return 99 if n == 2 else abs(n)" if other_teacher_failure else "def f(n): return abs(n)"
    execute = AsyncMock(wraps=programming.run_sandbox)
    with programming_run(None, None):
        run = await evidence_for_answer(problem=p, answer=StudentAnswerInfo(q_id="q1", number="1",
            type="programming", content=source), student_id="s", task_id="t", provider=fixed_provider(),
            reporter=None, generate=None, execute=execute)
    from backend.skills.programming_evidence import describe_run
    facts, examples = describe_run(run, "en", p)
    assert facts.teacher_tests.total == 2 and facts.teacher_tests.passed == (1 if other_teacher_failure else 2)
    assert facts.generated_tests.total == 0
    assert all(c.function_args[0] > 0 for c in run.cases)
    assert all(c.function_args[0] > 0 for invoke in execute.await_args_list for c in invoke.args[1])
    assert all(item.get("input") != "[-1]" for item in examples)


@pytest.mark.asyncio
async def test_new_input_generation_repairs_known_bound_before_reference_or_student_runs():
    p = problem("Implement f(n): n is an integer. Return n.", criterion="输入保证大于零。",
        solution_code="def f(n): return n")
    provider = fixed_provider(response({"single_arguments": [-1, 1]}))
    execute = AsyncMock(wraps=programming.run_sandbox)
    with programming_run(None, None):
        run = await evidence_for_answer(problem=p, answer=StudentAnswerInfo(q_id="q1", number="1",
            type="programming", content="def f(n): return abs(n)"), student_id="s", task_id="t",
            provider=provider, reporter=None, generate=programming._generate_test_cases, execute=execute)
    assert run.status == "executed" and run.report.passed_count == run.report.total_count == 2
    assert all(c.function_args[0] > 0 for invoke in execute.await_args_list for c in invoke.args[1])
    assert provider.ainvoke.await_count == 1


@pytest.mark.parametrize(
    "stem,args",
    [
        ("Implement def f(x): x is a list of integers. Its elements must form a permutation.", [[0, 0]]),
        ("Implement def f(x): x is an integer. Its decimal representation is palindromic.", [12]),
        ("Implement def f(s): Return the length of the string.", [1]),
        ("实现 def f(a,b): 返回字符串长度。", ["x", "y"]),
    ],
)
def test_unproved_or_complex_domains_do_not_acquire_an_input_type_from_outputs(stem, args):
    assert input_domain(problem(stem), call(args)) == "unknown"


def test_stdin_roles_keep_source_whitespace_and_do_not_swap_names():
    p = problem("Read two integers a, b. 0 <= a < b. Print their sum.")
    original = TestCase(io_mode="stdin", input="  0\t4\r\n")
    assert input_domain(p, original) == "allowed" and original.input == "  0\t4\r\n"
    assert input_domain(p, TestCase(io_mode="stdin", input="4 0")) == "outside"
    assert input_domain(p, TestCase(io_mode="stdin", input="0 4 2")) == "outside"
    assert input_domain(problem("Read stdin and print it exactly."), TestCase(input=" x! \r\n")) == "allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [True, False])
async def test_fresh_out_of_domain_inputs_never_become_student_failures(reference):
    code = "def f(n): return n if n > 0 else 99"
    p = problem(
        "Implement def f(n): n must be a positive integer. Return n.",
        solution_code="def f(n): return n" if reference else None,
    )
    ai_payload = (
        {"inputs": [{"args": [0]}]}
        if reference
        else {
            "cases": [{"io_mode": "function", "function_name": "f", "function_args": [0], "expected_return": "0"}]
        }
    )
    route = fixed_provider(response(ai_payload), response(ai_payload))
    execute = AsyncMock(wraps=programming.run_sandbox)
    with programming_run(None, None):
        for sid in ("first", "second"):
            run = await evidence_for_answer(
                problem=p,
                answer=StudentAnswerInfo(q_id="q1", number="1", type="programming", content=code),
                student_id=sid,
                task_id="task",
                provider=route,
                reporter=None,
                generate=programming._generate_test_cases,
                execute=execute,
            )
            assert (
                run.status == "unavailable"
                and run.reason == "programming_input_domain_unverified"
                and not run.report
            )
    execute.assert_not_awaited()
    assert route.ainvoke.await_count == (2 if reference else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,expected", [(-1, False), (3, True)])
async def test_boundary_checks_only_evaluate_a_new_input_in_the_declared_domain(monkeypatch, extra, expected):
    p = problem(
        "Implement def f(n): n must be a positive integer. Return 2*n.",
        solution_code="def f(n): return 2*n",
        test_cases=[TestCase(source="teacher", function_name="f", function_args=[2], expected_return="4")],
    )
    a = StudentAnswerInfo(q_id="q1", number="1", type="programming", content="def f(n): return 4 if n == 2 else 0")
    route = fixed_provider(response({**award(10), "boundary_case": {"args": [extra], "expected": str(2 * extra)}}))
    execute = AsyncMock(wraps=programming.run_sandbox)
    monkeypatch.setattr(programming, "run_sandbox", execute)
    with programming_run(None, None):
        result = await programming.ProgrammingSkill(route).grade(p, a)
    assert result.score == 10 and result.programming_evidence.score_status == "final"
    assert bool(result.programming_evidence.boundary_note) == expected
    assert execute.await_count == (4 if expected else 2)
    assert result.programming_evidence.teacher_tests.passed == 1 and p.test_cases[0].expected_return == "4"


@pytest.mark.parametrize(
    "stem,raw,status",
    [
        ("Read two integers separated by spaces and print the result.", "2、3", "allowed"),
        ("Read a JSON list of integers from stdin. Return its length.", "[2, 3]", "allowed"),
        ("Read a JSON list of integers from stdin. Return its length.", '["2", 3]', "outside"),
        ("Read a list of integers. Return its length.", "2 3", "unknown"),
        ("Read one integer n. If n is positive, print 1; otherwise print 0.", "-2", "allowed"),
    ],
)
def test_explicit_stdin_protocol(stem, raw, status):
    assert input_domain(problem(stem), TestCase(io_mode="stdin", input=raw)) == status


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["teacher_and_generated", "only_generated", "fresh_unknown"])
async def test_persisted_and_released_results_exclude_out_of_scope_observations(monkeypatch, mode):
    from uuid import uuid4
    from backend.config import settings
    from backend.db import assignment_repository, course_repository, grading_repository, submission_repository
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.domain.errors import ResultNotReleasable
    from backend.services import grading_runs, task_facade

    monkeypatch.setattr(settings, "multi_sample_n", 1)
    uid = uuid4().hex
    teacher, student = "teacher-" + uid, "student-" + uid
    with session_scope() as session:
        for user, role in [(teacher, "teacher"), (student, "student")]:
            session.add(UserRecord(id=user, username=user, role=role, password_hash="fixed", is_active=True))
    tid = task_facade.create_task(
        owner_id=teacher, name="Domain", semester_id=None, course_id=None, idempotency_key=uid
    )["task_id"]
    assignment = assignment_repository.get_assignment(tid, actor_id=teacher)
    course_repository.enroll(course_id=assignment.course_id, student_id=student)
    stem = "Implement def f(n): the input n is guaranteed to be a positive integer. Return 2*n."
    test_cases = [
        TestCase(
            io_mode="function", source="llm_generated", function_name="f", function_args=[-1], expected_return="-2"
        ).model_dump()
    ]
    if mode == "teacher_and_generated":
        test_cases.insert(
            0,
            TestCase(
                io_mode="function", source="teacher", function_name="f", function_args=[3], expected_return="6"
            ).model_dump(),
        )
    if mode == "fresh_unknown":
        stem = "Implement def f(n): n is an integer. Its decimal representation must be palindromic. Return 2*n."
        test_cases = []
    q = assignment_repository.add_question(
        tid,
        teacher_id=teacher,
        q_id="q1",
        order_index=0,
        type="编程题",
        stem=stem,
        criterion="Correctness.",
        test_cases=test_cases,
        reference_answer="```python\ndef f(n): return 2*n\n```",
    )
    assignment_repository.publish(assignment_id=tid, teacher_id=teacher, expected_version=1)
    sub = submission_repository.create_submission(assignment_id=tid, student_id=student)
    submission_repository.add_revision(
        submission_id=sub.id,
        student_id=student,
        source="online",
        answers=[
            {
                "question_id": q.id,
                "q_id": "q1",
                "type": "编程题",
                "content": "def f(n): return 0 if n == -1 else 2*n",
            }
        ],
    )
    payload = (
        {"inputs": [{"args": [13]}]}
        if mode == "fresh_unknown"
        else {**award(10), "boundary_case": {"args": [-1], "expected": "-2"}}
    )
    route = fixed_provider(response(payload))
    registry = SimpleNamespace(count=lambda: 1, list_available=lambda: [route], pick_default=lambda: route)
    run = grading_runs.create_run(teacher_id=teacher, assignment_id=tid)
    await grading_runs.process_run(run_id=run.id, worker_id="worker", registry=registry)
    (saved,) = grading_repository.list_results_for_run(run.id)
    (frozen,) = assignment_repository.list_questions(tid, teacher_id=teacher)
    assert frozen.test_cases == test_cases
    evidence = saved.ai_expert_results[0]["programming_evidence"]
    assert not evidence.get("boundary_note") and evidence["generated_tests"]["total"] == 0
    if mode == "teacher_and_generated":
        assert saved.ai_score == 10 and evidence["teacher_tests"] == {"passed": 1, "total": 1}
        grading_runs.release(teacher_id=teacher, run_id=run.id)
        (released,) = grading_runs.student_results(student_id=student, assignment_id=tid)
        assert released.effective_score == 10 and "additional input" not in released.effective_comment
    else:
        assert saved.ai_score is None and evidence["primary_reason"] == "programming_input_domain_unverified"
        with pytest.raises(ResultNotReleasable):
            grading_runs.release(teacher_id=teacher, run_id=run.id)
        assert not grading_runs.student_results(student_id=student, assignment_id=tid)
        if mode == "only_generated":
            route.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_teacher_explicit_input_and_expectation_remain_authoritative_even_when_domain_disagrees():
    case = TestCase(source="teacher", function_name="f", function_args=[0], expected_return="42")
    p = problem("Implement def f(n): n must be a positive integer. Return n.", test_cases=[case])
    with programming_run(None, None):
        act = await evidence_for_answer(
            problem=p,
            answer=StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def f(n): return 42"),
            student_id="s",
            task_id="t",
            provider=fixed_provider(),
            reporter=None,
            generate=None,
            execute=programming.run_sandbox,
        )
    assert act.status == "executed" and act.report.passed_count == 1
    assert (
        act.cases[0].function_args == [0]
        and act.cases[0].expected_return == "42"
        and act.cases[0].source == "teacher"
    )


@pytest.mark.asyncio
async def test_mixed_easy_and_unverified_ai_proposals_are_retained_with_originals_and_withheld_together():
    from backend.services.programming_reference_inputs import NewTestInput
    from backend.services import programming_runs as runs
    from backend.skills.programming_evidence import describe_run

    p = problem("Implement def f(n): n is an integer in [1, 5). Return n.", solution_code="def f(n): return n")
    proposals = [NewTestInput(args=[2]).draft(), NewTestInput(args=[5]).draft()]
    execute = AsyncMock(side_effect=AssertionError("Do not grade from just the easy retained case."))
    generate = AsyncMock(return_value=(proposals, None))
    with programming_run(None, None) as state:
        for s in ("first", "second"):
            run = await evidence_for_answer(
                problem=p,
                answer=StudentAnswerInfo(q_id="q1", number="1", type="programming", content="def f(n): return 2"),
                student_id=s,
                task_id="task",
                provider=fixed_provider(),
                reporter=None,
                generate=generate,
                execute=execute,
            )
            assert run.status == "unavailable" and run.report is None
            fact, _ = describe_run(run, "zh", p)
            assert (
                fact.score_status == "unavailable"
                and "1 条超出输入范围" in fact.safe_summary
                and "0 条" not in fact.safe_summary
                and "整组待确认" in fact.safe_summary
            )
        audits = [r["value"] for r in state.records.values() if r["kind"] == runs.AUXILIARY]
    (audit,) = audits
    assert [
        (row["position"], row["domain"], row["original"]["function_args"]) for row in audit["ai_candidates"]
    ] == [(1, "allowed", [2]), (2, "outside", [5])]
    assert all("expected_return" not in row["original"] for row in audit["ai_candidates"])
    generate.assert_awaited_once()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_numbers_survive_quarantine_of_saved_ai_case_in_front_of_teacher_case():
    from backend.services import programming_runs as runs
    from backend.skills.programming_evidence import describe_run
    from backend.services.programming_partial_review import record_deduction_basis
    from backend.models import ExpertResult

    samples = [
        TestCase(function_name="f", function_args=[0], expected_return="-42", source="llm_generated"),
        TestCase(function_name="f", function_args=[1], expected_return="2", source="teacher"),
        TestCase(function_name="f", function_args=[2], expected_return="2", source="llm_generated"),
    ]
    p = problem("Implement def f(n): n must be a positive integer. Return n.", test_cases=samples)
    with programming_run(None, None) as state:
        run = await evidence_for_answer(
            problem=p,
            answer=StudentAnswerInfo(q_id="q1", number="1", type="programming", content="def f(n): return n"),
            student_id="s",
            task_id="task",
            provider=fixed_provider(),
            reporter=None,
            generate=None,
            execute=programming.run_sandbox,
        )
        (audit,) = [r["value"] for r in state.records.values() if r["kind"] == runs.AUXILIARY]
    assert audit["summary"]["positions"] == [2, 3]
    assert audit["ai_candidates"][0]["original"]["expected_return"] == "-42"
    assert audit["ai_candidates"][1]["original"]["expected_return"] == "2"
    fact, examples = describe_run(run, "en", p)
    assert fact.teacher_tests.total == fact.generated_tests.total == 1 and examples[0]["case_number"] == 2
    assert "1 outside the input range" in fact.safe_summary and "0 with unverified" not in fact.safe_summary
    assert not fact.teacher_tests.passed and fact.generated_tests.passed == 1
    # Referring to skipped case #1 cannot turn case #2 into a pass or fail.
    vote = ExpertResult(
        provider="probe", score=5, max_score=10, confidence=0.9, comment="x", programming_evidence=fact
    )
    item = SimpleNamespace(step_no=1, max_points=10, score=5, desc="Teacher case 1 failed.", deduction_basis=None)
    record_deduction_basis(vote, [item], run, p)
    assert not vote.programming_evidence.partial_check


def test_conditional_score_and_conditional_input_limit_remain_separate():
    p = problem("Implement f(n): n is an integer. Return its square.",
                criterion="Award full marks if all teacher tests pass.")
    assert input_domain(p, call([-2])) == "allowed"
    p.criterion += " If n is positive, it must be prime."
    assert input_domain(p, call([4])) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement,usable", [([[2]], True), ([[1]], False), ([[-4]], False), ([[2], [3]], False)])
async def test_only_new_outside_inputs_share_one_repair_and_retain_the_hard_legal_cases(replacement, usable):
    from backend.services import programming_runs as runs

    p = problem("Implement f(n): n is a list of nonnegative integers. Return n.",
                solution_code="def f(n): return n")
    provider = fixed_provider(response({"single_arguments": [[1], [-4], [9]]}),
                              response({"single_arguments": replacement}))
    with programming_run(None, None) as state:
        for sid in ("first", "second"):
            run = await evidence_for_answer(
                problem=p, answer=StudentAnswerInfo(q_id="q1", number="1", type="programming",
                                                     content="def f(n): return n if n != [9] else []"),
                student_id=sid, task_id="batch", provider=provider, reporter=None,
                generate=programming._generate_test_cases, execute=programming.run_sandbox)
            if usable:
                assert [c.function_args for c in run.cases] == [[[1]], [[2]], [[9]]]
                assert run.report.passed_count == 2 and not run.report.results[2].passed
            else:
                assert run.report is None and run.reason == "programming_input_domain_unverified"
        audit, = [r["value"] for r in state.records.values() if r["kind"] == runs.AUXILIARY
                  and "original_inputs" in r["value"]]
    assert [row["function_args"] for row in audit["original_inputs"]] == [[[1]], [[-4]], [[9]]]
    assert audit["domains"] == ["allowed", "outside", "allowed"]
    assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_scalar_outliers_get_distinct_in_domain_neighbors_before_reference_execution():
    from backend.services import programming_runs as runs

    p = problem("Implement f(n): n is an integer in [1, 12]. Return n.", solution_code="def f(n): return n")
    provider = fixed_provider(response({"single_arguments": [0, 1, 2, 6, 11, 12, 100]}))
    with programming_run(None, None) as state:
        ev = await evidence_for_answer(
            problem=p, answer=StudentAnswerInfo(q_id="q1", number="1", type="programming", content="def f(n): return 0"),
            student_id="s", task_id="t", provider=provider, reporter=None,
            generate=programming._generate_test_cases, execute=programming.run_sandbox)
        audit, = [r["value"] for r in state.records.values() if r["kind"] == runs.AUXILIARY]
    assert [c.function_args[0] for c in ev.cases] == [3, 1, 2, 6, 11, 12, 10]
    assert audit["original_inputs"][0]["function_args"] == [0] and audit["original_inputs"][-1]["function_args"] == [100]
    assert ev.report.total_count == 7 and ev.report.passed_count == 0 and provider.ainvoke.await_count == 1
