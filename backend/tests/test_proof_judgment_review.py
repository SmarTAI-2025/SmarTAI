"""Review orchestration contracts; fixed replies do not measure AI accuracy."""
import json

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.part_rubric import PartRubric
from backend.services.proof_goals import PartGoals, ProofGoal, ProofOutline, compile_proof_goals
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, retarget_draft
from backend.skills import math_grading as grading
from backend.skills.part_scoring import PartDecision
from backend.tests.test_frozen_part_rubric import question_row
from backend.tests.test_math_evidence_reliability import raw_provider
from backend.tests.test_math_grading import expert_for


def material():
    stem = "Prove existence. Decide uniqueness."
    raw = dict(id="q1", label="1", stem=stem, total_pts=10, parts=[dict(
        id="p", label=None, stem=None, total_pts=10, steps=[dict(id="s", criterion="Proof", total_pts=10)],
        alternative=dict(maximum={"points": 5}, deduct_per_error=1))])
    rubric = PartRubric.model_validate(raw)
    outline = ProofOutline(parts=[PartGoals(part_id="p", goals=[ProofGoal(
        target=target, source_id="question:1", points=points,
        partial_progress="A valid necessary condition has been established.",
        sufficient_argument="The stated premises imply the requested result.")
        for target, points in (("Prove existence.", 6), ("Decide uniqueness.", 4))])])
    rubric = compile_proof_goals(rubric, outline, {"p": {"question:1": stem}}, language="en")
    row = question_row(raw)
    row["type"] = "推理题"
    row["ai_completion_provenance"] = {"criterion": {"source_kind": "ai_generated"}}
    row["criterion"], draft = generated_rubric(row, RubricDefinition(part_rubric=rubric))
    p = ProblemInfo(**row)
    p._math_rubric_source = {"presentation": {key: row[key] for key in (
        "question_structure", "ai_completion_provenance")}}
    return p, retarget_draft(draft, row).plan


def reading(repairs=("partial", "partial")):
    findings = [dict(group=f"goal{i}", evidence=f"student:{i}",
                     implication="Check this implication under the given hypotheses.", repair=repair,
                     **({"remaining": "Check whether an additional argument is necessary.",
                         "origin_quote": f"student:{i}"} if repair != "none" else {}))
                for i, repair in enumerate(repairs, 1)]
    return dict(part_id="p", target_quote="part:p", student_quote="student:1, student:2",
                method="minor_variant", relevance="complete" if set(repairs) == {"none"} else "partial",
                progress="Both requested claims were considered.", goal_findings=findings)


def assessed(repairs):
    return grading.ProofPartAssessment.model_validate(dict(
        comment="Check the argument.", part_decisions=[reading(repairs)])).assessment()


def test_observations_preserve_math_and_source_without_repeating_loss_labels():
    _, plan = material()
    a = assessed(("partial", "local"))
    snapshot = a.model_dump_json()
    old = a.part_decisions[0]
    out = grading._proof_review_observations(old, plan.part_rubric)
    assert out["method"] == old.method and out["student_quote"] == old.student_quote
    assert {f["group"] for f in out["mathematical_observations"]} == {"goal1", "goal2"}
    assert all(f["implication"] and f["evidence"] for f in out["mathematical_observations"])
    assert not any(key in out for key in ("errors", "relevance", "goal_findings"))
    assert all("repair" not in f for f in out["mathematical_observations"])
    assert a.model_dump_json() == snapshot
    # Addressed/legacy replies may contain errors but no goal_findings.
    canonical = old.model_copy(update={"goal_findings": ()})
    legacy = grading._proof_review_observations(canonical, plan.part_rubric)
    assert [f["group"] for f in legacy["mathematical_observations"]] == ["goal1", "goal2"]
    assert all("rule_id" not in f for f in legacy["mathematical_observations"])


