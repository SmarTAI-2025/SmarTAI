"""Tests for post-extraction problem dedup (fixes 重复识别 / 满分分配混乱).

The extraction pipeline may emit the same question twice: a sub-question split
out of a full 解答题, a near-duplicate of the same numbered question, or a
question cut across two extraction chunks (trailing fragment in chunk N,
leading fragment with empty ``number`` plus the full question in chunk N+1).
``dedupe_extracted_problems`` must collapse those into one row per real
question before ``resolve_question_score_policy`` freezes max scores.
"""
import copy
import json
from types import SimpleNamespace

import pytest

from backend.agents import ingest_agent
from backend.config import settings
from backend.models import QuestionScorePolicy
from backend.skills import question_score
from backend.tools.problem_dedup import (
    dedupe_extracted_problems,
    normalize_stem,
)

# ─── Fixture stems (2024 新高考数学卷 shape) ─────────────────────────────────


def _problem(
    q_id: str,
    number: str,
    stem: str,
    *,
    type_="概念题",
    criterion="",
    reference=None,
    issues=None,
):
    return {
        "q_id": q_id,
        "number": number,
        "type": type_,
        "stem": stem,
        "criterion": criterion,
        "max_score": 10,
        "review_status": "needs_review",
        "reference_answer": reference,
        "preparation_issues": issues or [],
    }


P1_FULL = (
    "1. 已知集合 $A=\\{x \\mid 0 < x < 5\\}$，$B=\\{1, 2, 3\\}$，则 $A \\cap B=$ 【　】\n"
    "A. $\\{1, 2\\}$  B. $\\{1, 3\\}$  C. $\\{2, 3\\}$  D. $\\{1, 2, 3\\}$"
)
# One character differs in the middle: near-duplicate, not containment.
P1_NEAR = P1_FULL.replace("0 < x < 5", "0 < x < 6")

P2_FULL = (
    "已知函数 $f(x)=\\ln x + ax + b(x-1)^3$。\n"
    "(1) 若 $b=0$，且 $f'(x) \\ge 0$，求 $a$ 的最小值；\n"
    "(2) 证明：曲线 $y=f(x)$ 是中心对称图形；\n"
    "(3) 若 $f(x) > -2$ 当且仅当 $1 < x < 2$，求 $b$ 的取值范围。"
)
# The "第一问被拆成独立题" case: shared conditions + only sub-question (1).
P2_HEAD = P2_FULL.split("(2)")[0].rstrip()
# The cross-chunk leading fragment: starts mid-question, no number.
P2_TAIL = "(2)" + P2_FULL.split("(2)")[1]
# A clearly different question that also carries number "1" (section reset).
P_MULTI = (
    "为了解某种植区亩收入情况，抽取样本得到均值 $\\bar{x}=2.1$，方差 $s^2=0.01$。"
    "已知以往亩收入 $X\\sim N(1.8, 0.12)$，则【　】\n"
    "A. $P(X>2)>0.2$  B. $P(X>2)<0.5$  C. 无法判断  D. 均不成立"
)


# ─── normalize_stem ──────────────────────────────────────────────────────────


def test_normalize_stem_nfkc_whitespace_casefold():
    assert normalize_stem("Ａｂｃ  ｘ\n(1)") == "abcx(1)"
    assert normalize_stem("") == ""
    # Full-width ideographic space (【　】) is stripped like any whitespace.
    assert normalize_stem("【　】") == "【】"


# ─── Pure-function merge rules ───────────────────────────────────────────────


