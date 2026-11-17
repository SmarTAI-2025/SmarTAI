"""Public math feedback describes the last valid vote, not the review process."""
import json

import pytest

from backend.agents.multi_expert import _expert_to_correction
from backend.domain.grading_review import review_state
from backend.services.math_final_feedback import _combined_comment, final_math_feedback
from backend.skills import math_grading as grading
from backend.skills.math_method_review import with_method_feedback
from backend.tests.test_math_grading import expert_for
from backend.tests.test_math_method_review import case


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("method", ["reference", "alternative"])
@pytest.mark.parametrize("tail", ["", "\n工具核验与当前判断仍有待核对之处：请核对定义域。", "\nProof evidence to check: Check the reverse implication.\nEspecially the domain."])
def test_public_feedback_uses_current_vote_and_keeps_private_history(method, language, tail):
    p, a, plan, experts, _ = case()
    expert = experts[method == "alternative"]
    trace = grading.read_math_trace(expert)
    trace["assessment"]["comment"] = "The final scoring reason."
    trace["method_review"] = {"status": "reassessed", "choices": [{"reason": "PRIVATE adjudication"}],
        "original_assessment": {"comment": "PRIVATE superseded feedback", "score": 1}}
    trace["method_review_pending"] = True
    trace["targeted_review"] = {"reason": "PRIVATE review process"}
    trace["review_advisory"] = bool(tail)
    before_desc = expert.steps[0].desc
    label = "\n定向复核依据：" if language == "zh" else "\nTargeted review: "
    expert = expert.model_copy(update={"comment": "PRIVATE previous score and 解法已重判",
        "steps": [expert.steps[0].model_copy(update={"desc": before_desc + label + "PRIVATE review process" + tail}), *expert.steps[1:]],
        "raw_output": json.dumps(trace)})
    before = expert.model_dump_json()
    shown = with_method_feedback(expert, language)
    assert shown.comment.startswith("The final scoring reason.")
    assert "PRIVATE" not in shown.comment
    assert shown.steps[0].desc == before_desc + tail
    assert shown.score == expert.score and shown.confidence == expert.confidence
    assert len(shown.steps) == len(expert.steps)
    assert shown.raw_output == expert.raw_output and expert.model_dump_json() == before
    note = "解题思路与标答不同，按另解标准评分。" if language == "zh" else "scored using the alternative rubric."
    assert (note in shown.comment) == (method == "alternative")
    assert with_method_feedback(shown, language) == shown


def test_mixed_parts_note_does_not_apply_alternative_policy_to_every_part():
    _, _, _, experts, _ = case()
    trace = grading.read_math_trace(experts[1])
    trace["assessment"]["part_decisions"].append({"part_id": "second", "method": "reference", "relevance": "complete"})
    comment, _ = final_math_feedback("Final mathematics.", [], trace, "zh")
    assert comment.endswith("部分小问解题思路与标答不同，按另解标准评分。")


@pytest.mark.parametrize("relevance", ["complete", "partial", "incorrect"])
def test_alternative_policy_requires_only_optional_review_even_at_zero(relevance):
    p, a, plan, experts, _ = case(teacher="参考解配方得10分。其他解法不得分。")
    trace = grading.read_math_trace(experts[1])
    assessment = grading.MathAssessment.model_validate(trace["assessment"])
    assessment.part_decisions[0] = assessment.part_decisions[0].model_copy(update={"relevance": relevance, "errors": ()})
    expert = expert_for(assessment, plan, a.content).model_copy(update={"confidence": .95})
    assert expert.score == 0
    result = _expert_to_correction(p.q_id, p.type, expert)
    assert "math_alternative_method" in result.review_reasons
    assert "math_method_review_incomplete" not in result.review_reasons
    assert review_state(result.model_dump()) == (True, False)
    assert review_state({**result.model_dump(), "teacher_score": 0, "review_status": "confirmed"}) == (False, True)


def test_public_projection_does_not_guess_without_current_trace_version():
    comment, steps = "Original text", [{"desc": "Original scored reason", "score": 3}]
    assert final_math_feedback(comment, steps, {"version": "older", "assessment": {"comment": "Other"},
        "method_review": {"status": "reassessed"}}, "zh") == (comment, steps)


def test_independent_summary_is_not_changed_by_substring_replacement():
    old = {"provider": "a", "score": 8, "max_score": 10, "comment": "Old text", "confidence": .9}
    new = {**old, "comment": "Final text"}
    assert _combined_comment("Independent explanation mentioning Old text", [old], [new]) == "Independent explanation mentioning Old text"
    assert _combined_comment("Old text", [old], [new]) == "Final text"
    prefix = "【a（8.0/10, conf=0.90）】\n"
    assert _combined_comment(prefix + "Old text", [old], [new]) == prefix + "Final text"
