"""Recreate the derived files of the frozen U1 V2 packet in a new directory."""

import argparse
import csv
import json
from pathlib import Path


from .fixture_sources import build_fixtures


PACKET = Path(__file__).resolve().parents[3] / "manual_test_samples/programming_u1_20261028"


POLICIES = {
    "execute_after_contract_confirmation": "教师确认合同后应执行",
    "do_not_execute_missing": "实际缺交：不执行",
    "do_not_execute_missing_entry": "已确认缺本题函数：不执行",
    "parse_only_no_execution": "只做语法检查，不执行",
    "compile_only_no_execution": "只编译，不运行",
    "do_not_execute_until_mapping_confirmed": "先复核入口/归属，不执行",
    "do_not_execute_platform_unavailable": "平台不可用：不执行、未评分",
    "do_not_execute_until_case_confirmed": "预期冲突：先复核用例，不执行",
    "future_oci_compile_and_execute": "后续产品支持后应在OCI编译执行；本次仅Mac样本验证",
    "do_not_execute_explicit_contract_violation": "明确入口/签名违规：不改源码、不执行，按草案人工复核",
}

HEADERS = [
    "提交ID",
    "题目ID",
    "来源文件",
    "材料来源",
    "满分",
    "AI建议分_待确认",
    "扣分理由",
    "应否执行",
    "本次Mac验证事实",
    "复核条件",
    "平台及用例故障处理",
    "教师确认分",
    "教师确认意见",
    "确认人角色或编号",
    "确认日期",
    "确认材料版本",
    "确认范围",
]

REVIEW_INTRO = """# 待教师确认的判分表

**以下是AI建议，不是教师金标准；所有教师确认栏为空。不得据本表直接发布真实学生成绩。**

“本次Mac验证事实”是构造样本的独立运行或静态检查；不代表SmarTAI产品、真实OCI或真实模型已执行。本地测试没有自动算分，建议分由人工撰写的草案列明。

[可填写CSV](MANUAL_GRADING_REVIEW.csv)保留教师最终分、意见、角色/编号、日期和版本六列。请先确认[教师规则草案](QUESTIONS_AND_RULES.md)，再逐行确认；分歧保留，不覆盖AI原建议。
"""

REVIEW_FOOTER = """## 必须单独确认的判分分歧

- S02-F01：5/5样例通过但违反O(n)动态规划要求，草案建议7分。
- S09-F02：4/5通过，但重复值规则占4分，草案建议6分。
- S14-S01：4/5通过，结果门槛草案建议0分；不能直接给8分。
- S05-F01语法错误的过程分草案为3；S16-S01语法错误为0，差异来自不同题目规则，均待教师确认。
- S10同一helper对求和不造成数值错误、对计数造成零值误计，不能跨题连带扣分。
- S17平台不可用、S18错误预期、S12入口不符及S19题号冲突均保持未评分，不能强填0。
- 实际缺交只有在原始提交清单和来源确认后才可建议0；上传/识别失败保持未评分。


- S21-F04/S21-F05使用同份combine源码：教师明确要求add时建议8分；教师没有规定入口时建议10分。不是按函数名固定扣分。
- S22-F04仅一个pair参数违反明确双参数签名，建议只扣接口2分；不靠错误调用制造算法失败。
- S23两题共同依赖完整import、常量及helper；提取丢失上下文造成的异常不属于学生错误。"""

QUESTIONS_INTRO = """# 题目、教师规则草案与标答

**全部完整合同和规则均为构造补充、待教师确认；不是教师金标准。** PY-F01仅原题一句来自仓库旧测试题，其余均为本次构造。

函数题 .in.txt 是本地验算的位置参数数组，例如 [[-2,0,5]] 表示一个列表参数；.expected.txt 是返回值。stdin题文件是原样标准输入/输出。questions.json是样本索引，不是产品API导入合同。

新增双整数题还提供可直接粘贴/上传的[自然语言纯文本资料](teacher_inputs/two_integer_cases.txt)。它与JSON参数文件分开保留；[静态对应预期](teacher_inputs/acceptance_expectations.json)只是验收依据，不实现后台适配。
"""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def case_filename(value):
    if Path(value).name != value or value in {"", ".", ".."}:
        raise ValueError(f"Invalid case path component: {value!r}")
    return value


def write_cases(output, questions):
    for question in questions:
        for case in question["cases"]:
            if question["mode"] == "function_call":
                inputs = json.dumps(case["input"], ensure_ascii=False) + "\n"
                expected = str(case["expected"]) + "\n"
            else:
                inputs = case["input"]
                expected = case["expected"]
            case_path = output / "cases" / case_filename(question["q_id"]) / case_filename(case["case_id"])
            write_text(case_path.with_suffix(".in.txt"), inputs)
            write_text(case_path.with_suffix(".expected.txt"), expected)