def test_merges_near_duplicate_same_number_keeps_first():
    problems = {
        "q1": _problem("q1", "1", P1_FULL),
        "q2": _problem("q2", "1", P1_NEAR),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1"]
    assert result["q1"]["stem"] == P1_FULL
    assert result["q1"]["number"] == "1"


def test_merges_contained_same_number_and_backfills_empty_fields():
    problems = {
        "q1": _problem("q1", "1", P1_FULL),
        "q2": _problem(
            "q2", "1", P_MULTI,  # different stem, same number: no merge
        ),
        "q3": _problem("q3", "2", P2_HEAD, type_="解答题"),
        "q4": _problem(
            "q4",
            "2",
            P2_FULL,
            type_="解答题",
            criterion="1. 第(1)问: 34%。2. 第(2)(3)问: 66%。",
            reference="略",
        ),
    }
    result = dedupe_extracted_problems(problems)
    # q3 ⊂ q4 (same number) collapses; q1/q2 both survive (section reset).
    assert list(result) == ["q1", "q2", "q3"]
    merged = result["q3"]
    assert merged["stem"] == P2_FULL
    assert merged["criterion"] == "1. 第(1)问: 34%。2. 第(2)(3)问: 66%。"
    assert merged["reference_answer"] == "略"
    assert merged["number"] == "2"


def test_concatenates_suffix_prefix_overlap():
    # A chunk cut inside the question: earlier item holds the head and ends
    # mid-sentence, later item holds the tail. Their shared verbatim region
    # (>= 40 normalized chars) must be spliced, not dropped.
    s = P2_FULL.index("(2)")
    e = P2_FULL.index("求 $b$ 的取值范围")
    head = P2_FULL[:e]
    tail = P2_FULL[s:]
    assert len(normalize_stem(P2_FULL[s:e])) >= 40
    problems = {
        "q1": _problem("q1", "4", head, type_="解答题"),
        "q2": _problem("q2", "4", tail, type_="解答题"),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1"]
    assert normalize_stem(result["q1"]["stem"]) == normalize_stem(P2_FULL)


def test_concatenates_leading_fragment_with_empty_number():
    s = P2_FULL.index("(2)")
    e = P2_FULL.index("求 $b$ 的取值范围")
    head = P2_FULL[:e]
    tail = P2_FULL[s:]
    problems = {
        "q1": _problem("q1", "4", head, type_="解答题"),
        "q2": _problem(
            "q2",
            "",  # leading fragment: no number, per the extraction prompt rule
            tail,
            type_="解答题",
            criterion="1. 第(2)问: 50%。2. 第(3)问: 50%。",
        ),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1"]
    assert result["q1"]["number"] == "4"
    assert normalize_stem(result["q1"]["stem"]) == normalize_stem(P2_FULL)
    # Empty fields on the kept row are backfilled from the discarded one.
    assert result["q1"]["criterion"] == "1. 第(2)问: 50%。2. 第(3)问: 50%。"


def test_merges_subquestion_fragment_of_full_question():
    # "第一问" split out as its own entry (different display number) while the
    # full question is also present: containment keeps the full question.
    problems = {
        "q1": _problem("q1", "4", P2_FULL, type_="解答题"),
        "q2": _problem("q2", "(1)", P2_HEAD, type_="解答题"),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1"]
    assert result["q1"]["stem"] == P2_FULL
    assert result["q1"]["number"] == "4"


def test_keeps_same_number_different_stems():
    problems = {
        "q1": _problem("q1", "1", P1_FULL),
        "q2": _problem("q2", "1", P_MULTI),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1", "q2"]
    assert result["q1"]["stem"] == P1_FULL
    assert result["q2"]["stem"] == P_MULTI


def test_renumbers_and_rewrites_preparation_issues():
    problems = {
        "q7": _problem(
            "q7",
            "1",
            P1_FULL,
            issues=[
                {
                    "issue_id": "ocr_review_q7",
                    "q_id": "q7",
                    "field": "source",
                    "code": "parse_anomaly",
                    "severity": "warning",
                    "source_ids": [],
                    "details": {},
                    "status": "open",
                }
            ],
        ),
        "q12": _problem(
            "q12",
            "2",
            P_MULTI,
            issues=[{"issue_id": "x", "q_id": "q12"}],
        ),
        # Duplicate of q7: its issue id must point at the surviving q_id.
        "q13": _problem(
            "q13",
            "1",
            P1_NEAR,
            issues=[{"issue_id": "y", "q_id": "q13"}],
        ),
    }
    result = dedupe_extracted_problems(problems)
    assert list(result) == ["q1", "q2"]
    assert result["q1"]["preparation_issues"][0] == {
        "issue_id": "ocr_review_q7",
        "q_id": "q1",
        "field": "source",
        "code": "parse_anomaly",
        "severity": "warning",
        "source_ids": [],
        "details": {},
        "status": "open",
    }
    assert result["q1"]["preparation_issues"][1] == {"issue_id": "y", "q_id": "q1"}
    assert result["q2"]["preparation_issues"][0] == {"issue_id": "x", "q_id": "q2"}


def test_empty_input_and_single_item():
    assert dedupe_extracted_problems({}) == {}
    single = {"q9": _problem("q9", "1", P1_FULL, issues=[{"q_id": "q9"}])}
    result = dedupe_extracted_problems(single)
    assert list(result) == ["q1"]
    assert result["q1"]["stem"] == P1_FULL
    assert result["q1"]["preparation_issues"][0]["q_id"] == "q1"


def test_does_not_mutate_input():
    problems = {
        "q1": _problem("q1", "1", P1_FULL),
        "q2": _problem("q2", "1", P1_NEAR),
    }
    snapshot = copy.deepcopy(problems)
    dedupe_extracted_problems(problems)
    assert problems == snapshot


# ─── Integration: chunked extract_problems dedupes end to end ────────────────


@pytest.mark.asyncio
async def test_extract_problems_dedupes_across_chunks(monkeypatch):
    # Force a small extraction chunk (120 chars, 30-char overlap).
    monkeypatch.setattr(settings, "source_chunk_chars", 120)
    monkeypatch.setattr(settings, "source_chunk_overlap_chars", 30)
    text = P1_FULL + "\n" + P2_FULL + "\n" + P_MULTI
    chunk_texts = ingest_agent._chunk_problem_text(text, 120, 30)
    assert len(chunk_texts) >= 2  # guarantees the chunked path

    def _row(q_id, number, type_, stem, criterion=""):
        return {
            "q_id": q_id,
            "number": number,
            "type": type_,
            "stem": stem,
            "criterion": criterion,
        }

    # Chunk 0 holds the complete choice question plus the essay question cut
    # mid-stem; the final chunk holds the leading fragment (empty number) and
    # the complete essay question; overlap chunks re-emit the truncated head.
    def response_for(chunk_index: int) -> str:
        if chunk_index == 0:
            problems = [
                _row("q1", "1", "选择题", P1_FULL, "答案唯一: 答对满分, 答错 0 分"),
                _row("q2", "2", "解答题", P2_HEAD),
            ]
        elif chunk_index == len(chunk_texts) - 1:
            problems = [
                _row("q1", "", "解答题", P2_TAIL),
                _row("q2", "2", "解答题", P2_FULL, "1. 第(1)问: 34%。"),
            ]
        else:
            problems = [_row("q1", "2", "解答题", P2_HEAD)]
        return json.dumps({"problems": problems}, ensure_ascii=False)

    calls = []

    async def fake_ainvoke(provider, messages):
        calls.append(1)
        return SimpleNamespace(content=response_for(len(calls) - 1))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", fake_ainvoke)
    problem_store: dict = {}

    await ingest_agent.extract_problems(
        text,
        SimpleNamespace(provider_id="mock:model"),
        problem_store,
    )

    assert len(calls) == len(chunk_texts)
    assert list(problem_store) == ["q1", "q2"]
    assert problem_store["q1"]["stem"] == P1_FULL
    assert problem_store["q1"]["number"] == "1"
    # The truncated head and the leading fragment collapse into the full stem.
    assert problem_store["q2"]["stem"] == P2_FULL
    assert problem_store["q2"]["number"] == "2"


# ─── Contract: dedup runs before score-policy freezing (fixes 满分混乱) ───────


def _choice_stem(i: int) -> str:
    return (
        f"已知集合 $A_{i} = \\{{ {i}, {i + 1}, {i + 2} \\}}$，"
        f"$B = \\{{ 0, {i} \\}}$，则 $A_{i} \\cap B$ 中元素的个数为"
    )


def _multi_stem(i: int) -> str:
    return (
        f"设函数 $f_{i}(x) = x^2 - {i}x$，若 $f_{i}(x_1) = f_{i}(x_2)$ 且 "
        f"$x_1 < x_2$，则下列结论中正确的是"
    )


def _blank_stem(i: int) -> str:
    return (
        f"若曲线 $y = e^{{x}} + {i}x$ 在点 $(0, 1)$ 处的切线也是曲线 "
        f"$y = \\ln(x+1) + a$ 的切线，则 $a =$________。"
    )


def _essay_stem(i: int) -> str:
    return (
        f"记 $\\triangle ABC_{i}$ 的内角 $A, B, C$ 的对边分别为 $a, b, c$，"
        f"已知 $\\sin C = \\sqrt{{2}} \\cos B$，$a^2 + b^2 - c^2 = "
        f"{i}\\sqrt{{2}}ab$，求角 $B$ 的大小。"
    )


@pytest.mark.asyncio
async def test_deduped_extraction_feeds_score_policy(monkeypatch):
    # 22 raw entries: the 19 real questions (section-local numbering resets)
    # plus 3 duplicates — a near-duplicate single choice, a split-out 第一问,
    # and a chunk-boundary fragment.
    problems: dict = {}
    q = 1

    def add(number, stem, type_="概念题", **kw):
        nonlocal q
        problems[f"q{q}"] = _problem(f"q{q}", number, stem, type_=type_, **kw)
        q += 1

    for i in range(1, 9):
        add(str(i), _choice_stem(i), "选择题")
    for i in range(1, 4):
        add(str(i), _multi_stem(i), "多选题")
    for i in range(1, 4):
        add(str(i), _blank_stem(i), "填空题")
    for i in range(1, 6):
        add(str(i), P2_FULL if i == 4 else _essay_stem(i), "解答题")
    # Duplicates (the observed 22nd/23rd/24th entries).
    add("1", _choice_stem(1).replace("{ 0, 1 }", "{ 0, 2 }"), "选择题")
    add("4", P2_HEAD, "解答题")
    add("", P2_TAIL, "解答题")
    assert len(problems) == 22

    deduped = dedupe_extracted_problems(problems)
    assert list(deduped) == [f"q{i}" for i in range(1, 20)]
    assert [p["number"] for p in deduped.values()] == [
        "1", "2", "3", "4", "5", "6", "7", "8",
        "1", "2", "3",
        "1", "2", "3",
        "1", "2", "3", "4", "5",
    ]

    scores = [5.0] * 8 + [6.0] * 3 + [5.0] * 3 + [13.0, 15.0, 15.0, 17.0, 17.0]

    async def fake_structured(provider, *, system_prompt, user_prompt, output_model, **kw):
        plan = output_model(
            scores=[
                question_score.InterpretedQuestionScore(q_id=q_id, max_score=s)
                for q_id, s in zip(deduped, scores)
            ]
        )
        return plan, SimpleNamespace(content="{}")

    monkeypatch.setattr(question_score, "structured_llm_call", fake_structured)
    policy = QuestionScorePolicy(
        mode="per_question",
        per_question_text=(
            "单选每题5分；多选每题6分；填空每题5分；"
            "解答依次为13、15、15、17、17分。"
        ),
    )
    resolved = await question_score.resolve_question_score_policy(
        deduped, policy, SimpleNamespace(provider_id="mock:model")
    )

    assert len(resolved) == 19
    assert all(r.issue_code != "max_score_not_found" for r in resolved.values())
    assert sum(r.max_score for r in resolved.values()) == 150
