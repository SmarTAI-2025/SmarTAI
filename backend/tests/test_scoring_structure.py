"""Public scores follow frozen budgets; deduction evidence is not an award."""
from decimal import Decimal

import pytest

from backend.models import ExpertResult, StepScore
from backend.services.part_rubric import PartRubric
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.skills.math_grading import weighted_math_steps


def rules(*, deduction=False, alternative=None, penalty=None):
    part = dict(id="p", total_pts=10, alternative=alternative or dict(deduct_per_error=1))
    if deduction:
        part["deductions"] = [dict(id="error", criterion="Each independent error", deduct_pts=1, **(penalty or {}))]
    else:
        part["steps"] = [dict(id=f"s{i}", criterion=f"Goal {i}", total_pts=pts)
                         for i, pts in enumerate([4, 3, 3], 1)]
    return PartRubric.model_validate(dict(id="q", label="1", stem="Prove the result.", total_pts=10, parts=[part]))


def decision(*, method="alternative", errors=3, relevance=None, steps=()):
    return PartDecision(part_id="p", target_quote="part:p", student_quote="student:1",
        relevance=relevance or ("partial" if errors else "complete"), method=method,
        reference_method="Construct special elements", student_method="Compare arbitrary coordinates",
        progress="Submitted argument", steps=steps,
        errors=[dict(id=f"e{i}", rule_id="alternative" if method=="alternative" else "error",
                     evidence=f"student:{i}", origin_quote=f"student:{i}", reason=f"Assertion {i} divides by x without proving x is nonzero.")
                for i in range(1, errors+1)])


ANSWER = "\n".join(f"assertion {i}" for i in range(1, 15))


@pytest.mark.parametrize("method", ["alternative", "reference", "minor_variant"])
@pytest.mark.parametrize("errors", [0, 1, 3, 12])
def test_deductions_have_one_net_score_and_only_actual_charges(method, errors):
    rubric = rules(deduction=method!="alternative")
    score, steps = settle_parts(rubric, [decision(method=method, errors=errors)], ANSWER, rubric.stem)
    assert score == max(0, 10-errors)
    assert len(steps) == 1 and steps[0].item_id is None
    assert steps[0].score == score
    assert steps[0].is_alternative == (method == "alternative")
    assert [d.points for d in steps[0].deductions] == [1]*min(errors, 10)
    assert StepScore.model_validate_json(steps[0].model_dump_json()) == steps[0]


@pytest.mark.parametrize("policy,expected", [
    ({"minimum":{"points":8},"deduct_per_error":1}, [1,1]),
    ({"max_deduction":1.5,"deduct_per_error":1}, [1,.5]),
    ({"maximum":{"points":5},"minimum":{"points":5}}, []),
])
def test_alternative_effective_deductions_obey_caps_and_floor(policy, expected):
    rubric=rules(alternative=policy)
    score, steps=settle_parts(rubric,[decision()],ANSWER,rubric.stem)
    assert [d.points for d in steps[0].deductions] == expected
    assert score == rubric.parts[0].alternative.maximum.resolve(10)-sum(expected)


@pytest.mark.parametrize("penalty,expected", [({"limit":2},[1,1]),({"max_deduction":1.5},[1,.5])])
def test_primary_deduction_count_and_amount_caps(penalty, expected):
    rubric=rules(deduction=True,penalty=penalty)
    score, steps=settle_parts(rubric,[decision(method="reference")],ANSWER,rubric.stem)
    assert [d.points for d in steps[0].deductions] == expected
    assert score == 10-sum(expected)


@pytest.mark.parametrize("method", ["reference","minor_variant"])
@pytest.mark.parametrize("relevance", ["incorrect","unanswered","unrelated","uncertain"])
def test_zero_primary_keeps_every_frozen_step_in_order(method, relevance):
    rubric=rules()
    d=decision(method=method,errors=0,relevance=relevance)
    score, steps=settle_parts(rubric,[d],ANSWER,rubric.stem)
    assert score==0
    assert [(s.item_id,s.score,s.deductions) for s in steps]==[(f"s{i}",0,None) for i in range(1,4)]


