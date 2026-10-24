"""Synthetic adversarial inputs, authored before running the reliability baseline.

``wire`` describes model replies, including deliberately WRONG replies. ``oracle``
is independent acceptance policy. The provider must never consult the oracle.
These cases are program-path tests, not teacher labels or model accuracy data.
The v1 envelopes target main@109acaec; adapt envelopes only after CORE handoff.
"""
from copy import deepcopy
import hashlib
import json

BASE = "109acaec30db917b52c0ca827f15a4b5830647e8"


def item(id, text, points, source="provided", **kw):
    return dict(id=id, description=text, points=points, source=source, **kw)


def plan(criterion, items=(), mode="points", **route):
    return dict(effective_criterion=criterion, routes=[dict(
        id="main", description="依据本题原标准", applies_when="本题全部作答",
        mode=mode, items=list(items), **route)], follow_through="遵守原标准的错误延续政策。",
        numerical_policy="只用原题给出的条件和精度。", advisories=[])


def claim(id, quote, items=(), **kw):
    return dict(id=id, quote=quote, item_ids=list(items), verdict="unknown",
                reason="合成模型判断，须由程序验证来源与账本。", **kw)


def award(id, score, claims=(), reason="按本项标准计分。"):
    return dict(item_id=id, score=score, claim_ids=list(claims), reason=reason)


def draft(claims, awards=(), deductions=(), checks=(), **kw):
    return dict(route_id="main", route_reason="本题计分路线。", claims=list(claims),
                awards=list(awards), deductions=list(deductions), checks=list(checks),
                confidence=.99, rationale="合成模型的具体计分判断。", comment="依据各项计分。", **kw)


def check(stem, student, reference, **kw):
    return dict(claim_id="s1", context_quote=stem, request=dict(
        student_expression=student, reference_expression=reference, **kw))


def case(id, stem, answer, criterion, wire_plan, assessment, *, group="math/numeric",
         reference="42", type="计算题", oracle=None, **wire):
    return dict(id=id, group=group, problem=dict(q_id=id, number="1", type=type,
        stem=stem, reference_answer=reference, criterion=criterion, max_score=10),
        answer=answer, wire=dict(plan=wire_plan, assessment=assessment, **wire),
        oracle=oracle or {})


RESULT_RULE = "结果正确10分"
RESULT_PLAN = plan(RESULT_RULE, [item("result", RESULT_RULE, 10)])
STEM = "计算6*7，要求精确值。"
GOOD = draft([claim("s1", "42", ["result"])], [award("result", 10, ["s1"])])
CASES = []

# Raw JSON -> parser -> actual subprocess verifier -> feedback -> ledger -> DTO.
wrong = draft([claim("s1", "41", ["result"])], [award("result", 10, ["s1"])],
              checks=[check(STEM, "41", "42", comparison="numeric")])
corrected = deepcopy(wrong)
corrected["claims"][0]["verdict"] = "refuted"
corrected["awards"][0].update(score=0, reason="41不等于42，结果项不得分。")
CASES.append(case("R01_real_tool_feedback", STEM, "41", RESULT_RULE, RESULT_PLAN, wrong,
    feedback=corrected, fence=True,
    oracle=dict(score=0, mode="points", structured=True, tool="refuted", feedback=1)))

tex_stem = r"核对数值：\(2+2\)，答案必须精确。"
tex_answer = r"\(4\)"
CASES.append(case("R02_raw_tex_claim_context", tex_stem, tex_answer, RESULT_RULE, RESULT_PLAN,
    draft([claim("s1", tex_answer, ["result"])], [award("result", 10, ["s1"])],
          checks=[check(tex_stem, "4", "4", comparison="numeric")]), reference=r"\(4\)",
    oracle=dict(score=10, mode="points", structured=True, tool="supported", source_exact=True)))

# Scheduling opportunity is independent of the fixture's checks=[] declaration.
for id, group, stem, answer, reference in [
    ("R03_numeric_checks_empty", "math/numeric", "求2+2的值。", "4", "4"),
    ("R04_symbolic_checks_empty", "math/symbolic", "展开(x+1)^2。", "x^2+2*x+1", "(x+1)^2"),
    ("R05_physics_units_empty", "physics/numeric", "质量2kg受力6N，求加速度。", "3 m/s^2", "3 m/s^2"),
    ("R06_pi_constant", "math/numeric", "计算sin(pi/2)。", "1", "1"),
    ("R07_e_constant", "math/numeric", "计算log(e)。", "1", "1"),
    ("R08_i_constant", "math/numeric", "计算i^2。", "-1", "-1"),
]:
    CASES.append(case(id, stem, answer, RESULT_RULE, RESULT_PLAN,
        draft([claim("s1", answer, ["result"])], [award("result", 10, ["s1"])]),
        group=group, reference=reference,
        oracle=dict(score=10, mode="points", structured=True, applicable=True, min_tools=1)))