def test_complete_and_alternative_ledgers_keep_their_existing_review_protocol():
    _, plan = material()
    full = assessed(("none", "none")).part_decisions[0]
    alternative = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1",
        method="alternative", reference_method="Substitution", student_method="Construction",
        relevance="complete", progress="A different construction proves the result.")
    for decision in (full, alternative):
        assert grading._proof_review_observations(decision, plan.part_rubric) == decision.model_dump(
            mode="json", exclude_defaults=True)


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("misclassified_example", [
    "For every positive integer n, x^n=e iff (x^-1)^n=e; both orders are the same minimum or both infinite.",
    "Substituting u=2 into the established identity f(u)=u*u gives the requested value f(2)=4.",
    "For arbitrary a,b, membership in the two sets is equivalent, so the sets are equal.",
])
def test_generated_loss_rules_do_not_promote_unverified_proof_examples(language, misclassified_example):
    _, plan = material()
    original = plan.part_rubric
    outline = ProofOutline(parts=[PartGoals(part_id="p", goals=[ProofGoal(
        target=original.stem, source_id="question:1", points=10,
        partial_progress=misclassified_example, sufficient_argument="Unverified shortcut.")])])
    frozen = original.model_dump_json()
    rules = compile_proof_goals(original, outline, {"p": {"question:1": original.stem}}, language=language)
    compiled = rules.model_dump_json()
    assert misclassified_example not in compiled and "Unverified shortcut" not in compiled
    assert [r.deduct_pts for r in rules.parts[0].deductions] == [10, 5, 1]
    assert rules.parts[0].alternative == original.parts[0].alternative
    assert original.model_dump_json() == frozen
    # New requests only ask for source goals and budgets. Old responses remain
    # readable, but their proof sketches cannot become score-lowering rules.
    assert set(ProofGoal.model_json_schema()["properties"]) == {"target", "source_id", "points"}
    assert ProofGoal(target="Conclusion", source_id="question:1", points=10).points == 10


@pytest.mark.parametrize("source,rewritten", [
    (r"Show that $D_4=\langle r,s\mid r^2=s^2=1,rs=sr^{-1}\rangle$.",
     r"Show that $D_4=\langle r,s\mid r^4=s^2=1,rs=sr^{-1}\rangle$."),
    ("For every positive integer n, prove P(n).", "For some positive integer n, prove P(n)."),
    ("证明当 x>0 时，f(x)=1。", "证明当 x>=0 时，f(x)=1。"),
    ("Show f(x)=10.", "Show f(x)=1."),
])
def test_source_id_cannot_authorize_an_invented_or_changed_goal(source, rewritten):
    _, plan = material()
    rules = plan.part_rubric
    def compile(target):
        return compile_proof_goals(rules, ProofOutline(parts=[PartGoals(part_id="p", goals=[
            ProofGoal(target=target, source_id="question:1", points=10)])]),
            {"p": {"question:1": source}}, language="en")
    with pytest.raises(ValueError, match="Quote the requested result"):
        compile(rewritten)
    correct = compile(source)
    assert all(rule.criterion.startswith(source) for rule in correct.parts[0].deductions)


def test_multiline_goal_uses_original_text_and_an_intersecting_source_line():
    from backend.services.proof_goals import _source_match
    lines = {"question:1": "Let f(x)=x*x for real x.", "question:2": "Show that for every real x,",
             "question:3": "$f(x) = x^{2}$."}
    def outline(source_id):
        return ProofOutline(parts=[PartGoals(part_id="p", goals=[ProofGoal(
            target="Show that for every real x, \\(f(x)=x^{2}\\).", source_id=source_id, points=10)])])
    found = _source_match(outline("question:2").parts[0].goals[0], lines)
    assert found.text == lines["question:2"]+"\n"+lines["question:3"]
    with pytest.raises(ValueError, match="source_id"):
        _source_match(outline("question:1").parts[0].goals[0], lines)


@pytest.mark.parametrize("clauses", [
    ("Use the recurrence to prove the bound. ", "Deduce convergence."),
    ("构造一个满足条件的对象。\n", "然后判断该对象是否唯一。"),
    ("Let x>0. ", "Show f(x)=1."),
])
def test_authentic_quotes_must_not_omit_other_requests_or_assumptions(clauses):
    _, plan = material()
    source = "".join(clauses)
    def compile(targets):
        return compile_proof_goals(plan.part_rubric, ProofOutline(parts=[PartGoals(part_id="p", goals=[
            ProofGoal(target=t, source_id="question:1", points=10/len(targets)) for t in targets])]),
            {"p": {"question:1": source}}, language="en")
    for incomplete in clauses:
        with pytest.raises(ValueError, match="source content was omitted"):
            compile([incomplete.strip()])
    assert len(compile([source]).parts[0].deductions) == 3
    assert len(compile([c.strip() for c in clauses]).parts[0].deductions) == 6


@pytest.mark.parametrize("evidence", ["question:2,question:3", "question:2；question:3",
                                      "question:2 question:3", "Decide whether the witness\nis unique."])
