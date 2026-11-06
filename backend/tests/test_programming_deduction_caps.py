"""Preserve the two original blind model responses and grade their frozen cases."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import TestCase
from backend.services import programming_runs as runs
from backend.services.programming_partial_review import REVIEW_REASON, review_inputs, review_partial_score
from backend.tests.test_programming_partial_review import grade, registry
from backend.tests.test_programming_scoring_contract import fixed_provider, response
from backend.tests.test_programming_skill import _make_answer, _make_problem

DATA = Path(__file__).parent / "fixtures" / "programming_blind_partial"


def replay(name):
    original = (DATA / f"{name}.json").read_text()
    frozen = json.loads((DATA / "teacher-and-submission.json").read_text())
    cases = [TestCase(function_name="sum_positive", function_args=json.loads(c["call_arguments"])["args"],
                      expected_return=c["expected"]) for c in sorted(frozen["case_examples"], key=lambda c: c["case_label"])]
    problem = _make_problem(stem=frozen["problem"], test_cases=cases)
    problem.solution_code, problem.criterion = frozen["solution_code"], frozen["rubric"]
    model_reply = response({})
    model_reply.content = original
    return problem, _make_answer(frozen["submitted_source"]), model_reply


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sha", [
    ("blind-four","f5980e540e5d4530c01dcc00658626c81a79a757a2f1f2c1cd936b4740b5b7da"),
    ("blind-nine","e04c6d74e90707bea2fa854eb4cc29b2af0899cef7830c1174e56e388de81fce"),
])
async def test_independent_models_original_awards_are_checked_against_teachers_explicit_cap(name, sha):
    problem, answer, first = replay(name)
    assert hashlib.sha256(first.content.encode()).hexdigest() == sha
    model = fixed_provider(first, response({"confirmed":[]}))
    corr = await grade(registry(model), problem, answer, language="zh")
    vote, = corr.expert_results
    assert corr.score == 4 and vote.score == 4 and model.ainvoke.await_count == 2
    assert REVIEW_REASON in corr.review_reasons and corr.requires_human_review and "合计扣了" in corr.comment
    evidence = vote.programming_evidence
    assert (evidence.teacher_tests.passed, evidence.teacher_tests.total) == (4, 5)
    issue, = evidence.partial_check["issues"]
    assert issue["steps"] == [1,3] and issue["cases"] == [4]
    assert issue["allowed"] == "4" and float(issue["deducted"]) == 6
    assert issue["governing_rule"] in problem.criterion and issue["source_quote"] in answer.content
    assert issue["model_assessment"] == "not_confirmed" and issue["verification_source"] == "teacher_rubric_source_reference_and_awards"
    assert all(q in d for q, d in zip(issue["reason_quotes"], issue["descriptions"], strict=True))


def different_program(name, source, reference, cap, descriptions):
    problem = _make_problem(stem=f"Implement def {name}(x):", test_cases=[
        TestCase(function_name=name, function_args=[x], expected_return=repr(x)) for x in [0, 1, -0.5]])
    problem.solution_code = f"def {name}(x): return {reference}"
    problem.criterion = cap
    draft = dict(max_score=10, confidence=.9, steps=[
        dict(step_no=i, desc=text,max_points=5,score=3,is_correct=False) for i,text in enumerate(descriptions, start=1)])
    return problem, _make_answer(f"def {name}(x): return {source}"), response(draft)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expression,rule,desc,should_check,must_review", [
    ("distance","abs(x)","abs(...) costs only 2 points.",
     ("Using abs caused a loss of sign.", "Using abs caused an incorrect negative result."),True,True),
    ("measure","int(x)","int(...) costs at most 1 point.",
     ("Using int caused the fractional part to disappear.", "Using int caused an incorrect output."),True,True),
    ("distance","abs(x)","Assess direction and method independently.",
     ("Case 3 illustrates a sign discrepancy.", "Case 3 also violates the separate method requirement."),True,False),
    ("distance","abs(x)","abs(...) costs only 2 points.",
     ("Using abs caused a sign error.", "A separate requirement asks for an explanation."),True,False),
    ("distance","abs(x)","If speed is adequate, abs(...) costs only 2 points.",
     ("Both results use abs.", "The negative answer uses abs."),True,False),
    ("distance","abs(x) if x < 0 else x","abs(...) costs only 2 points.",
     ("Using abs caused a sign error.", "Using abs caused wrong output."),True,False),
    ("distance","abs(x)","abs(...) costs only 4 points.",
     ("Using abs caused a sign error.", "Using abs caused wrong output."),False,False),
    ("distance","abs(x)","abs(...) costs only 2 points, provided a full explanation is also present.",
     ("Both results use abs.", "The negative answer uses abs."),True,False),
    ("distance","abs(x)","Assess the rubric as written.",
     ("The predicate uses abs here.", "Se volvió a usar abs."),True,False),
    ("distance","abs(x)","Assess sign and documentation independently. abs(...) costs only 2 points.",
     ("The unexpected abs affects the sign in case 3.", "The abs call in case 3 has no documentation."),True,False),
    ("distance","abs(x)","Assess method and explanation.",
     ("An intermediate argument needs justification.", "The comment does not explain the units."),False,False),
])
async def test_source_caps_require_matched_source_change_and_actual_shared_ground(name,expression,rule,desc,should_check,must_review):
    problem, answer, reply = different_program(name,expression,"x",rule,desc)
    model = fixed_provider(reply,response({"confirmed":[1]}))
    corr = await grade(registry(model),problem,answer)
    assert corr.score == 6 and model.ainvoke.await_count == 1 + should_check
    assert (REVIEW_REASON in corr.review_reasons) is must_review
    assert corr.requires_human_review is must_review


@pytest.mark.asyncio
async def test_old_vote_with_matching_execution_recomputes_evidence_without_replaying_the_review(monkeypatch):
    problem, answer, first = replay("blind-nine")
    provider = fixed_provider(first, response({"confirmed":[]}))
    reg = registry(provider)
    with runs.programming_run(reg, None) as state:
        corr = await grade(reg, problem, answer)
        key, votes, _ = review_inputs(corr, "s")
        vote, = votes
        # Simulate the exact pre-fix checkpoint: public steps omit optional
        # awards, the raw validated model response and execution remain available.
        from backend.models import StepScore
        vote.steps = [StepScore.model_validate(s.model_dump()) for s in vote.steps]
        vote.programming_evidence.partial_check = None
        corr.review_reasons.clear()
        corr.requires_human_review = False
        state.restored_evidence_ids.add(vote.programming_evidence.evidence_id)
        state.store(runs.AUXILIARY,key,{"status":"done","reviewed":[],"model_confirmed":[]})
        state.provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("no second paid check")))
        await review_partial_score(corr,state=state,problem=problem,answer=answer,student_id="s",language="en",reporter=None)
        assert REVIEW_REASON in corr.review_reasons and vote.programming_evidence.partial_check["status"] == "confirmed"
        state.provider.ainvoke.assert_not_awaited()
        assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_explicitly_separate_criteria_and_unsupported_reference_do_not_confirm():
    problem, answer, reply = different_program("position","abs(x)","x",
        "Assess sign and documentation independently. abs(...) costs only 2 points.",
        ("Using abs changes the sign in case 3.", "The abs call in case 3 has no explanation."))
    data = json.loads(reply.content)
    for step, requirement in zip(data["steps"], ["sign","documentation"], strict=True):
        step["deduction_basis"] = {"failed_teacher_cases":[3], "rubric_quote":requirement}
    provider = fixed_provider(response(data))
    corr = await grade(registry(provider),problem,answer)
    assert not corr.requires_human_review and provider.ainvoke.await_count == 1
    problem.solution_code = None
    provider = fixed_provider(reply, response({"confirmed":[1]}))
    corr = await grade(registry(provider),problem,answer)
    assert provider.ainvoke.await_count == 2 and not corr.requires_human_review


def test_arbitrary_nested_call_shape_compares_against_full_reference_without_evaluation():
    from backend.services.programming_deduction_caps import uncited_deductions
    steps = [SimpleNamespace(step_no=i, max_points=3, score=1, desc=text)
             for i,text in enumerate(["The submitted abs changes the result.", "El resultado usa abs."], 1)]
    issue = uncited_deductions(steps, 4, "`list(sorted(abs(...)))` costs only 2 points.",
        "def make(x): return list(sorted(abs(x)))", "def make(x): return list(sorted(x))", "make")
    assert issue["kind"] == "capped_source_deduction_exceeded"
    assert issue["source_quote"] == "abs(x)" and issue["source_pattern"] == "list(sorted(abs(...)))"


@pytest.mark.asyncio
@pytest.mark.parametrize("match", [True, False])
async def test_optional_reviewer_locators_must_match_all_local_witnesses(match):
    problem, answer, first = replay("blind-four")
    desc = [s["desc"] for s in json.loads(first.content)["steps"] if not s["is_correct"]]
    witness = dict(issue=1, rubric_quote="sum(set(...))丢重复值只扣重复值4分", source_quote="set(x for x in nums if x > 0)", reason_quotes=desc)
    if not match:
        witness["source_quote"] = "a suggestion from the reviewer that is not in the source"
    model = fixed_provider(first, response({"confirmed":[1],"witnesses":[witness]}))
    corr = await grade(registry(model),problem,answer)
    issue, = corr.expert_results[0].programming_evidence.partial_check["issues"]
    assert model.ainvoke.await_count == 2 and corr.requires_human_review
    assert issue.get("reviewed_witness") == (witness if match else None)