proof_rule = "有效证明10分；允许任意正确证明方法。"
proof_text = "设奇数为2k+1，则其平方为2(2k²+2k)+1，仍为奇数。"
CASES.append(case("R09_alternative_proof", "证明奇数的平方是奇数。", proof_text, proof_rule,
    plan(proof_rule, [item("proof", "有效证明10分", 10)]),
    draft([claim("s1", proof_text, ["proof"])], [award("proof", 10, ["s1"])]),
    group="math/proof", type="证明题", reference="可用模2同余证明。",
    oracle=dict(score=10, mode="points", structured=True, max_tools=0)))

work_rule = "有效过程8分；结果2分。"
work_plan = plan(work_rule, [item("method", "有效过程8分", 8), item("result", "结果2分", 2)])
for id, answer, claims, awards, expected in [
    ("R10_correct_result_wrong_work", "2x=6，除以3得x=3。",
     [claim("s1", "除以3", ["method"]), claim("s2", "x=3", ["result"])],
     [award("method", 0, reason="除数应为2。"), award("result", 2, ["s2"])], 2),
    ("R11_correct_work_wrong_final", "2x=6，两边除以2，x=2。",
     [claim("s1", "两边除以2", ["method"]), claim("s2", "x=2", ["result"])],
     [award("method", 8, ["s1"]), award("result", 0, reason="6/2不等于2。")], 8),
]:
    CASES.append(case(id, "解2x=6并写出方法。", answer, work_rule, work_plan,
        draft(claims, awards), reference="x=3",
        oracle=dict(score=expected, mode="points", structured=True)))

CASES.append(case("R12_award_claim_mapping", "解2x=6并写出方法。", "x=3", work_rule, work_plan,
    draft([claim("s1", "x=3", ["result"])],
          [award("method", 8, ["s1"]), award("result", 2, ["s1"])]),
    reference="x=3", oracle=dict(reject_bad_mapping=True)))

ded_rule = "每个错误扣1分；同源后续错误不重复扣分。"
ded_plan = plan(ded_rule, [item("error", "每个错误扣1分", 1, max_occurrences=None)], "deductions")
ded_claims = [claim("s1", "6/2=2", ["error"]),
              claim("s2", "2*2=4", ["error"], depends_on=["s1"])]
deductions = [dict(error_id="e1", rule_id="error", claim_id="s1", reason="除法首错。"),
              dict(error_id="e2", rule_id="error", claim_id="s2", caused_by="s1", reason="首错的纯后果。")]
CASES.append(case("R13_follow_through", "先计算6/2，再将结果乘2。", "6/2=2；2*2=4", ded_rule,
    ded_plan, draft(ded_claims, deductions=deductions), reference="3；6",
    oracle=dict(score=9, mode="deductions", structured=True)))
ind = deepcopy(deductions)
ind[1].pop("caused_by")
ind[1]["reason"] = "2*2又算为5，是独立乘法错误。"
CASES.append(case("R14_independent_second_error", "先计算6/2，再将结果乘2。", "6/2=2；2*2=5",
    ded_rule, ded_plan, draft([ded_claims[0], claim("s2", "2*2=5", ["error"], depends_on=["s1"])],
    deductions=ind), reference="3；6", oracle=dict(score=8, mode="deductions", structured=True)))

for id, rule, desc, amount, source, extra, expected in [
    ("R15_chinese_provided", "每错扣一分", "每错扣一分", 1, "provided", {}, 9),
    ("R16_chinese_ai_tamper", "每错扣一分", "每错扣一分", 9, "ai", {}, None),
    ("R17_arabic_ai_tamper", "每错扣1分", "每错扣1分", 9, "ai", {}, None),
    ("R18_question_total", "每题10分，每错扣1分", "每错扣1分", 1, "provided", {}, 9),
    ("R19_english_count_cap", "Deduct 1 point per error, at most 3 times.",
     "Deduct 1 point per error, at most 3 times.", 1, "provided", {"max_occurrences": 3}, 9),
]:
    CASES.append(case(id, STEM, "41", rule, plan(rule, [item("error", desc, amount, source, **extra)], "deductions"),
        draft([claim("s1", "41", ["error"])], deductions=[
            dict(error_id="e1", rule_id="error", claim_id="s1", reason="数值错误。")]),
        oracle=(dict(reject_wrong_amount=True) if expected is None else
                dict(score=expected, mode="deductions", structured=True))))

exempt_rule = "每处大错扣2分，小错扣0分。"
exempt_plan = plan(exempt_rule, [item("major", "每处大错扣2分", 2)], "deductions",
    exemptions=[dict(id="minor", description="轻微笔误免扣", source_quote="小错扣0分")])