def test_an_omitted_goal_can_cite_all_its_wrapped_question_lines(evidence):
    _, plan = material()
    question = "Prove existence.\nDecide whether the witness\nis unique."
    decision = reading(("none", "absent"))
    decision["student_quote"] = "student:1"
    decision["goal_findings"][1].update(
        evidence=evidence, origin_quote="Decide whether the witness\nis unique.")
    assessment = grading.ProofPartAssessment.model_validate(dict(
        comment="The second requested result was omitted.", part_decisions=[decision])).assessment()
    score, _ = grading.score_assessment(assessment, plan, "Here is a valid witness.", problem_text=question)
    assert score == 6


@pytest.mark.parametrize("evidence", ["question:2,question:999", "question:2,question:4",
                                      "question:2-3", "question:2,student:1"])
def test_question_evidence_does_not_accept_missing_foreign_or_malformed_lines(evidence):
    from backend.services.math_source_evidence import source_catalog
    from backend.skills.part_scoring import _obligation_evidence
    scope = "Prove existence.\nDecide whether the witness\nis unique."
    question = scope + "\nA different part: evaluate the integral."
    assert not _obligation_evidence(question, scope, evidence, source_catalog("", question, ""))


def test_question_lines_cannot_replace_student_evidence_for_an_earned_goal():
    _, plan = material()
    decision = reading(("none", "none"))
    decision["student_quote"] = "student:1"
    decision["goal_findings"][1]["evidence"] = "question:1,question:2"
    assessment = grading.ProofPartAssessment.model_validate(dict(
        comment="Do not turn a request into a student proof.", part_decisions=[decision])).assessment()
    with pytest.raises(ValueError, match="completed proof goals require original student evidence"):
        grading.score_assessment(assessment, plan, "Here is a valid witness.",
                                 problem_text="Prove existence.\nDecide uniqueness.")


@pytest.mark.asyncio
async def test_wrapped_source_is_supplied_as_one_task_not_fragment_goals(monkeypatch):
    from backend.services import proof_goals as module
    _, plan = material()
    source = "Let f(x)=x*x\nShow that for every real x\nf(x)=x^2"
    async def reply(provider, *, user_prompt, validate_output, **kwargs):
        if kwargs.get("output_model") is module.ProofGoalAudit:
            outline = json.loads(user_prompt)["proposal"]
            audit = module.ProofGoalAudit(parts=[dict(part_id=p["part_id"], verdict="consistent",
                                                  reason="Each part requests the quoted result.") for p in outline["parts"]])
            validate_output(audit)
            return audit, None
        assert json.loads(user_prompt)["question_sources"] == {"question:1": source}
        outline = ProofOutline(parts=[PartGoals(part_id="p", goals=[ProofGoal(
            target=source, source_id="question:1", points=10)])])
        validate_output(outline)
        return outline, None
    monkeypatch.setattr(module, "structured_llm_call", reply)
    rules = await module.prepare_source_goals(None, source, plan.part_rubric, language="en")
    assert rules.parts[0].deductions[0].criterion.startswith(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("repairs,expected", [(("none", "none"), 10), (("none", "partial"), 8),
                                             (("local", "none"), 9), (("absent", "partial"), 2)])
async def test_one_review_reassesses_unanimous_proof_gaps_using_unchanged_rules(repairs, expected):
    p, plan = material()
    answer = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type,
        content="The proposed pair satisfies the equation.\nI compare every possible solution with this pair.")
    original = assessed(("partial", "partial"))
    votes = [expert_for(original, plan, answer.content) for _ in range(3)]
    for vote in votes:
        trace = grading.read_math_trace(vote)
        trace.update(review_pending=True, relevance_review_pending=True)
        vote.raw_output = json.dumps(trace)
    snapshots = [vote.model_dump_json() for vote in votes]
    frozen = plan.model_dump_json()
    revisions = [dict(expert_index=i, reason="Check the concrete implication, not how many lines are written.",
                      comment="Independent evidence check.", decisions=[reading(repairs)]) for i in range(3)]
    provider, calls = raw_provider(json.dumps({"revisions": revisions}))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, answer, votes, provider)
        # This still consumes the existing one-pass review budget.
        assert not grading.take_review_budget()
    assert len(calls) == 1
    assert [vote.score for vote in result] == [expected] * 3
    assert [vote.model_dump_json() for vote in votes] == snapshots
    assert plan.model_dump_json() == frozen
    sent = json.loads(calls[0][-1].content)
    assert sent["answer"] == answer.content
    assert sent["plan"]["parts"][0]["alternative"]["maximum"] == {"points": 5.0}
    for expert in sent["experts"]:
        assert "mathematical_observations" in expert["decisions"][0]
        assert "relevance" not in expert["decisions"][0]
    assert all(grading.read_math_trace(vote)["plan_id"] == plan.plan_id for vote in result)
