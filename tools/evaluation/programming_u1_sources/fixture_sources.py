"""Authored U1 seeds and fixture transformations from the V1/V2 packet builders."""

import copy


def build_fixtures(write_source, write_json):
    """Write fixtures via the caller's output functions; never run the toy programs."""
    fib = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"
    shared = (
        "def count_positive(nums):\n"
        "    return sum(1 for x in nums if is_positive(x))\n"
        "\n"
        "def is_positive(x):\n"
        "    return x > 0\n"
        "\n"
        "def sum_positive(nums):\n"
        "    return sum(x for x in nums if is_positive(x))\n"
    )
    stdin = (
        "import sys\n"
        "values = list(map(int, sys.stdin.read().split()))\n"
        "n = values[0]\n"
        "print(sum(values[1:1+n]))\n"
    )
    c = (
        "#include <stdio.h>\n"
        "int main(void) {\n"
        "    int n; long long x, total = 0;\n"
        '    if (scanf("%d", &n) != 1) return 1;\n'
        "    for (int i = 0; i < n; ++i) {\n"
        '        if (scanf("%lld", &x) != 1) return 1;\n'
        "        total += x;\n"
        "    }\n"
        '    printf("%lld\\n", total);\n'
        "    return 0;\n"
        "}\n"
    )
    cpp = (
        "#include <iostream>\n"
        "int main() {\n"
        "    int n; long long x, total = 0;\n"
        "    if (!(std::cin >> n)) return 1;\n"
        "    for (int i = 0; i < n; ++i) {\n"
        "        if (!(std::cin >> x)) return 1;\n"
        "        total += x;\n"
        "    }\n"
        "    std::cout << total << '\\n';\n"
        "    return 0;\n"
        "}\n"
    )
    questions = []

    def question(qid, title, language, mode, entry, stem, rules, ref, code, cases, origin="constructed"):
        write_source("references/" + ref, code)
        out = []
        for i, (inp, expected, note) in enumerate(cases, 1):
            cid = f"{qid}-T{i:02d}"
            out.append(
                dict(
                    case_id=cid,
                    input=inp,
                    expected=expected,
                    note=note,
                    provenance="AI_CONSTRUCTED_PENDING_TEACHER_CONFIRMATION",
                    input_file=f"cases/{qid}/{cid}.in.txt",
                    expected_file=f"cases/{qid}/{cid}.expected.txt",
                )
            )
        questions.append(
            dict(
                q_id=qid,
                title=title,
                language=language,
                mode=mode,
                function_name=entry,
                max_score=10,
                provenance=origin,
                teacher_rules_status="AI_PROPOSED_PENDING_TEACHER_CONFIRMATION",
                teacher_confirmed=False,
                stem=stem,
                teacher_rules_draft=rules,
                reference="references/" + ref,
                cases=out,
            )
        )

    question(
        "PY-F01",
        "斐波那契：动态规划方法约束",
        "python",
        "function_call",
        "fib",
        "实现 def fib(n): 返回 F_n。构造补充约定 F_0=0、F_1=1，整数 0≤n≤30；必须动态规划，时间 O(n)，可用滚动状态。原仓库仅明确函数计算第n项、动态规划及 O(n)。",
        [
            "普通项结果正确4分（n≥2）；基例n=0、1作为整体2分（两者都正确才给这2分）；动态规划且O(n)方法3分；指定fib(n)返回整数1分。",
            "各维度独立、同一问题不重复扣；样例通过率仅作证据，不按比例结算。递归未记忆化即使全部小样例正确，也不给方法3分。",
            "确认真实缺交为0；语法错误可对清楚可辨的正确DP思路给3分，其余不给。此语法错误过程分规则须教师特别确认。",
        ],
        "fib.py",
        fib,
        [
            ([0], 0, "零边界"),
            ([1], 1, "一边界"),
            ([2], 1, "递推初项"),
            ([5], 5, "普通项"),
            ([10], 55, "普通项"),
        ],
        "legacy_repository_question_with_constructed_completion",
    )
    sum_cases = [
        ([[]], 0, "空序列"),
        ([[1, 2, 3]], 6, "普通正数"),
        ([[-2, 0, 5]], 5, "负数与零"),
        ([[2, 2, 3]], 7, "重复值必须逐次贡献"),
        ([[-4, -1]], 0, "全负数"),
    ]
    count_cases = [
        ([[]], 0, "空序列"),
        ([[1, 2, 3]], 3, "普通正数"),
        ([[-2, 0, 5]], 1, "零不能计入"),
        ([[2, 2, 3]], 3, "重复值各计一次"),
        ([[-4, -1]], 0, "全负数"),
    ]
    question(
        "PY-F02",
        "正数求和",
        "python",
        "function_call",
        "sum_positive",
        "实现 def sum_positive(nums): 返回列表中严格大于0的整数之和；重复值逐个计入，空列表返回0；长度0..100，元素-1000..1000。与PY-F03可共用一份完整源码及helper。",
        [
            "重复值贡献正确4分；正负筛选的数值贡献正确2分（零贡献为0，允许等价的>=0实现）；非重复普通求和正确2分；约定函数接口和空序列返回正确2分。",
            "sum(set(...))丢重复值只扣重复值4分，不能因为4/5通过而给8分；确认缺交0分。",
        ],
        "positive_sum.py",
        "def sum_positive(nums):\n    return sum(x for x in nums if x > 0)\n",
        sum_cases,
    )
    question(
        "PY-F03",
        "正数计数",
        "python",
        "function_call",
        "count_positive",
        "实现 def count_positive(nums): 统计严格大于0的整数个数；重复元素分别计数；空列表返回0；输入范围同PY-F02。",
        [
            "遍历计数及重复值语义5分；严格大于0、排除零的边界3分；指定接口和空序列2分。",
            "同一helper对不同题的影响按题判断；把 >0 写成 >=0 不影响PY-F02求和，但本题扣边界3分。确认缺交0分。",
        ],
        "positive_count.py",
        "def count_positive(nums):\n    return sum(1 for x in nums if x > 0)\n",
        count_cases,
    )
    stdin_cases = [
        ("3\n1 2 3\n", "6\n", "普通"),
        ("3\n-2 0 5\n", "3\n", "负数"),
        ("0\n", "0\n", "空数据"),
        ("4\n2 2\n3 4\n", "11\n", "跨行空白"),
        ("2\n1000000000 1000000000\n", "2000000000\n", "较大整数"),
    ]
    stdin_stem = "首个整数n(0..100)，随后恰有n个整数(-10^9..10^9)，空白可跨行；输出它们之和及换行，不输出提示语。输入均合法，不要求处理格式错误。"
    stdin_rules = [
        "本题采用构造的结果门槛规则：所有合法输入语义正确且接口正确10分；确认任一合法输入错误为0分，不给过程分；确认缺交0分。",
        "有限用例全通过仅是证据，还须人工检查题意；不能用4/5直接给8分。语法/编译失败归因学生源码时建议0分；平台故障不触发0分。",
    ]
    question(
        "PY-S01",
        "stdin整数求和",
        "python",
        "stdin_stdout",
        None,
        stdin_stem,
        stdin_rules,
        "sum_stdin.py",
        stdin,
        stdin_cases,
    )
    native_cases = stdin_cases + [
        ("3\n1000000000 1000000000 1000000000\n", "3000000000\n", "必须支持64位总和")
    ]
    question(
        "C-S01",
        "C单文件整数求和（补充批次）",
        "c",
        "stdin_stdout",
        None,
        stdin_stem + " 总和须使用足够范围（例如long long）；C11单文件。",
        stdin_rules,
        "sum_stdin.c",
        c,
        native_cases,
    )
    question(
        "CPP-S01",
        "C++单文件整数求和（补充批次）",
        "cpp",
        "stdin_stdout",
        None,
        stdin_stem + " 总和须使用足够范围（例如long long）；C++17单文件。",
        stdin_rules,
        "sum_stdin.cpp",
        cpp,
        native_cases,
    )

    submissions = []

    def submit(
        sid,
        qid,
        filename,
        code,
        score,
        reason,
        policy="execute_after_contract_confirmation",
        scenario="normal",
        expected=None,
        review=None,
    ):
        path = None
        if filename:
            path = "submissions/" + filename
            write_source(path, code)
        submissions.append(
            dict(
                submission_id=sid,
                synthetic_student_id=sid.split("-")[0],
                q_id=qid,
                source_file=path,
                provenance="AI_CONSTRUCTED_NOT_REAL_STUDENT",
                suggested_score=score,
                teacher_confirmed_score=None,
                teacher_name=None,
                teacher_confirmed_at=None,
                teacher_confirmed=False,
                score_status="AI_SUGGESTION_PENDING_HUMAN",
                deduction_reason=reason,
                execution_policy=policy,
                scenario=scenario,
                expected_observation=expected,
                review_condition=review or "必须先确认本题规则/满分/接口与来源；建议分由教师最终确认。",
                platform_failure_rule="平台故障保持未评分（null），恢复后原源码原有效用例复测；用例错误则保留旧证据，经教师确认新版预期后用原源码复测，不以失败率扣分。",
            )
        )

    submit(
        "S01-F01",
        "PY-F01",
        "S01_fib_correct.py",
        fib,
        10,
        "无扣分；方法、基例及接口符合草案。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S02-F01",
        "PY-F01",
        "S02_fib_recursive.py",
        "def fib(n):\n    if n < 2:\n        return n\n    return fib(n-1) + fib(n-2)\n",
        7,
        "扣方法3分：无记忆化递归，时间指数级；5/5正确不能直接判满分。",
        expected={"passed": 5, "total": 5},
        review="教师确认方法3分独立维度；复杂度由源码审阅确定，不以本机小样例耗时冒充复杂度证据。",
    )
    submit(
        "S03-F01",
        "PY-F01",
        "S03_fib_bad_zero.py",
        fib.replace("    a, b = 0, 1", "    if n == 0:\n        return 1\n    a, b = 0, 1"),
        8,
        "扣基例2分：n=0返回1；其余维度保留。",
        expected={"passed": 4, "total": 5},
    )
    submit(
        "S04-F01",
        "PY-F01",
        None,
        None,
        0,
        "仅在教师确认确实未交本题后建议0；不能把上传/识别丢失当缺交。",
        policy="do_not_execute_missing",
        scenario="confirmed_missing",
        review="核对提交清单和学生原文件，确认实际缺交；若仅来源缺失，改为未评分。",
    )
    submit(
        "S05-F01",
        "PY-F01",
        "S05_fib_syntax.py",
        fib.replace("for _ in range(n):", "for _ in range(n)"),
        3,
        "源码缺冒号不能执行；草案给可辨识DP思路3分。此3分为AI建议，须教师裁决语法错误是否给过程分。",
        policy="parse_only_no_execution",
        scenario="syntax_error",
        expected={"syntax_error": True},
    )
    submit(
        "S06-F01",
        "PY-F01",
        "S06_fib_runtime.py",
        (
            "def fib(n):\n"
            "    if n == 0:\n"
            "        return 1 // n\n"
            "    a, b = 0, 1\n"
            "    for _ in range(n):\n"
            "        a, b = b, a + b\n"
            "    return a\n"
        ),
        8,
        "n=0产生ZeroDivisionError；扣基例2分，不把学生运行错误当平台故障。",
        expected={"passed": 4, "total": 5, "runtime_errors": 1},
    )
    submit(
        "S07-F02",
        "PY-F02",
        "S07_shared_reverse.py",
        shared,
        10,
        "乱序、helper居中不应扣分；保留完整源码调用sum_positive。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S07-F03",
        "PY-F03",
        "S07_shared_reverse.py",
        shared,
        10,
        "count_positive定义在另一题之前不影响归属，helper不单独计题。",
        expected={"passed": 5, "total": 5},
    )
    ordered = (
        "def is_positive(x):\n"
        "    return x > 0\n"
        "\n"
        "def sum_positive(nums):\n"
        "    return sum(x for x in nums if is_positive(x))\n"
        "\n"
        "def count_positive(nums):\n"
        "    return sum(1 for x in nums if is_positive(x))\n"
    )
    submit(
        "S08-F02",
        "PY-F02",
        "S08_helper_first.py",
        ordered,
        10,
        "helper在首位；不得执行首个def当成本题入口。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S08-F03",
        "PY-F03",
        "S08_helper_first.py",
        ordered,
        10,
        "与S07函数顺序不同、应等价判分。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S09-F02",
        "PY-F02",
        "S09_unique_sum.py",
        "def sum_positive(nums):\n    return sum(set(x for x in nums if x > 0))\n",
        6,
        "重复值贡献错误扣4分；4/5通过=80%，但教师草案建议6/10。",
        expected={"passed": 4, "total": 5},
    )
    bug = shared.replace("return x > 0", "return x >= 0")
    submit(
        "S10-F02",
        "PY-F02",
        "S10_shared_zero_bug.py",
        bug,
        10,
        "helper把零纳入对求和无数值影响；不得连带扣本题。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S10-F03",
        "PY-F03",
        "S10_shared_zero_bug.py",
        bug,
        7,
        "同一helper使零被计数；只扣本题严格正数边界3分。",
        expected={"passed": 4, "total": 5},
    )
    missing = "def sum_positive(nums):\n    return sum(x for x in nums if x > 0)\n"
    submit(
        "S11-F02",
        "PY-F02",
        "S11_missing_q3.py",
        missing,
        10,
        "本题完整；同文件另题缺交不影响本题。",
        expected={"passed": 5, "total": 5},
    )
    submit(
        "S11-F03",
        "PY-F03",
        "S11_missing_q3.py",
        missing,
        0,
        "源码中没有count_positive；仅在教师确认该题确实未作答后建议0，不能用sum_positive替代。",
        policy="do_not_execute_missing_entry",
        scenario="confirmed_missing_entry",
        review="教师核对完整上传源码并确认未写本题；若入口名只是未知，保持未评分并复核映射。",
    )
    amb = (
        "def first(nums):\n"
        "    return sum(x for x in nums if x > 0)\n"
        "\n"
        "def second(nums):\n"
        "    return sum(1 for x in nums if x > 0)\n"
    )
    submit(
        "S12-F02",
        "PY-F02",
        "S12_entry_mismatch.py",
        amb,
        None,
        "明确合同要求sum_positive，但源码仅first/second，属于入口不符；先核对题目/提交是否错配，不按位置猜入口。",
        policy="do_not_execute_until_mapping_confirmed",
        scenario="entry_mismatch",
        review="教师确认原题与提交版本是否匹配；若确为接口错误，按确认规则判分，不自动改名或将其认定实际缺交。",
    )
    submit(
        "S13-S01",
        "PY-S01",
        "S13_stdin_correct.py",
        stdin,
        10,
        "全部输入与接口符合草案。",
        expected={"passed": 5, "total": 5},
    )
    absbug = stdin.replace("sum(values[1:1+n])", "sum(abs(x) for x in values[1:1+n])")
    submit(
        "S14-S01",
        "PY-S01",
        "S14_stdin_abs_bug.py",
        absbug,
        0,
        "负数求和错误；结果门槛规则不给过程分，4/5通过不能记8分。",
        expected={"passed": 4, "total": 5},
    )
    promptbug = stdin.replace("print(sum(values[1:1+n]))", 'print("sum =", sum(values[1:1+n]))')
    submit(
        "S15-S01",
        "PY-S01",
        "S15_stdin_prompt.py",
        promptbug,
        0,
        "输出额外sum =不符合纯整数输出；不能由测试器悄悄删除提示语。",
        expected={"passed": 0, "total": 5},
    )
    submit(
        "S16-S01",
        "PY-S01",
        "S16_stdin_syntax.py",
        stdin.replace("print(sum(values[1:1+n]))", "print(sum(values[1:1+n])"),
        0,
        "语法错误且本题规则不给过程分；与PY-F01不同，不能套统一70/30。",
        policy="parse_only_no_execution",
        scenario="syntax_error",
        expected={"syntax_error": True},
    )
    submit(
        "S17-S01",
        "PY-S01",
        "S17_platform_unavailable.py",
        stdin,
        None,
        "构造平台事件：OCI不可用，代码未执行；不得记0、0/5或扣学生分。代码审阅可预判正确，但正式分数保持null。",
        policy="do_not_execute_platform_unavailable",
        scenario="platform_unavailable",
        review="负责人恢复同版本执行环境并重跑原提交，教师确认真实执行证据后评分；故障案例是构造情境，本轮未制造真实故障。",
    )
    submit(
        "S18-S01",
        "PY-S01",
        "S18_bad_expected.py",
        stdin,
        None,
        "构造教师测试冲突：T01预期误写7而正确和为6；先复核用例，不把学生正确源码扣分。",
        policy="do_not_execute_until_case_confirmed",
        scenario="incorrect_expected",
        review="教师确认并修正错误预期，保留修改前证据与版本，再运行；不能让AI擅自改教师预期并宣称已确认。",
    )
    conflict = (
        "# 第3题 (PY-F03)\n"
        "def sum_positive(nums):\n"
        "    return sum(x for x in nums if x > 0)\n"
        "\n"
        "# 第2题 (PY-F02)\n"
        "def count_positive(nums):\n"
        "    return sum(1 for x in nums if x > 0)\n"
    )
    for qid in ["PY-F02", "PY-F03"]:
        submit(
            "S19-" + qid[-3:],
            qid,
            "S19_conflicting_labels.py",
            conflict,
            None,
            "源码注释题号与教师明确函数入口互相冲突，保留全部内容，复核归属前不执行、不评分。",
            policy="do_not_execute_until_mapping_confirmed",
            scenario="conflicting_assignment",
            review="教师核对注释错误或题目错配，记录确认映射版本；不得仅按出现顺序或首函数猜题。",
        )
    write_source(
        "mapping_inputs/S07_reverse_answer_blocks.md",
        (
            "# 构造作答材料：题号倒序\n"
            "\n"
            "本文件与S07为同一构造学生的另一种输入形式，不能作为第二次提交重复计分。普通文本映射流程本轮未执行。\n"
            "\n"
            "## 第3题（PY-F03）\n"
            "\n"
            "对应入口 count_positive(nums)。\n"
            "\n"
            "## 第2题（PY-F02）\n"
            "\n"
            "对应入口 sum_positive(nums)。\n"
            "\n"
            "## 两题共用完整源码\n"
            "\n"
            "```python\n"
        )
        + shared
        + "```\n",
    )
    write_json(
        "mapping_inputs/expected_assignments.json",
        {
            "provenance": "AI_CONSTRUCTED_PENDING_TEACHER_CONFIRMATION",
            "file": "mapping_inputs/S07_reverse_answer_blocks.md",
            "order_in_document": ["PY-F03", "PY-F02"],
            "expected_mapping": {"PY-F02": "sum_positive", "PY-F03": "count_positive"},
            "same_submission_as": "submissions/S07_shared_reverse.py",
            "product_ingestion_executed": False,
            "must_preserve_full_source": True,
        },
    )
    for qid, lang, ref, ext in [("C-S01", "c", c, "c"), ("CPP-S01", "cpp", cpp, "cpp")]:
        prefix = "C" if lang == "c" else "X"
        submit(
            prefix + "01",
            qid,
            prefix + "01_correct." + ext,
            ref,
            10,
            "单文件64位累加正确；当前main语言执行缺口不能扣分。",
            policy="future_oci_compile_and_execute",
            expected={"passed": 6, "total": 6},
        )
        wrong = ref.replace("total += x;", "total += (x < 0 ? -x : x);")
        submit(
            prefix + "02",
            qid,
            prefix + "02_abs_bug." + ext,
            wrong,
            0,
            "负数取绝对值造成错误；结果门槛草案为0，不按5/6计分。",
            policy="future_oci_compile_and_execute",
            expected={"passed": 5, "total": 6},
        )
        syntax = ref.replace("total += x;", "total += x")
        submit(
            prefix + "03",
            qid,
            prefix + "03_compile_error." + ext,
            syntax,
            0,
            "少分号；只有已具备正常编译器时才能归为学生编译错误；编译器缺失属于平台故障。",
            policy="compile_only_no_execution",
            scenario="compile_error",
            expected={"compile_error": True},
        )
        submit(
            prefix + "04",
            qid,
            None,
            None,
            0,
            "教师确认确实缺交后建议0；系统不支持本语言不等于缺交。",
            policy="do_not_execute_missing",
            scenario="confirmed_missing",
        )

    write_json(
        "scenarios.json",
        [
            {
                "id": "platform_unavailable",
                "type": "CONSTRUCTED_EVENT_NOT_OBSERVED_INCIDENT",
                "submission": "S17-S01",
                "event": {
                    "runner": "unavailable",
                    "executed": False,
                    "student_failure_count": None,
                    "score": None,
                },
                "expected_action": "未评分，环境恢复后重试；不扣分",
            },
            {
                "id": "incorrect_expected",
                "type": "CONSTRUCTED_EVENT_NOT_OBSERVED_INCIDENT",
                "submission": "S18-S01",
                "case_id": "PY-S01-T01",
                "original_expected": "7\n",
                "mathematically_expected": "6\n",
                "expected_action": "保留冲突，教师复核用例前不执行/不评分",
            },
            {
                "id": "platform_timeout",
                "type": "CONSTRUCTED_EVENT_NOT_OBSERVED_INCIDENT",
                "source_file": "submissions/S13_stdin_correct.py",
                "event": {"worker_lost": True, "completion_unknown": True, "score": None},
                "expected_action": "执行结果未知不等于学生超时；恢复后重试，不能据此扣分",
            },
        ],
    )

    next(row for row in submissions if row["submission_id"] == "S06-F01")["expected_observation"][
        "exception_classes"
    ] = ["ZeroDivisionError"]
    failed_case_numbers = {
        "S03-F01": [1],
        "S06-F01": [1],
        "S09-F02": [4],
        "S10-F03": [3],
        "S14-S01": [2],
        "S15-S01": [1, 2, 3, 4, 5],
        "C02": [2],
        "X02": [2],
    }
    for row in submissions:
        expected = row["expected_observation"]
        if expected and "passed" in expected:
            expected["failed_cases"] = [
                f"{row['q_id']}-T{i:02d}" for i in failed_case_numbers.get(row["submission_id"], [])
            ]

    extend_with_two_integer_cases(questions, submissions, write_source, write_json)
    write_json("questions.json", questions)
    write_json("submissions.json", submissions)
    return questions, submissions


