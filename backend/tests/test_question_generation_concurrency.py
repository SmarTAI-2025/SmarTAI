from __future__ import annotations

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from backend.agents import ingest_agent, question_preparation_agent
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.domain.errors import ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services.question_structure import build_major_question_structure


def _problems(count: int = 7) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for index in range(1, count + 1):
        q_id = f"q{index}"
        stem = (
            "First major question. (a) Calculate. (b) Prove."
            if index == 1
            else f"Major question {index}."
        )
        rows[q_id] = {
            "q_id": q_id,
            "number": f"1.1.{index}",
            "type": "proof",
            "stem": stem,
            "max_score": 10,
            "question_structure": build_major_question_structure(
                {
                    "q_id": q_id,
                    "number": f"1.1.{index}",
                    "stem": stem,
                },
                major_order=index - 1,
            ).model_dump(),
        }
    return rows


def _targets(problems: dict[str, dict]) -> list[dict[str, str]]:
    return [
        {
            "target_id": f"{q_id}:{target}",
            "q_id": q_id,
            "target": target,
        }
        for q_id in problems
        for target in ("reference_answer", "criterion")
    ]


def _provider(max_concurrent: int = 5):
    return SimpleNamespace(
        provider_id="fake:model",
        config=SimpleNamespace(max_concurrent=max_concurrent, rpm=0),
    )


def _candidate(target: dict[str, str]) -> AICompletionCandidateOutput:
    if target["target"] == "reference_answer":
        text = (
            "(a) Calculation.\n(b) Proof."
            if target["q_id"] == "q1"
            else "Complete model answer."
        )
    else:
        text = (
            "(a) 4 points\n(b) 6 points"
            if target["q_id"] == "q1"
            else "Step 1: 100%"
        )
    return AICompletionCandidateOutput(
        target_id=target["target_id"],
        q_id=target["q_id"],
        target=target["target"],
        text_value=text,
    )


@pytest.mark.asyncio
async def test_seven_major_questions_use_seven_bounded_calls_and_stable_order(
    monkeypatch,
):
    problems = _problems()
    targets = _targets(problems)
    active = 0
    peak = 0
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_generate(*, problems_data, requested_targets, **_kwargs):
        nonlocal active, peak
        assert len(problems_data) == 1
        q_id = next(iter(problems_data))
        calls.append((q_id, tuple(row["target_id"] for row in requested_targets)))
        active += 1
        peak = max(peak, active)
        try:
            # Later source questions finish first, proving output is not
            # accidentally ordered by completion time.
            await asyncio.sleep((8 - int(q_id[1:])) * 0.002)
            return [_candidate(row) for row in reversed(requested_targets)]
        finally:
            active -= 1

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        fake_generate,
    )
    reporter = ProgressReporter("major-concurrency")

    result = await question_preparation_agent.generate_major_question_materials(
        problems_data=problems,
        requested_targets=targets,
        test_case_count=6,
        provider=_provider(2),
        reporter=reporter,
    )

    assert Counter(q_id for q_id, _target_ids in calls) == Counter({
        q_id: 1 for q_id in problems
    })
    assert all(len(target_ids) == 2 for _q_id, target_ids in calls)
    assert peak == 2
    assert [item.target_id for item in result] == [
        row["target_id"] for row in targets
    ]
    # Q1 contains two subparts but remains one provider call and one progress
    # unit; its full structure is available inside that single request.
    assert sum(q_id == "q1" for q_id, _target_ids in calls) == 1
    snapshot = await reporter.snapshot()
    assert snapshot.stage_metrics == {
        "solution_total_questions": 7,
        "solution_completed_questions": 7,
        "solution_failed_questions": 0,
    }
    assert snapshot.completed_question_ids == list(problems)
    assert snapshot.active_question_ids == []
    assert snapshot.failed_question_ids == []
    assert snapshot.question_labels["q1"] == "1.1.1"


