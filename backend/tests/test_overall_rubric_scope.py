"""Teacher-selected overall rules are shared source material, not fuzzy matches."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import ingest_agent, question_preparation_agent as agent
from backend.models import ProblemSourceDraft, QuestionScorePolicy
from backend.progress.tracker import ProgressReporter
from backend.services.question_structure import build_major_question_structure


RULE = (
    "指定七题每题满分10分，合计70分；1.1.33奖励题不纳入。"
    "从10分起计，每一处独立重要推导错误扣1分；同根因的后续错误不重复扣分。"
    "局部笔误、仅遗漏特殊情况等小错只指出不扣分；不同正确证明均接受。"
    "空白0分，得分范围0至10分，不以教师批注作为学生论证。"
)
NUMBERS = ("1.1.5", "1.1.7", "1.1.20", "1.1.29", "1.1.31", "1.2.3", "1.2.16")


def questions():
    rows = {}
    for index, number in enumerate(NUMBERS, 1):
        q_id = f"q{index}"
        row = dict(q_id=q_id, number=number, type="证明题", stem=f"Prove statement {number}.",
                   criterion="", reference_answer="A complete valid proof.", max_score=10)
        row["question_structure"] = build_major_question_structure(row, major_order=index - 1).model_dump()
        rows[q_id] = row
    return rows


def source(role="rubric", *, kind="inline_text", mode="extract_from_source", token="overall"):
    return ProblemSourceDraft(source_token=token, task_id="task", owner_id="owner", role=role,
        source_kind=kind, structure_mode=mode, filename=f"{token}.txt", size_bytes=1,
        content_sha256=token, expires_at=9999999999)


def fuzzy_candidates(text=RULE):
    return [ingest_agent.MaterialImportCandidateOutput(q_id=q_id, target="criterion", text_value=text,
        confidence=.9, match_status="possible", source_excerpt=text,
        reason="The overall rubric applies to all selected questions.") for q_id in questions()]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fresh", "extracted_checkpoint", "ocr_only"])
async def test_overall_rubric_keeps_original_scope_without_matching_call(monkeypatch, route):
    extracted = questions()

    async def extract(_text, *args, **kwargs):
        destination = args[-1]
        destination.update(deepcopy(extracted))

    parser = AsyncMock(return_value=fuzzy_candidates())
    generator = AsyncMock(side_effect=AssertionError("Complete teacher material needs no generation"))
    monkeypatch.setattr(agent, "extract_problems", extract)
    monkeypatch.setattr(agent, "extract_problems_from_ocr_markdown", extract)
    monkeypatch.setattr(agent, "parse_material_import_to_candidates", parser)
    monkeypatch.setattr(agent, "generate_major_question_materials", generator)
    sources = [(source("problem", mode="organized", token="problems"), "Selected problem statements"),
               (source(), RULE)]
    args = dict(provider_id="fake", reporter=ProgressReporter(f"overall-{route}"),
                score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10))
    alignment_started = AsyncMock()
    if route == "ocr_only":
        result = await agent.prepare_ocr_question_packages(sources, **args)
    else:
        result = await agent.prepare_question_packages(sources, SimpleNamespace(provider_id="fake"), **args,
            on_base_alignment_started=alignment_started,
            **({"recovered_extracted_problem_data": extracted} if route == "extracted_checkpoint" else {}))
    assert [row["number"] for row in result.values()] == list(NUMBERS)
    assert not parser.called and not generator.called
    assert not alignment_started.called  # No false billable-inflight recovery guard.
    for row in result.values():
        assert row["criterion"] == RULE
        assert row["max_score"] == 10
        assert not row.get("preparation_issues")
        provenance = row["material_provenance"]["criterion"]
        assert provenance["match_status"] == "exact" and provenance["confidence"] == 1
        assert provenance["source_kind"] == "inline_text"
        assert provenance["review_status"] == "pending"  # No invented teacher confirmation.
        assert "criterion" not in row.get("ai_completion_provenance", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,mode", [("inline_text", "organized"), ("upload", "extract_from_source")])
async def test_real_per_question_or_uploaded_ambiguity_is_not_suppressed(monkeypatch, kind, mode):
    parser = AsyncMock(return_value=fuzzy_candidates())
    monkeypatch.setattr(agent, "parse_material_import_to_candidates", parser)
    result = await agent.prepare_question_packages(
        [(source("problem", mode="organized", token="problems"), "Selected statements"),
         (source(kind=kind, mode=mode), RULE)], SimpleNamespace(provider_id="fake"),
        provider_id="fake", reporter=ProgressReporter(f"ambiguous-{kind}"),
        score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10),
        recovered_extracted_problem_data=questions())
    assert parser.await_count == 1
    assert all(any(issue["code"] == "ambiguous_question_match" for issue in row["preparation_issues"])
               for row in result.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("ocr_only", [False, True])
async def test_conflicting_overall_sources_remain_visible(monkeypatch, ocr_only):
    parser = AsyncMock(side_effect=AssertionError("Explicit overall scope requires no fuzzy matching"))
    monkeypatch.setattr(agent, "parse_material_import_to_candidates", parser)
    sources = [(source("problem", mode="organized", token="problems"), "Selected statements"),
               (source(), RULE), (source(token="conflict"), "每处独立推导错误扣3分。")]
    args = dict(provider_id="fake", reporter=ProgressReporter("conflicting-overall"),
                score_policy=QuestionScorePolicy(mode="uniform", uniform_max_score=10))
    if ocr_only:
        async def extract(_text, destination, **kwargs):
            destination.update(questions())
        monkeypatch.setattr(agent, "extract_problems_from_ocr_markdown", extract)
        result = await agent.prepare_ocr_question_packages(sources, **args)
    else:
        result = await agent.prepare_question_packages(sources, SimpleNamespace(provider_id="fake"),
            **args, recovered_extracted_problem_data=questions())
    assert not parser.called
    assert all([issue["code"] for issue in row["preparation_issues"]] == ["source_conflict"]
               for row in result.values())


@pytest.mark.asyncio
async def test_material_prompt_preserves_absolute_deductions_and_scoped_exceptions(monkeypatch):
    call = AsyncMock(return_value=SimpleNamespace(content='{"candidates":[]}'))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)
    await ingest_agent.parse_material_import_to_candidates(
        text=RULE, problems_data=questions(), targets=["criterion"], structure_mode="organized",
        extraction_hint="", provider=SimpleNamespace(provider_id="fake"))
    system = call.await_args.args[1][0].content
    assert "only as percentages totaling 100%" not in system
    assert "Never copy an absolute point total" not in system
    assert "deduction" in system and "scope" in system and "exceptions" in system