CASES.append(case("R20_minor_exemption", "写出牛顿第二定律并解释。", "F=ma。符号F误写为f一次。", exempt_rule,
    exempt_plan, draft([claim("s1", "符号F误写为f一次。", ["minor"])], deductions=[
        dict(error_id="e1", rule_id="minor", claim_id="s1", reason="仅符号笔误，按明确小错豁免。")]),
    group="physics/concept", type="简答题", reference="F=ma",
    oracle=dict(score=10, mode="deductions", structured=True)))

# A valid and a direct expert must still be jointly reviewed when scores disagree.
eight = draft([claim("s1", "42", ["method", "result"])],
    [award("method", 6, ["s1"]), award("result", 2, ["s1"])])
bad = deepcopy(eight)
bad["claims"][0]["quote"] = "student did not write this"
CASES.append(case("R21_mixed_direct_review", STEM, "42", work_rule, work_plan, eight,
    experts=[eight, bad], direct_score=2, oracle=dict(min_review=1, available=True)))

CASES.append(case("R22_refuted_feedback_parse_failure", STEM, "41", RESULT_RULE, RESULT_PLAN, wrong,
    feedback="{broken JSON", repair="{still broken JSON",
    oracle=dict(score=10, structured=True, tool="refuted", feedback_failed=True, conflict_advice=True)))

other = deepcopy(wrong)
other["awards"][0]["score"] = 8
wrong_review = deepcopy(wrong)
wrong_review["needs_review"] = True
other["needs_review"] = True
CASES.append(case("R23_shared_review_budget", STEM, "41", RESULT_RULE, RESULT_PLAN, wrong_review,
    experts=[wrong_review, other], oracle=dict(available=True, max_review=1, min_review=1)))

for id, stem, answer, reference, request in [
    ("R24_invented_domain", "对所有实数x化简sqrt(x^2)。", "x", "Abs(x)",
     dict(student_expression="x", reference_expression="Abs(x)", assumptions={"x": {"positive": True}})),
    ("R25_invented_tolerance", STEM, "41", "42", dict(student_expression="41", reference_expression="42",
     comparison="numeric", allow_approximation=True, absolute_tolerance=2)),
    ("R26_unrelated_tool_expression", STEM, "41", "42", dict(student_expression="1", reference_expression="1")),
]:
    CASES.append(case(id, stem, answer, RESULT_RULE, RESULT_PLAN,
        draft([claim("s1", answer, ["result"])], [award("result", 10, ["s1"])],
              checks=[dict(claim_id="s1", context_quote=stem, request=request)]), reference=reference,
        group="math/symbolic" if id.startswith("R24") else "math/numeric",
        oracle=dict(no_supported_evidence=True)))

missing_rule = "正向证明5分，反向证明5分。"
missing_plan = plan(missing_rule, [item("forward", "正向证明5分", 5), item("backward", "反向证明5分", 5)])
missing = draft([claim("s1", "由A推出B。", ["forward"])],
    [award("forward", 5, ["s1"]), award("backward", 0, reason="未证明B推出A。")])
missing["missing_obligations"] = [dict(item_id="backward", source_quote="反向证明5分", checked_answer="由A推出B。")]
CASES.append(case("R27_missing_obligation", "证明A当且仅当B。", "由A推出B。", missing_rule, missing_plan,
    missing, group="math/proof", type="证明题", reference="双向证明。",
    oracle=dict(score=5, mode="points", structured=True, missing_obligation=True)))
concept_rule = "合力为零4分；保持静止或匀速直线运动6分。"
concept_plan = plan(concept_rule, [item("force", "合力为零4分", 4),
    item("motion", "保持静止或匀速直线运动6分", 6)])
CASES.append(case("R28_explicit_contradiction", "说明物体处于平衡时的状态。", "合力不为零；保持静止。",
    concept_rule, concept_plan, draft([claim("s1", "合力不为零", ["force"]), claim("s2", "保持静止", ["motion"])],
    [award("force", 0, reason="与平衡条件矛盾。"), award("motion", 6, ["s2"])]),
    group="physics/concept", type="简答题", reference="合力为零，静止或匀速直线运动。",
    oracle=dict(score=6, mode="points", structured=True, max_tools=0)))

CASES.append(case("R29_unknown_tool", "给定特殊函数f，求f(1)。", "f(1)", RESULT_RULE, RESULT_PLAN,
    draft([claim("s1", "f(1)", ["result"])], [award("result", 10, ["s1"])],
    checks=[check("给定特殊函数f，求f(1)。", "f(1)", "f(1)")]), reference="f(1)", group="math/symbolic",
    oracle=dict(score=10, structured=True, tool="unknown", feedback=0)))