def extend_with_two_integer_cases(questions, submissions, write_source, write_json):
    pure = "材料来源：本次构造，待教师确认。\n用例1：输入 2、3，预期 5。\n用例2：输入 -2、3，预期 1。\n用例3：输入 0、0，预期 0。\n用例4：输入 100、-100，预期 0。\n"
    write_source("teacher_inputs/two_integer_cases.txt", pure)
    values = [([2, 3], 5), ([-2, 3], 1), ([0, 0], 0), ([100, -100], 0)]
    add = "def add(a, b):\n    return a + b\n"
    combine = "def combine(a, b):\n    return a + b\n"
    wrong = "def add(pair):\n    return pair[0] + pair[1]\n"
    stdin = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n"
    question_specs = [
        (
            "PY-F04",
            "双参数求和：教师明确入口",
            "add",
            "function_call",
            "实现 def add(a, b):，接收两个范围[-100,100]的整数位置参数并返回其和；函数名和双参数签名为本题明确要求。",
            add,
            "add_explicit.py",
            {"function_name": "add", "positional_arity": 2},
        ),
        (
            "PY-F05",
            "双参数求和：教师未规定入口",
            "combine",
            "function_call",
            "编写一个Python顶层函数，计算输入的两个整数之和并返回整数，输入范围[-100,100]。教师没有规定函数名或固定签名；能清楚表达两个输入且语义正确的合理实现可接受。",
            combine,
            "combine_unspecified.py",
            None,
        ),
        (
            "PY-S02",
            "双整数stdin求和：同纯文本输入对照",
            None,
            "stdin_stdout",
            "完整Python程序从stdin读取恰好两个整数（范围[-100,100]），以任意空白分隔，输出和及换行，不输出提示语。教师纯文本测试资料与PY-F04/PY-F05使用同一个原文件。",
            stdin,
            "add_stdin.py",
            None,
        ),
    ]
    for qid, title, entry, mode, stem, code, ref, req in question_specs:
        q = copy.deepcopy(questions[0])
        q.update(
            q_id=qid,
            title=title,
            language="python",
            mode=mode,
            function_name=entry,
            stem=stem,
            provenance="constructed",
            reference="references/" + ref,
            cases=[],
            teacher_input_file="teacher_inputs/two_integer_cases.txt",
            teacher_entry_requirement=req,
        )
        if qid == "PY-F04":
            q["teacher_rules_draft"] = [
                "求和语义正确8分；明确要求的add(a,b)函数名及双位置参数接口作为整体2分。接口不符只扣这2分，不重复扣语义分；禁止自动重命名/改签名后当作原提交运行。",
                "接口不符时不按错误调用强行执行；可根据清楚可审阅的构造源码建议语义8分，须教师确认，不能把适配异常计为算法错误。",
            ]
        elif qid == "PY-F05":
            q["teacher_rules_draft"] = [
                "求和语义正确8分；提供合理可调用的Python顶层函数并返回整数2分。未规定函数名/固定签名，因此不能只因命名不同扣接口分。",
                "本地样本验证明确固定combine(a,b)为该构造源码的入口，仅是验算记录，不是教师隐含要求或自动入口推断；实际产品不能确定入口时先复核。",
            ]
            q["local_validation_binding"] = {
                "function_name": "combine",
                "positional_arity": 2,
                "provenance": "AI_CONSTRUCTED_FIXTURE_BINDING_NOT_TEACHER_REQUIREMENT",
                "automatic_inference": False,
            }
        else:
            q["teacher_rules_draft"] = [
                "求和语义正确8分；正确读取两整数并仅输出整数和及换行2分。未设置额外风格分。",
                "同样的“输入2、3，预期5”由题目决定向stdin给2 3及换行，不把中文描述原句当作程序输入；此处只是验收期望，没有实现适配器。",
            ]
        write_source(q["reference"], code)
        for i, (args, result) in enumerate(values, 1):
            cid = f"{qid}-T{i:02d}"
            inp = args if mode == "function_call" else f"{args[0]} {args[1]}\n"
            expected = result if mode == "function_call" else f"{result}\n"
            t = {
                "case_id": cid,
                "input": inp,
                "expected": expected,
                "note": "纯文本双参数语义对照",
                "provenance": "AI_CONSTRUCTED_PENDING_TEACHER_CONFIRMATION",
                "input_file": f"cases/{qid}/{cid}.in.txt",
                "expected_file": f"cases/{qid}/{cid}.expected.txt",
            }
            q["cases"].append(t)
        questions.append(q)

    def add_row(
        sid,
        qid,
        filename,
        code,
        score,
        why,
        policy="execute_after_contract_confirmation",
        scenario="normal",
        contract_evidence=None,
    ):
        s = copy.deepcopy(submissions[0])
        s.update(
            submission_id=sid,
            synthetic_student_id=sid.split("-")[0],
            q_id=qid,
            source_file="submissions/" + filename,
            suggested_score=score,
            deduction_reason=why,
            execution_policy=policy,
            scenario=scenario,
            review_condition="先确认构造规则与原始题意；未规定入口不能制造隐含要求，明确入口不符不能自动改名/改签名。最终分及复核结论由教师确认。",
            expected_observation=None,
        )
        if not policy.startswith("do_not_execute"):
            n = len(next(q for q in questions if q["q_id"] == qid)["cases"])
            s["expected_observation"] = {"passed": n, "total": n, "failed_cases": []}
        if contract_evidence:
            s["explicit_contract_violation"] = contract_evidence
        write_source(s["source_file"], code)
        submissions.append(s)

    add_row(
        "S20-F04",
        "PY-F04",
        "S20_add_correct.py",
        add,
        10,
        "明确add(a,b)接口与求和语义均正确；纯文本输入2、3对应add(2,3)=5。",
    )
    add_row(
        "S21-F04",
        "PY-F04",
        "S21_combine.py",
        combine,
        8,
        "求和逻辑清楚且正确，建议语义8分；教师明确要求add而提交combine，扣接口整体2分。原样不调用、不擅自重命名；不是平台失败或缺交。",
        "do_not_execute_explicit_contract_violation",
        "explicit_contract_violation",
        {
            "kind": "function_name",
            "required_name": "add",
            "observed_name": "combine",
            "required_positional_arity": 2,
            "observed_positional_arity": 2,
        },
    )
    add_row(
        "S21-F05",
        "PY-F05",
        "S21_combine.py",
        combine,
        10,
        "与S21-F04完全相同的原始源码；本题未规定函数名/固定签名，combine(a,b)是合理实现，不得扣命名分。本地入口固定不代表教师要求。",
    )
    add_row(
        "S22-F04",
        "PY-F04",
        "S22_add_wrong_signature.py",
        wrong,
        8,
        "教师明确要求两个位置参数，提交仅一个pair参数；逻辑可辨且正确但接口整体扣2分。不得把调用TypeError当作算法错误，亦不得自动把两参数打包来让其通过。",
        "do_not_execute_explicit_contract_violation",
        "explicit_contract_violation",
        {
            "kind": "positional_arity",
            "required_name": "add",
            "observed_name": "add",
            "required_positional_arity": 2,
            "observed_positional_arity": 1,
        },
    )
    full = (
        "import operator\n"
        "\n"
        "POSITIVE_BOUNDARY = 0\n"
        "\n"
        "# 第3题（PY-F03）：定义顺序在第2题之前\n"
        "def count_positive(nums):\n"
        "    return sum(1 for value in nums if is_positive(value))\n"
        "\n"
        "# 第2题（PY-F02）\n"
        "def sum_positive(nums):\n"
        "    return sum(value for value in nums if is_positive(value))\n"
        "\n"
        "# 两题共用，置于最后；依赖顶部import和常量\n"
        "def is_positive(value):\n"
        "    return operator.gt(value, POSITIVE_BOUNDARY)\n"
    )
    for qid in ["PY-F02", "PY-F03"]:
        add_row(
            "S23-" + qid[-3:],
            qid,
            "S23_full_source_context.py",
            full,
            10,
            "保留整份源码时正确；import operator、顶层POSITIVE_BOUNDARY与末尾共享helper均是实际依赖。函数/题号倒序不应扣分；不能只提取目标def后把NameError归给学生。",
        )
    add_row(
        "S24-S02",
        "PY-S02",
        "S24_add_stdin.py",
        stdin,
        10,
        "与函数题同一纯文本资料，按题意使用stdin两整数；正确结果5不能因原文本适配失败而扣分。",
    )
    mappings = []
    for i, (args, result) in enumerate(values, 1):
        mappings.append(
            {
                "raw_text": pure.splitlines()[i],
                "function_case_id": f"PY-F04-T{i:02d}",
                "unspecified_entry_case_id": f"PY-F05-T{i:02d}",
                "stdin_case_id": f"PY-S02-T{i:02d}",
                "semantic_input": args,
                "expected_result": result,
            }
        )
    write_json(
        "teacher_inputs/acceptance_expectations.json",
        {
            "provenance": "AI_CONSTRUCTED_EXPECTATIONS_NOT_ADAPTER",
            "raw_text_file": "teacher_inputs/two_integer_cases.txt",
            "product_adaptation_executed": False,
            "cases": mappings,
            "paired_same_source_submissions": ["S21-F04", "S21-F05"],
            "explicit_signature_violation_submission": "S22-F04",
            "full_source_context_submissions": ["S23-F02", "S23-F03"],
            "must_preserve": ["import operator", "POSITIVE_BOUNDARY = 0", "def is_positive(value):"],
            "do_not_infer_teacher_contract_from_validation_binding": True,
        },
    )
