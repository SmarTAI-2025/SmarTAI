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
    assert not marked_counts(stem.replace("n 为正整数", "n 为正整数且 n>2")).get("(2)")
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