@pytest.mark.asyncio
async def test_byok_configured_four_way_concurrency_is_effective(monkeypatch):
    problems = _problems(4)
    entered = 0
    peak = 0
    all_entered = asyncio.Event()

    async def fake_generate(*, requested_targets, **_kwargs):
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        if entered == 4:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        return [_candidate(row) for row in requested_targets]

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        fake_generate,
    )
    reporter = ProgressReporter("configured-four")
    await question_preparation_agent.generate_major_question_materials(
        problems_data=problems,
        requested_targets=_targets(problems),
        test_case_count=6,
        provider=_provider(4),
        reporter=reporter,
    )
    assert peak == 4


@pytest.mark.asyncio
async def test_failed_major_question_is_not_counted_complete(monkeypatch):
    problems = _problems(3)

    async def fake_generate(*, problems_data, requested_targets, **_kwargs):
        q_id = next(iter(problems_data))
        await asyncio.sleep(0)
        if q_id == "q2":
            raise TimeoutError("provider timed out")
        return [_candidate(row) for row in requested_targets]

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        fake_generate,
    )
    reporter = ProgressReporter("one-failure")
    with pytest.raises(TimeoutError):
        await question_preparation_agent.generate_major_question_materials(
            problems_data=problems,
            requested_targets=_targets(problems),
            test_case_count=6,
            provider=_provider(2),
            reporter=reporter,
        )

    snapshot = await reporter.snapshot()
    assert snapshot.completed_question_ids == ["q1", "q3"]
    assert snapshot.failed_question_ids == ["q2"]
    assert snapshot.question_error_codes == {"q2": "provider_timeout"}
    assert snapshot.active_question_ids == []
    assert snapshot.stage_metrics["solution_completed_questions"] == 2
    assert snapshot.stage_metrics["solution_failed_questions"] == 1


@pytest.mark.asyncio
async def test_slow_question_does_not_block_other_major_question_progress(monkeypatch):
    problems = _problems(3)
    release = asyncio.Event()
    third_started = asyncio.Event()

    async def fake_generate(*, problems_data, requested_targets, **_kwargs):
        q_id = next(iter(problems_data))
        if q_id == "q1":
            await release.wait()
        elif q_id == "q3":
            third_started.set()
            await release.wait()
        return [_candidate(row) for row in requested_targets]

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        fake_generate,
    )
    reporter = ProgressReporter("slow-major-question")
    task = asyncio.create_task(
        question_preparation_agent.generate_major_question_materials(
            problems_data=problems,
            requested_targets=_targets(problems),
            test_case_count=6,
            provider=_provider(2),
            reporter=reporter,
        )
    )
    await asyncio.wait_for(third_started.wait(), timeout=1)
    snapshot = await reporter.snapshot()
    assert snapshot.completed_question_ids == ["q2"]
    assert snapshot.active_question_ids == ["q1", "q3"]
    assert snapshot.stage_metrics["solution_completed_questions"] == 1
    release.set()
    await task


@pytest.mark.asyncio
async def test_cancellation_clears_active_questions_without_false_failure(monkeypatch):
    problems = _problems(3)
    two_started = asyncio.Event()
    started = 0

    async def fake_generate(**_kwargs):
        nonlocal started
        started += 1
        if started == 2:
            two_started.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        fake_generate,
    )
    reporter = ProgressReporter("cancel-generation")
    task = asyncio.create_task(
        question_preparation_agent.generate_major_question_materials(
            problems_data=problems,
            requested_targets=_targets(problems),
            test_case_count=6,
            provider=_provider(2),
            reporter=reporter,
        )
    )
    await asyncio.wait_for(two_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = await reporter.snapshot()
    assert snapshot.active_question_ids == []
    assert snapshot.completed_question_ids == []
    assert snapshot.failed_question_ids == []


@pytest.mark.asyncio
async def test_missing_or_invalid_subpart_material_fails_the_major_unit(monkeypatch):
    problems = _problems(1)
    targets = _targets(problems)

    async def missing_subpart(*, requested_targets, **_kwargs):
        return [
            AICompletionCandidateOutput(
                target_id=row["target_id"],
                q_id=row["q_id"],
                target=row["target"],
                text_value=(
                    "(a) Only the first answer."
                    if row["target"] == "reference_answer"
                    else "(a) 5 points\n(b) 6 points"
                ),
            )
            for row in requested_targets
        ]

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        missing_subpart,
    )
    reporter = ProgressReporter("invalid-subpart-generation")
    with pytest.raises(ValidationError) as exc:
        await question_preparation_agent.generate_major_question_materials(
            problems_data=problems,
            requested_targets=targets,
            test_case_count=6,
            provider=SimpleNamespace(provider_id="fake:model"),
            reporter=reporter,
        )
    assert exc.value.code == "provider_response_invalid"
    snapshot = await reporter.snapshot()
    assert snapshot.completed_question_ids == []
    assert snapshot.failed_question_ids == ["q1"]


