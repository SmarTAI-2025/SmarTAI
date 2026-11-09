import pytest

from backend.recognition.formula_panel import conflicting_heading
from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1
from backend.recognition.planner import (
    EngineCapabilitiesV1,
    PageObservationV1,
    RecognitionPlanRequestV1,
    plan_recognition,
)
from backend.services.count_witnesses import (
    marked_counts,
    counterexample,
    reference_error,
    submitted_count_checks,
)
from backend.services.growth_order import ordered_growth_classes


def test_narrow_panel_only_when_selected_without_displacing_the_page():
    panel = NormalizedRegionV1(x0=0.1, y0=0.5, x1=0.8, y1=0.6)
    observations = [
        PageObservationV1(page_number=1, formula_panels=[panel], risks=["diagram"])
    ]
    engine = EngineCapabilitiesV1(
        route_id="reader",
        fingerprint="reader",
        visual_inputs=["page_image"],
        region_reads=True,
    )

    def plan(purpose, cap, version="printed-formula-panels-v1"):
        request = RecognitionPlanRequestV1(
            purpose=purpose,
            scope="pages",
            total_pages=1,
            requested_pages=[1],
            observations=observations,
        )
        return plan_recognition(
            request,
            engine=engine,
            policy=RecognitionPolicyV1(version=version, max_initial_calls=cap),
        )

    assert plan("problems", 2).decisions[0].regions == [NormalizedRegionV1(), panel]
    for old in (
        plan("problems", 1),
        plan("submissions", 2),
        plan("problems", 2, "faithful-v2"),
    ):
        assert old.initial_calls == 1 and old.decisions[0].regions == [
            NormalizedRegionV1()
        ]


def test_formula_comparison_and_unaffected_neighbour():
    whole = r"1. 10② 函数：$n^n$, $2^n$, $n/\log_{2}2$, $n^2$" + "\n1.12 判断下列结论。"
    crop = r"1.10(2) 函数：$n^{n},2^{n},n/\log_{n}{2},n^2$"
    assert conflicting_heading(whole, crop) == "1.10"
    assert conflicting_heading(whole, crop.replace("_{n}{2}", "_2 2")) is None
    assert conflicting_heading(whole, crop.replace("1.10", "1.09")) is None
    assert conflicting_heading(whole + "\n" + whole, crop) is None


def test_loop_boundaries_and_source_dependent_checks_are_bounded():
    stem = """设 n 为正整数。求 @ 语句执行次数。
(1) i=1; while (i<n) { @ i++; }
(2) $i=1$; do { @ $i++$; } while ($i<n$);
(3) i=2; j=3; while(j>0){ @ if (i>3) {i-=2; j--;} else i++;}
(4) while(external) { @ x++; }
(5) while(1) { @ x++; }"""
    result = marked_counts(stem)
    assert set(result) == {"(1)", "(2)", "(3)"}
    assert result["(1)"]["samples"]["1"] == 0 and result["(2)"]["samples"]["1"] == 1
    assert result["(3)"]["exact"] == 9
    assert "n=1" in counterexample("n-1", result["(2)"])
    assert counterexample("max(1,n-1)", result["(2)"]) is None
    assert marked_counts("写出证明过程。") == {}
    assert set(
        marked_counts(stem.replace("n 为正整数", "n 为正整数且 n>2"))["(2)"]["samples"]
    ) == {"4", "5"}
    assert reference_error("(1) n-1\n(2) n-1\n(3) 9", result).startswith("(2) n=1")


def test_graded_count_expression_needs_a_located_closed_answer():
    answers = "(1) n-1\n(2) 3*4=12\n(3) 未作答"
    result = submitted_count_checks(
        answers, {"(1)": {"samples": {"1": 1, "4": 3}}, "(2)": {"exact": 12}}
    )
    assert "n=1" in result["(1)"]["counterexample"]
    assert result["(2)"]["confirmed_exact_count"] == 12


def test_growth_equivalence_and_factorial_and_quasipolynomial():
    stem = r"按增长率由小到大排列：$n^{\log_2 n}$, $n!$, $(3/2)^n$, $n^n$, $n\log_2 n$, $n/\log_n 2$, $\sqrt{n}$, $n$。"
    groups = ordered_growth_classes(stem)
    assert groups == [
        [r"\sqrt{n}"],
        ["n"],
        [r"n\log_2 n", r"n/\log_n 2"],
        [r"n^{\log_2 n}"],
        ["(3/2)^n"],
        ["n!"],
        ["n^n"],
    ]
    assert not ordered_growth_classes(stem.replace("n!", "arbitrary(n)"))
    assert not ordered_growth_classes(stem.replace("小到大", "大到小"))


