"""Route reassessment survives the existing database/public feedback pipeline."""
import json

import pytest

from backend.services.rubric_drafts import bind_draft, retarget_draft
from backend.skills import math_grading as grading
from backend.tests.test_alternative_route import alternate, material
from backend.tests.test_math_method_review import case, choice, revision, reassessment, provider, run
from backend.tests.test_task_grading_retry import case as db_case, start  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [False, True])
@pytest.mark.parametrize("final_change", [False, True])
@pytest.mark.parametrize("legacy_annotations", [False, True])
async def test_only_final_grading_feedback_survives_public_roundtrip(db_case, errors, final_change, legacy_annotations):
    from sqlalchemy import select
    from backend.db import grading_repository as grades
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.models import Correction
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_grading_checkpoints import prepare, stop

    p, a, plan, experts, right = case()
    row, draft = material(teacher="参考解配方得10分。", alternative={"deduct_per_error": 2})
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == db_case[0], AssignmentQuestionRecord.q_id == p.q_id))
        for key in ("number", "type", "stem", "criterion", "reference_answer", "max_score"):
            setattr(question, key, row[key])
        question.source = {"presentation": {"rubric_draft": draft.model_dump(mode="json")}}
    run_id = start(db_case, f"method-review-feedback-{errors}")["job_id"]
    checkpoints, questions, revisions, _ = prepare(db_case, run_id)
    question, student = questions[0], revisions[0][1]
    p = problem_from_question(question)
    plan = bind_draft(retarget_draft(draft, p), p)
    checkpoints.save_math_plan(p, plan)
    for expert in experts:
        trace = grading.read_math_trace(expert)
        trace["plan_id"] = plan.plan_id
        expert.raw_output = json.dumps(trace)
    final = revision(right if errors else alternate("complete"))
    final["reason"] = "代入第二个根仍违反原方程，因此保留此处扣分。" if errors else "原等价变换已覆盖两个方向，不要求重抄标答。"
    main, _ = provider("primary", choice(), {"revisions": [final] if final_change else []})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    expert = result[0]
    if legacy_annotations:
        trace = grading.read_math_trace(expert)
        trace["method_review"]["original_assessment"]["comment"] = "PRIVATE superseded wrong-method feedback"
        expert = expert.model_copy(update={"comment": expert.comment + "\n\n解法改判依据：PRIVATE process history\n\n解法已重判，最终联合复核未完成。",
                                          "raw_output": json.dumps(trace)})
        if final_change:
            expert.steps[0] = expert.steps[0].model_copy(update={"desc": expert.steps[0].desc + "\n定向复核依据：" + final["reason"]})
    method_reason = choice()['choices'][0]['reason']
    checkpoints.save_correction(student, Correction(q_id=p.q_id, type=p.type, score=expert.score,
        max_score=10, confidence=.9, comment=expert.comment, steps=expert.steps,
        expert_results=[expert], synthesis_method="single"))
    stop(db_case, run_id)
    before = [r.model_dump() for r in grades.list_results_for_run(run_id)]
    payload = task_facade.task_results(task_id=db_case[0], owner_id=db_case[1])
    correction = next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == p.q_id)
    restored = correction["expert_results"][0]
    assert all(word not in correction["comment"] for word in ("PRIVATE", "解法已重判", "改判依据", "定向复核依据", "联合复核"))
    assert restored["score"] == (8 if errors or not final_change else 10)
    assert method_reason not in restored['comment']
    assert restored['comment'].count("解题思路与标答不同，按另解标准评分。") == 1
    assert all(word not in str(restored) for word in ("PRIVATE", "解法已重判", "改判依据", "定向复核依据", "联合复核"))
    assert len(next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == p.q_id)["expert_results"]) == 1
    assert all(method_reason not in step['desc'] for step in restored['steps'])
    assert len(restored["steps"]) == 1 and sum(s["score"] for s in restored["steps"]) == restored["score"]
    assert restored["raw_output"] is None
    assert [r.model_dump() for r in grades.list_results_for_run(run_id)] == before
    assert task_facade.task_results(task_id=db_case[0], owner_id=db_case[1]) == payload