@pytest.mark.asyncio
async def test_duplicate_target_is_rejected_before_any_provider_call(monkeypatch):
    problems = _problems(1)
    targets = _targets(problems)
    called = False

    async def unexpected(**_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        question_preparation_agent,
        "generate_missing_question_materials",
        unexpected,
    )
    with pytest.raises(ValidationError) as exc:
        await question_preparation_agent.generate_major_question_materials(
            problems_data=problems,
            requested_targets=[*targets, targets[0]],
            test_case_count=6,
            provider=SimpleNamespace(provider_id="fake:model"),
            reporter=ProgressReporter("duplicate-target"),
        )
    assert exc.value.code == "provider_response_invalid"
    assert called is False


def test_question_generation_concurrency_uses_byok_and_endpoint_cap(monkeypatch):
    monkeypatch.setattr(
        question_preparation_agent.settings,
        "max_concurrent_llm_per_endpoint",
        3,
    )
    assert question_preparation_agent._major_question_generation_concurrency(
        _provider(1)
    ) == 1
    assert question_preparation_agent._major_question_generation_concurrency(
        _provider(10)
    ) == 3


def test_subpart_rubric_is_regenerated_unless_teacher_material_was_imported():
    problems = _problems(2)
    for problem in problems.values():
        problem["reference_answer"] = "Teacher-ready answer"
        problem["criterion"] = "Step 1: 100%"

    requested = question_preparation_agent.requested_major_question_materials(
        problems
    )
    assert requested == [{
        "target_id": "q1:criterion",
        "q_id": "q1",
        "target": "criterion",
    }]

    problems["q1"]["material_provenance"] = {
        "criterion": {"source_kind": "upload"}
    }
    assert question_preparation_agent.requested_major_question_materials(
        problems
    ) == []


@pytest.mark.asyncio
async def test_generation_request_carries_bounded_subpart_contract(monkeypatch):
    problems = _problems(1)
    captured = {}

    async def fake_invoke(_provider, messages):
        captured["system"] = messages[0].content
        captured["human"] = messages[1].content
        return SimpleNamespace(content=json.dumps({
            "candidates": [
                {
                    "target_id": "q1:reference_answer",
                    "q_id": "q1",
                    "target": "reference_answer",
                    "text_value": "(a) Calculation. (b) Proof.",
                },
                {
                    "target_id": "q1:criterion",
                    "q_id": "q1",
                    "target": "criterion",
                    "text_value": "(a) 4 points (b) 6 points",
                },
            ],
        }))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", fake_invoke)
    result = await ingest_agent.generate_missing_question_materials(
        problems_data=problems,
        requested_targets=_targets(problems),
        test_case_count=6,
        provider=SimpleNamespace(provider_id="fake:model"),
    )

    request = json.loads(captured["human"].split("\n", 1)[1])
    structure = request["known_problems"][0]["question_structure"]
    assert structure["scoring_unit"] == "major_question"
    assert [part["label"] for part in structure["subparts"]] == ["(a)", "(b)"]
    assert all("stem" not in part and "source_span_ids" not in part for part in structure["subparts"])
    assert "sum equals that major question's max_score" in captured["system"]
    assert [candidate.target_id for candidate in result] == [
        "q1:reference_answer",
        "q1:criterion",
    ]