def test_monotone_invariants_and_restricted_domain_do_not_establish_unrelated_formulas():
    stem = """设 n 为正整数。求 @ 语句频度。
(1) i=1;j=0;while(i+j<=n){ @ if(i>j)j++;else i++; }
(2) bound=n;c=0;while(bound>=(c+1)*(c+1)){ @ c++; }
(3) bound=n;c=0;while(bound>=(c+1)*(c+1)){ @ c++; bound--; }"""
    witnesses = marked_counts(stem)
    assert witnesses["(1)"]["formula"] == "n"
    assert witnesses["(2)"]["formula"] == "isqrt(n)"
    assert "formula" not in witnesses["(3)"]
    evidence = submitted_count_checks(
        r"(1) $n$" + "\n" + r"(2) $[\sqrt{n}]$", witnesses
    )
    assert evidence["(2)"] == {"proven_formula": "isqrt(n)"}
    assert not marked_counts(stem.replace("正整数。", "正整数，且为偶数。"))
    assert not submitted_count_checks("(1) 0=12", {"(1)": {"exact": 12}})


@pytest.mark.asyncio
async def test_actual_scanned_page_reserves_whole_page_and_keeps_disagreement_local():
    import io
    import pymupdf
    from PIL import Image
    from backend.tests.test_recognition_agent import Engine, request
    from backend.agents.recognition_agent import RecognitionAgent

    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=640)
        for box in [(25, 20, 350, 260), (25, 290, 350, 330)]:
            buf = io.BytesIO()
            Image.new("RGB", (600, 80), color="#333333").save(buf, format="PNG")
            page.insert_image(box, stream=buf.getvalue(), keep_proportion=False)
        data = pdf.tobytes()

    class Reader(Engine):
        async def recognize(self, unit):
            self.reads.append(unit)
            whole = unit.region == NormalizedRegionV1()
            return self.candidate(
                r"1.10 Formulas: $n^n,2^n,n/\log_2 2,n^2$" + "\n1.12 Statements."
                if whole
                else r"1.10 Formulas: $n^n,2^n,n/\log_n 2,n^2$"
            )

    from backend.recognition.runtime import RecognitionCapacity

    reader = Reader()
    batch = await RecognitionAgent(reader, capacity=RecognitionCapacity()).recognize(
        request(
            data,
            scope="pages",
            pages=[1],
            policy=RecognitionPolicyV1(version="printed-formula-panels-v1"),
        ),
        data,
        authorized_owner_id="owner",
        prompt_version="faithful-reader-v2",
    )
    assert len(reader.reads) == 2
    assert batch.document.coverage.processed_pages == [1]
    assert batch.document.pages[0].formula_conflicts == ["1.10"]
    assert not batch.document.unaligned_units


def test_single_fenced_program_and_reference_to_the_wrong_algebra_are_distinct():
    stem = "求 @ 语句的执行次数。\n```c\nx=0; while(x<3) { @ x++; }\n```"
    assert marked_counts(stem) == {"question": {"exact": 3}}
    assert (
        conflicting_heading(r"1.8 A: $2^n,n^n,n^2,3^n$", r"1.8 A: $2^n,n^\pi,n^2,3^n$")
        == "1.8"
    )
    assert (
        conflicting_heading(r"1.8 A: $2^n,n^n,n^2,3^n$", r"1.8 A: $2^n,n^2,3^n$")
        == "1.8"
    )


def test_formula_summary_binds_to_the_disputed_number_and_is_idempotent():
    from backend.services.question_sources import attach_recognition_review

    rows = {"qa": {"number": "1.10"}, "qb": {"number": "1.12"}}
    source = {"recognition": {"operation_id": "op", "formula_conflicts": ["1.10"]}}
    attach_recognition_review(rows, [source])
    attach_recognition_review(rows, [source])
    assert [i["code"] for i in rows["qa"]["preparation_issues"]] == [
        "recognition_partial"
    ]
    assert not rows["qb"].get("preparation_issues")


