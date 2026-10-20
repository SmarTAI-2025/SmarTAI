"""Authored model-boundary replies for the new schema, separate from the oracle.

Do not read case['expected'] here. Plans are constructed from question-only
rules; grade allocations below are independent constants. The production plan
compiler, ledger, tools, agent and review projection all remain real.
"""
from copy import deepcopy


# Literal teacher clauses and maximum allocations, deliberately not derived
# from expected scores or from the number of student lines.
POINT_RULES = {
    "C01": [("移项4分", 4), ("两边除以2的方法4分", 4), ("最终值2分", 2)],
    "C02": [("移项4分", 4), ("两边除以2的方法4分", 4), ("最终值2分", 2)],
    "C03": [("移项4分", 4), ("两边除以2的方法4分", 4), ("最终值2分", 2)],
    "C18": [("x²项3分", 3), ("2x项4分", 4), ("常数1占3分", 3)],
    "C19": [("化简x+1占8分", 8), ("x≠1占2分", 2)],
    "C23": [("公式F/m占4分", 4), ("数值3占4分", 4), ("加速度单位占2分", 2)],
    "C24": [("原函数x²占8分", 8), ("任意常数C占2分", 2)],
    "C25": [("原函数x²占8分", 8), ("任意常数占2分", 2)],
    "P01": [("可用任何有效方法：表达奇数结构4分", 4), ("推出平方为奇数6分", 6)],
    "P02": [("指出0处未定义5分", 5), ("区间连续条件失败5分", 5)],
    "P03": [("表示n=2k占4分", 4), ("有效推出n²=2整数占6分", 6)],
    "P04": [("合理分情况4分", 4), ("解x=0占3分", 3), ("解x=1占3分", 3)],
    "P05": [("重力做功与势能关系5分", 5), ("联系动能定理推出总能量不变5分", 5)],
    "P06": [("正向5分", 5), ("反向5分", 5)],
    "K01": [("合力为零4分", 4), ("静止或匀速直线运动6分", 6)],
    "K02": [("合力为零4分", 4), ("静止或匀速直线运动6分", 6)],
    "K04": [("连续不保证可导4分", 4), ("有效反例并定位6分", 6)],
}
AWARDS = {
    "C01": [0, 0, 2], "C02": [0, 0, 2], "C03": [4, 4, 0],
    "C18": [3, 4, 3], "C19": [8, 0], "C23": [4, 4, 0],
    "C24": [8, 0], "C25": [8, 2], "P01": [4, 6], "P02": [5, 5],
    "P03": [0, 0], "P04": [0, 0, 3], "P05": [5, 5], "P06": [0, 5],
    "K01": [4, 6], "K02": [0, 6], "K04": [4, 6],
}
HOLISTIC = {
    "C04": 10, "C05": 3, "C06": 6, "C07": 0, "C10": 10, "C11": 10,
    "C12": 0, "C13": 0, "C14": 10, "C15": 10, "C16": 10, "C17": 10,
    "C20": 0, "C21": 10, "C22": 10, "C26": 10, "C27": 10, "C28": 10,
    "K03": 4, "K05": 0, "K06": 0,
}


def plan_reply(case):
    """Question-only reply; called before any grading reply is requested."""
    id = case["id"]
    mode, items = "holistic", []
    if id in POINT_RULES:
        mode = "points"
        items = [dict(id=f"p{i}", description=text, points=points, source="provided")
                 for i, (text, points) in enumerate(POINT_RULES[id])]
    elif id in {"C08", "C09"}:
        mode = "deductions"
        items = [dict(id="error", description=(
            "首次数值错误扣1分" if id == "C08" else "每个独立数值错误扣1分"),
                      points=1, source="provided", max_occurrences=1 if id == "C08" else None)]
    return dict(effective_criterion=case["problem"]["criterion"],
                routes=[dict(id="main", description="按本题明确标准计分",
                             applies_when="所有本题作答", mode=mode, items=items)],
                follow_through="依本题原标准处理首错、正确承接和后续独立错误。",
                numerical_policy="依原题的精度、定义域、单位和任意常数要求。",
                advisories=[])


def assessment_reply(case):
    id = case["id"]
    claims = ([dict(id="s1", quote=case["answer"], verdict="unknown",
                    reason="对照学生原文与本题标准逐项判断。", item_ids=[])]
              if case["answer"] else [])
    draft = dict(route_id="main", route_reason="本题仅有一条适用路线。",
                 claims=claims, awards=[], deductions=[], checks=[],
                 confidence=.95, comment="依据本题标准计分。",
                 rationale=case["replay"]["response"]["comment"])
    if id in AWARDS:
        draft["awards"] = [
            dict(item_id=f"p{i}", score=value, claim_ids=["s1"] if value else [],
                 reason=f"{POINT_RULES[id][i][0]}；本项依据原文计{value}分。")
            for i, value in enumerate(AWARDS[id])]
        for c in claims:
            c["item_ids"] = [f"p{i}" for i in range(len(AWARDS[id]))]
    elif id in {"C08", "C09"}:
        lines = case["answer"].splitlines()
        draft["claims"] = [
            dict(id="s1", quote=lines[0], verdict="refuted", reason="6/2算成2。",
                 item_ids=["error"]),
            dict(id="s2", quote=lines[1], depends_on=["s1"],
                 verdict="supported" if id == "C08" else "refuted",
                 reason="承接前值的乘法正确。" if id == "C08" else "2*2又算成5。",
                 item_ids=["error"])]
        draft["deductions"] = [
            dict(error_id="e1", rule_id="error", claim_id="s1", reason="首个独立除法错误。")]
        if id == "C09":
            draft["deductions"].append(dict(error_id="e2", rule_id="error", claim_id="s2",
                                            reason="新的独立乘法错误，不是首错自然延续。"))
    else:
        draft["score"] = HOLISTIC[id]
    specification = case.get("verification")
    request = deepcopy(specification["request"]) if specification else None
    if id in {"C01", "C02", "C03"}:
        request = {"student_expression": "3" if id == "C03" else "2",
                   "reference_expression": "2", "comparison": "numeric"}
    if id in {"C26", "C27"}:
        request = {"student_expression": "4", "reference_expression": "4", "comparison": "numeric"}
    if request and claims:
        draft["checks"] = [dict(claim_id="s1", context_quote=case["problem"]["stem"], request=request)]
    return draft