for id, fault in [("R30_provider_failure", "permanent"), ("R31_cancelled", "cancel")]:
    CASES.append(case(id, STEM, "42", RESULT_RULE, RESULT_PLAN, GOOD, fault=fault,
        oracle=dict(available=False, cancelled=fault == "cancel")))

# Bad quotes are adversarial inputs. Rejection is sufficient, not a score oracle.
for id, source, quoted in [
    ("R32_changed_exponent", "x^2=1", "x^3=1"),
    ("R33_changed_quantifier", "存在x使P(x)成立", "任意x使P(x)成立"),
    ("R34_invented_condition", "x属于实数", "x>0"),
    ("R35_compressed_chain", "A=B=C", "A=C"),
    ("R36_digit_boundary", "1 2", "12"),
]:
    CASES.append(case(id, "核对所写论断。", source, RESULT_RULE, RESULT_PLAN,
        draft([claim("s1", quoted, ["result"])], [award("result", 10, ["s1"])]),
        group="math/proof", type="证明题", oracle=dict(reject_bad_quote=True)))

CASES.append(case("R37_local_quote_repair", "核对两步计算。", "2+2=4；3+3=6", work_rule, work_plan,
    draft([claim("s1", "2+2=4", ["method"]), claim("s2", "3+3=7", ["result"])],
          [award("method", 8, ["s1"]), award("result", 2, ["s2"])]),
    source_repair=draft([claim("s1", "2+2=4", ["method"]), claim("s2", "3+3=6", ["result"])],
          [award("method", 8, ["s1"]), award("result", 2, ["s2"])]), reference="4；6",
    oracle=dict(score=10, mode="points", structured=True, source_repair=1)))

CASES.append(case("R38_holistic_without_claims", "解释为何该论证有效。", "因为前提推出结论。", "综合判断论证质量。",
    plan("综合判断论证质量。", mode="holistic"), draft([], score=10), group="math/proof", type="证明题",
    oracle=dict(reject_empty_claims=True)))

energy = "只有重力做功时，动能增量等于重力功；重力功等于势能减少量，所以机械能不变。"
proof_physics_rule = "正确联系动能定理和重力势能10分"
CASES.append(case("R39_physics_energy_proof", "证明只有重力做功时机械能守恒。", energy,
    proof_physics_rule, plan(proof_physics_rule, [item("proof", proof_physics_rule, 10)]),
    draft([claim("s1", energy, ["proof"])], [award("proof", 10, ["s1"])]),
    group="physics/proof", type="证明题", reference="动能和重力势能之和不变。",
    oracle=dict(score=10, mode="points", structured=True, max_tools=0)))

concept_math = "连续不一定可导，例如绝对值函数在0处连续但不可导。"
math_concept_rule = "说明连续不保证可导4分；给出正确反例6分。"
CASES.append(case("R40_math_concept", "连续是否一定可导？给出反例。", concept_math, math_concept_rule,
    plan(math_concept_rule, [item("concept", "说明连续不保证可导4分", 4), item("example", "给出正确反例6分", 6)]),
    draft([claim("s1", "连续不一定可导", ["concept"]),
           claim("s2", "绝对值函数在0处连续但不可导", ["example"])],
          [award("concept", 4, ["s1"]), award("example", 6, ["s2"])]),
    group="math/concept", type="简答题", reference="绝对值函数是反例。",
    oracle=dict(score=10, mode="points", structured=True)))

source_rule = r"写出\(x^2=1\)得10分"
source_plan = plan(source_rule, [item("result", "写出平方方程，10分", 10,
                                    source_quote=source_rule)])
CASES.append(case("R41_plan_display_source_separation", "写出x的平方等于1。", "x^2=1", source_rule,
    source_plan, draft([claim("s1", "x^2=1", ["result"])], [award("result", 10, ["s1"])]),
    group="math/symbolic", reference="x^2=1", oracle=dict(score=10, mode="points", structured=True)))

abstract = "任意两元素为g^m与g^n，其乘积为g^(m+n)=g^(n+m)，所以交换。"
abstract_rule = "证明任意两元素交换10分"
CASES.append(case("R42_abstract_not_applicable", "证明循环群是阿贝尔群。", abstract, abstract_rule,
    plan(abstract_rule, [item("proof", abstract_rule, 10)]),
    draft([claim("s1", abstract, ["proof"])], [award("proof", 10, ["s1"])]),
    group="math/proof", type="证明题", reference="群元素写成同一生成元的幂。",
    oracle=dict(score=10, mode="points", structured=True, max_tools=0, not_applicable=True)))


def corpus_hash():
    return hashlib.sha256(json.dumps(CASES, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
