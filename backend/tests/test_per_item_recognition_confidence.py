"""Model assessments belong to individual extracted questions and answers."""
import json
from types import SimpleNamespace

import pytest

from backend.agents import ingest_agent
from backend.domain.confidence_policy import confidence_needs_attention
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services import task_facade
from backend.services.question_sources import attach_recognition_review
from backend.services.submission_source_pipeline import attach_submission_recognition


@pytest.mark.parametrize("value", [None, "low", "0.5", True, -1, 2, float("nan"), float("inf")])
def test_missing_or_invalid_item_confidence_stays_unknown(value):
    question = ProblemInfo(q_id="q1", number="1", type="concept", stem="literal", criterion="",
                           recognition_confidence=value)
    answer = StudentAnswerInfo(q_id="q1", number="1", type="concept", content="literal",
                               recognition_confidence=value)
    assert question.recognition_confidence is None
    assert answer.recognition_confidence is None
    assert not confidence_needs_attention(answer.recognition_confidence)
    assert answer.model_dump()["flag"] == []
    assert answer.model_dump(include={"content"}) == {"content": "literal"}


@pytest.mark.asyncio
async def test_question_parser_keeps_independent_scores_and_source_low_cannot_override(monkeypatch):
    rows = [
        dict(q_id=f"q{i}", number=str(i), type="concept", stem=f"Question {i}", criterion="",
             recognition_confidence=score)
        for i, score in enumerate([.98, .4, .6, None], 1)
    ]

    async def invoke(_provider, messages):
        return SimpleNamespace(content=json.dumps({"problems": rows}))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    packages = {}
    await ingest_agent.extract_problems("Four literal questions", SimpleNamespace(), packages)
    assert [p["recognition_confidence"] for p in packages.values()] == [.98, .4, .6, None]
    for file_confidence in ("low", .1):
        attach_recognition_review(packages, [{"recognition": {
            "operation_id": "source", "confidence": file_confidence, "coverage": {},
        }}])
    assert [key for key, p in packages.items() if p.get("preparation_issues")] == ["q2"]
    issue = packages["q2"]["preparation_issues"][0]
    assert issue["details"] == {"confidence": .4, "confidence_scope": "question"}
    assert issue["q_id"] == "q2" and issue["severity"] == "warning"


@pytest.mark.asyncio
async def test_answer_parser_scopes_confidence_to_student_and_question(monkeypatch):
    async def invoke(_provider, messages):
        student = "S1" if "S1.txt" in messages[-1].content else "S2"
        scores = [.95, .3] if student == "S1" else [.6, None]
        return SimpleNamespace(content=json.dumps({
            "stu_id": student, "stu_name": student,
            "stu_ans": [
                dict(q_id=f"q{i}", number=str(i), type="concept", content="1 + 1 = 3",
                     flag=["recognition_low_confidence"], recognition_confidence=score)
                for i, score in enumerate(scores, 1)
            ],
        }))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    sources = [
        ingest_agent.SubmissionSourceInput(
            source_id=s, stored_file_id=s, filename=f"{s}.txt",
            content_type="text/plain", text="Literal student answers",
        ) for s in ("S1", "S2")
    ]
    questions = {f"q{i}": dict(q_id=f"q{i}", number=str(i), type="concept", stem=f"Question {i}")
                 for i in (1, 2)}
    results = await ingest_agent.parse_student_answer_sources(sources, questions, SimpleNamespace())
    results = attach_submission_recognition(results, [
        SimpleNamespace(source_id=s.source_id, recognition={"confidence": "low", "coverage": {}})
        for s in sources
    ])
    actual = {}
    for result in results:
        for answer in result.student["stu_ans"]:
            actual[result.source_id, answer["q_id"]] = task_facade._answer_recognition_confidence(answer["flag"])
            assert answer["content"] == "1 + 1 = 3"  # Recognition never fixes a wrong answer.
            assert "recognition_low_confidence" not in answer["flag"]
    assert actual == {("S1", "q1"): .95, ("S1", "q2"): .3, ("S2", "q1"): .6, ("S2", "q2"): None}


def test_old_file_scores_are_not_read_as_answer_confidence():
    flags = ["recognition_confidence:0.1", "recognition_low_confidence"]
    assert task_facade._answer_recognition_confidence(flags) is None
    assert task_facade._public_answer_flags(flags) == []
    assert task_facade._effective_question_issues({"preparation_issues": [{
        "code": "recognition_needs_review", "details": {"confidence": .1},
    }]}) == []


def test_high_item_confidence_never_suppresses_real_source_failure():
    packages = {"q1": {"recognition_confidence": .99}}
    attach_recognition_review(packages, [{"recognition": {
        "operation_id": "source", "confidence": "high", "coverage": {"failed_pages": [2]},
    }}])
    assert packages["q1"]["preparation_issues"][0]["severity"] == "blocking"


def test_question_scores_survive_persistence_refresh_and_teacher_edit():
    from backend.tests.test_task_background_workflows import _seed_task

    owner, task = _seed_task()
    packages = {
        f"q{i}": dict(q_id=f"q{i}", number=str(i), type="concept", stem=f"Question {i}",
                     criterion="Award up to 10 points.", reference_answer="Reference",
                     recognition_confidence=score)
        for i, score in enumerate([.95, .4, None], 1)
    }
    attach_recognition_review(packages, [])
    task_facade._replace_draft_questions(task, owner, packages, "synthetic.txt")
    before = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    assert [p["recognition_confidence"] for p in before["problem_data"].values()] == [.95, .4, None]
    assert [q for q, p in before["problem_data"].items() if p["preparation_issues"]] == ["q2"]
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2",
                               patch={"stem": "Teacher transcribed content", "review_status": "confirmed"},
                               expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    assert after["problem_data"]["q2"]["recognition_confidence"] == .4
    assert after["problem_data"]["q2"]["review_status"] == "confirmed"


def test_merged_question_never_inherits_an_unrelated_fragment_assessment():
    from backend.tools.problem_dedup import dedupe_extracted_problems

    shared = "This is a sufficiently long literal source condition shared by both fragments."
    result = dedupe_extracted_problems({
        "q1": {"q_id": "q1", "number": "1", "stem": shared, "recognition_confidence": .2},
        "q2": {"q_id": "q2", "number": "1", "stem": "Context. " + shared + " Complete ending.",
               "recognition_confidence": .95},
    })
    assert result["q1"]["recognition_confidence"] == .95
    result = dedupe_extracted_problems({
        "q1": {"q_id": "q1", "number": "1", "stem": "Start. " + shared, "recognition_confidence": .9},
        "q2": {"q_id": "q2", "number": "1", "stem": shared + " Ending.", "recognition_confidence": .8},
    })
    assert result["q1"]["recognition_confidence"] is None