def write_questions(output, questions):
    sections = [QUESTIONS_INTRO]
    for q in questions:
        entry = q["function_name"] or "main / stdin"
        if q["q_id"] == "PY-F05":
            entry = "教师未规定；本地验算固定combine(a,b)，不构成教师要求"
        sections.extend(
            [
                f"## {q['q_id']} {q['title']}",
                "",
                f"满分：{q['max_score']}。语言：{q['language']}；模式：{q['mode']}；入口说明：{entry}。",
                "",
                q["stem"],
                "",
                "教师规则草案：",
                "",
            ]
        )
        sections.extend("- " + rule for rule in q["teacher_rules_draft"])
        if q.get("teacher_input_file"):
            sections.extend(
                ["", f"原始纯文本测试资料：[同源文件]({q['teacher_input_file']})（本次构造，未确认）。"]
            )
        sections.extend(
            [
                "",
                f"标答：[源码]({q['reference']})（本次构造，待教师确认）",
                "",
                "| 用例 | 本地验算输入 | 预期输出/返回值 | 目的 |",
                "|---|---|---|---|",
            ]
        )
        for case in q["cases"]:
            inputs = json.dumps(case["input"], ensure_ascii=False)
            expected = json.dumps(case["expected"], ensure_ascii=False)
            sections.append(f"| {case['case_id']} | `{inputs}` | `{expected}` | {case['note']} |")
        sections.append("")
    write_text(output / "QUESTIONS_AND_RULES.md", "\n".join(sections).rstrip() + "\n")


def observation_text(observation):
    if observation["status"] == "observed":
        return (
            f"Mac本地已执行，{observation['passed']}/{observation['total']}例符合预期输出；"
            f"运行异常{observation['runtime_errors']}例"
        )
    return {
        "syntax_error": "Mac语法检查确认SyntaxError；未执行",
        "compile_error": "Mac编译检查确认缺分号；未运行",
        "not_executed_by_policy": "本轮未执行（按情景策略）",
    }[observation["status"]]


def write_review(output, questions, submissions, report):
    observations = {row["submission_id"]: row for row in report["submissions"]}
    maxima = {q["q_id"]: q["max_score"] for q in questions}
    by_question = {qid: [] for qid in maxima}
    with (output / "MANUAL_GRADING_REVIEW.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(HEADERS)
        for s in submissions:
            sid, qid = s["submission_id"], s["q_id"]
            source = s["source_file"]
            score = s["suggested_score"] if s["suggested_score"] is not None else "未评分"
            policy = POLICIES[s["execution_policy"]]
            observed = observation_text(observations[sid])
            writer.writerow(
                [
                    sid,
                    qid,
                    source or "无文件（构造实际缺交）",
                    "构造样例，非真实学生",
                    maxima[qid],
                    score,
                    s["deduction_reason"],
                    policy,
                    observed,
                    s["review_condition"],
                    s["platform_failure_rule"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            label = f"[{sid}]({source})" if source else sid
            cells = [
                label,
                score,
                s["deduction_reason"],
                policy + "；" + observed,
                s["review_condition"],
                "待确认",
            ]
            by_question[qid].append(
                "| " + " | ".join(str(c).replace("|", r"\|").replace("\n", "<br>") for c in cells) + " |"
            )
    sections = [REVIEW_INTRO]
    for qid, rows in by_question.items():
        sections.extend(
            [
                f"## {qid}（满分{maxima[qid]}）",
                "",
                "| 提交 | AI建议分 | 扣分理由 | 应否执行 / 本次事实 | 复核条件 | 教师确认分 |",
                "|---|---:|---|---|---|---|",
                *rows,
                "",
            ]
        )
    write_text(output / "MANUAL_GRADING_REVIEW.md", "\n".join([*sections, REVIEW_FOOTER]) + "\n")


def generate(packet, output):
    output = output.resolve()
    if output.is_relative_to(packet.resolve()):
        raise ValueError("Choose a new output directory outside the frozen packet.")
    report = read_json(packet / "validation/local_fixture_results_v2.json")
    if report["result"] != "passed" or report["score_computation"] is not False:
        raise ValueError("Expected the reviewed V2 fixture report")
    output.mkdir(exist_ok=False)

    def put(name, content):
        write_text(output / name, content)

    def write_json(name, obj):
        put(name, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    questions, submissions = build_fixtures(put, write_json)
    write_cases(output, questions)
    write_questions(output, questions)
    write_review(output, questions, submissions, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for the derived files")
    generate(PACKET, parser.parse_args().output)