def test_missed_reasons_and_frozen_order_survive_zero_result():
    rubric=rules()
    d=decision(method="reference",errors=0,relevance="incorrect",steps=[
        dict(id=f"s{i}",outcome="missed",evidence="",reason=f"Missing goal {i}") for i in [3,1,2]])
    _,steps=settle_parts(rubric,[d],ANSWER,rubric.stem)
    assert [s.desc for s in steps]==[f"Missing goal {i}" for i in [1,2,3]]


@pytest.mark.parametrize("label", ["Independent error 1", "independent error", "独立错误2", "扣分点三", "Deduction point 3."])
def test_generic_error_labels_require_a_specific_explanation(label):
    d = decision(errors=1)
    d = d.model_copy(update={"errors": (d.errors[0].model_copy(update={"reason": label}),)})
    with pytest.raises(ValueError, match="specific wrong statement"):
        settle_parts(rules(), [d], ANSWER, rules().stem)


def test_error_detail_is_preserved_verbatim():
    d = decision(errors=1)
    _, steps = settle_parts(rules(), [d], ANSWER, rules().stem)
    assert steps[0].deductions[0].desc == d.errors[0].reason


def test_synthesis_keeps_deduction_evidence_without_adding_score_leaves(monkeypatch):
    from backend.skills import math_grading
    monkeypatch.setattr(math_grading,"read_math_trace",lambda _: {"assessment":{"route_id":"parts","part_decisions":[{}]}})
    experts=[]
    for count in [3,0]:
        score,steps=settle_parts(rules(),[decision(errors=count)],ANSWER,rules().stem)
        experts.append(ExpertResult(provider=f"model-{count}",score=score,max_score=10,comment="",steps=steps))
    rows=weighted_math_steps(experts,[Decimal(1),Decimal(1)],10,8.5)
    assert [r.score for r in rows]==[8.5]
    assert [d.points for d in rows[0].deductions]==[.5,.5,.5]
    assert rows[0].is_alternative


@pytest.mark.parametrize("method,errors", [("alternative",0),("alternative",3),("reference",3)])
def test_saved_trace_projects_route_and_charges_without_regrading(method, errors):
    from types import SimpleNamespace
    from backend.services.grading_feedback import _concise_steps
    from backend.skills.math_grading import MathAssessment, TRACE_VERSION
    rubric=rules(deduction=method=="reference")
    d=decision(method=method,errors=errors)
    assessment=MathAssessment(route_id="parts",route_reason="Frozen",comment="",part_decisions=[d])
    plan=SimpleNamespace(plan_id="frozen-plan",part_rubric=rubric)
    trace=dict(version=TRACE_VERSION,plan_id=plan.plan_id,assessment=assessment.model_dump(mode="json"))
    old=[dict(step_no=1,part_id="p",score=10-errors,is_correct=not errors,desc="Saved explanation")]
    shown=_concise_steps(old,trace,plan)
    assert shown[0]["score"]==old[0]["score"]
    assert shown[0].get("is_alternative",False)==(method=="alternative")
    assert [d["points"] for d in shown[0]["deductions"]]==[1]*errors
    assert "deductions" not in old[0]
    assert _concise_steps(old,{**trace,"plan_id":"different"},plan)==old
    inconsistent=[{**old[0],"score":9.5}]
    assert _concise_steps(inconsistent,trace,plan)==inconsistent


def test_saved_primary_zero_expands_only_the_frozen_steps():
    from types import SimpleNamespace
    from backend.services.grading_feedback import _part_steps
    rubric=rules()
    d=decision(method="reference",errors=0,relevance="incorrect")
    old=[dict(step_no=1,part_id="p",score=0,is_correct=False,desc="No credit")]
    shown=_part_steps(old,SimpleNamespace(part_decisions=[d]),rubric)
    assert [(s["item_id"],s["score"]) for s in shown]==[(f"s{i}",0) for i in [1,2,3]]
    assert len(old)==1 and "item_id" not in old[0]