@pytest.mark.asyncio
async def test_preparation_saves_neighbour_and_holds_only_disputed_question(
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from backend.agents import incremental_question_preparation as inc
    from backend.tests.test_incremental_question_preparation import (
        setup,
        row,
        generated,
        persisted,
    )
    from backend.domain.errors import ValidationError

    page_text = (
        "Question 1.10\nOrder the functions.\nQuestion 1.12\nJudge these statements."
    )
    owner, _, op, pipeline = await setup(monkeypatch, text=page_text)

    async def read(source, *, on_pages):
        await on_pages(
            [dict(page_number=1, text=page_text, formula_conflicts=["1.10"])]
        )
        draft, text, payload = source
        return (
            draft,
            text,
            {
                **payload,
                "recognition": {
                    "coverage": {"requested_pages": [1], "processed_pages": [1]}
                },
            },
        )

    pipeline.read_source = read
    monkeypatch.setattr(
        inc,
        "_extract_problems_call",
        AsyncMock(return_value=inc._rows([row("1.10"), row("1.12")])),
    )
    attempted = []

    async def complete(data, targets, *args, **kwargs):
        attempted.append(data["q1"]["number"])
        return [
            generated(t["target"], "Complete neighbouring material.") for t in targets
        ]

    monkeypatch.setattr(inc, "generate_missing_question_materials", complete)
    with pytest.raises(ValidationError):
        await pipeline.run()
    _, rows = persisted(owner, op)
    by_number = {r["number"]: r for r in rows.values()}
    assert attempted == ["1.12"]
    assert by_number["1.12"]["criterion"] and not by_number["1.10"]["criterion"]
    assert any(
        i["details"].get("reason") == "conflicting_printed_formula"
        for i in by_number["1.10"]["preparation_issues"]
    )


@pytest.mark.parametrize(
    "qualifier", ["且 n≠1", r"，且 $n\ne 1$", " where n != 1", "，n 不等于 1"]
)
def test_count_samples_obey_the_complete_stated_domain(qualifier):
    stem = f"设 n 为正整数{qualifier}。求 @ 语句执行次数。\n(1) i=1; do {{ @ i++; }} while(i<n);\n(2) i=0;while(i<n){{ @ i++; }}"
    checks = marked_counts(stem)
    assert checks and all(set(w["samples"]) == {"2", "4", "5"} for w in checks.values())
    assert reference_error("(1) n-1\n(2) n", checks) is None
    assert not submitted_count_checks("(1) n-1\n(2) n", checks)


def test_single_program_accepts_worked_answers_and_rejects_wrong_result():
    from types import SimpleNamespace
    from backend.services.count_witnesses import require_consistent_count_grades

    stem = "求 @ 语句的执行次数。\n```c\nx=0; while(x<3){ @ x++; }\n```"
    checks = marked_counts(stem)
    assert reference_error("4", checks)
    assert not reference_error("因此执行次数为 3。", checks)
    results = submitted_count_checks("4", checks)
    assert "counterexample" in results["question"]
    rule = SimpleNamespace(
        teacher_rubric=None,
        parts=[
            SimpleNamespace(
                label=None,
                id="singleton",
                steps=[SimpleNamespace(criterion="给出执行次数")],
            )
        ],
    )
    draft = SimpleNamespace(
        part_decisions=[
            SimpleNamespace(
                part_id="singleton", relevance="complete", method="reference", steps=[]
            )
        ]
    )
    with pytest.raises(ValueError, match="counterexample|cannot be complete"):
        require_consistent_count_grades(
            results, SimpleNamespace(part_rubric=rule), draft, "4", stem
        )


@pytest.mark.parametrize(
    "qualification",
    ["，只取斐波那契数列第十项", " that is a power of three", " 且 n mod 3 = 2"],
)
def test_unparsed_domain_is_never_assumed_to_contain_test_inputs(qualification):
    source = (
        "设 n 为正整数"
        + qualification
        + "。求 @ 语句频度。\n(1) i=0;while(i<n){ @ i++; }\n(2) i=0;while(i<3){ @ i++; }"
    )
    assert "(1)" not in marked_counts(source)
    assert marked_counts(source)["(2)"] == {"exact": 3}


def test_unrecognized_local_domain_only_disables_that_parts_parametric_samples():
    stem = "设 n 为正整数。求 @ 语句频度。\n(1) i=0; // only when n is a square\nwhile(i<n){ @ i++; }\n(2) i=0;while(i<n){ @ i++; }"
    assert set(marked_counts(stem)) == {"(2)"}
    assert not counterexample("n", {"exact": 3})


def test_plain_singleton_uses_full_program_and_english_restricted_domain():
    stem = "Let n be a positive integer and n != 1. Determine the execution count of the marked statement:\ni=1; while(i<n) { @ i++; }"
    witnesses = marked_counts(stem)
    assert set(witnesses["question"]["samples"]) == {"2", "4", "5"}
    assert not reference_error("n-1", witnesses)
    assert reference_error("n", witnesses)
    assert not marked_counts(stem + "\nOnly consider prime n.")
    assert not counterexample(None, {"exact": 3})


@pytest.mark.parametrize("base", [".5", "0.5", "(.5)", "(0.5)", "(1/2)", r"\frac{1}{2}"])
def test_decay_keeps_the_exact_source_expression(base):
    term = f"{base}^n"
    assert ordered_growth_classes(f"按增长率由小到大排列：${term}$, $2^n$, $n$.") == [
        [term],
        ["n"],
        ["2^n"],
    ]


@pytest.mark.parametrize(
    "scope",
    [
        "n趋于0+",
        r"n\to0^{+}",
        "n → 1",
        "n → −∞",
        "as n tends to zero from above",
        "in a neighbourhood of the origin",
        "n→∞ 或 n→0",
        "0<n<1",
    ],
)
def test_unproved_limit_never_reuses_infinity_order(scope):
    assert ordered_growth_classes(f"按增长率由小到大排列（{scope}）：n、n^2、n^3") == []


@pytest.mark.parametrize("scope", [r"当 $n\to +\infty$ 时", "as n tends to infinity", "n→∞"])
def test_explicit_positive_infinity_keeps_ordinary_growth_order(scope):
    assert ordered_growth_classes(f"按增长率由小到大排列（{scope}）：n^3、n、n^2") == [
        ["n"],
        ["n^2"],
        ["n^3"],
    ]


@pytest.mark.parametrize("term", ["-.5^n", "2(3)^n", "1.2.3^n", "1 2^n", "2^(3)4", "log_(2)3n", "log_2(3)n"])
def test_unsupported_tokens_cannot_turn_into_different_supported_terms(term):
    assert ordered_growth_classes(f"按增长率由小到大排列：{term}, n, 2^n") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("按增长率由小到大排列：.5^n、n、2^n", [[".5^n"], ["n"], ["2^n"]]),
        ("按增长率由小到大排列（n趋于0+）：n、n^2、n^3", None),
    ],
)
async def test_generator_and_grader_receive_only_supported_growth_evidence(stem, expected):
    import json
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.models import ProblemInfo, StudentAnswerInfo
    from backend.services.math_rubric_plan import fallback_math_rubric_plan
    from backend.skills.math_grading import MathGradingSkill
    from backend.tests.test_question_material_format_repair import Provider, valid

    p = ProblemInfo(
        q_id="q1",
        number="1",
        type="计算题",
        stem=stem,
        criterion="排序正确给满分",
        reference_answer="按题意排序",
    )
    answer = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type, content="见排序")
    grading = MathGradingSkill(None)._prompt(p, answer, fallback_math_rubric_plan(p), "")
    grade_context = json.loads(grading.split("INPUT DATA:\n")[-1])
    provider = Provider([valid()])
    await generate_missing_question_materials(
        {"q1": p.model_dump()},
        [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer"}],
        6,
        provider,
    )
    text = provider.calls[0][-1].content
    generator_context = json.JSONDecoder().raw_decode(text[text.index("{") :])[0]
    for context in (grade_context, generator_context["known_problems"][0]):
        assert context.get("source_growth_order") == expected
        assert ("growth_order_instruction" in context) == (expected is not None)


def test_textbook_heading_and_tex_orders_keep_supported_equivalences():
    stem = r"1.10② 按增长率由小至大的顺序排列下列各函数：$2^{100}$, $n^{2/3}$, $n\log_{2}{n}$, $n/\log_{n}{2}$, $n^{n}$"
    assert ordered_growth_classes(stem) == [
        ["2^{100}"],
        ["n^{2/3}"],
        [r"n\log_{2}{n}", r"n/\log_{n}{2}"],
        ["n^{n}"],
    ]
