"""Durable boundary regressions for the current preparation workflow."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.agents.ingest_agent import AICompletionCandidateOutput
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.question_preparation_artifacts import read_base_preparation_artifact, QUESTIONS_EXTRACTED_STAGE
from backend.tests.test_question_preparation_recovery import _queue_question_preparation, _claim, _RecoveryRegistry
from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks


def row(number, *, stem=None, criterion="", reference_answer=""):
    return dict(number=str(number), type="概念题", stem=stem or f"Explain item {number}.",
                criterion=criterion, reference_answer=reference_answer, max_score=10)


def generated(target, value):
    return AICompletionCandidateOutput(q_id="q1", target=target,
        target_id="q1:" + target, text_value=value)


async def setup(monkeypatch, text="one"):
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task, source_text=text)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    operation = _claim(owner, queued["job_id"], "incremental")
    inputs = api._rehydrate_question_preparation_inputs(operation)
    async def read(source, **kwargs):
        return source
    pipeline = inc.IncrementalQuestionPreparation(operation=operation, sources=inputs["sources"],
        provider=inputs["route"].provider, provider_id=inputs["recognition_provider_id"],
        reporter=ProgressReporter(operation.operation_id), score_policy=inputs["score_policy"],
        read_source=read)
    return owner, task, operation, pipeline


def mock_supplied_rule_interpretation(monkeypatch):
    from backend.services import rubric_drafts
    async def compile_rule(row, *_args, **_kwargs):
        definition = rubric_drafts.RubricDefinition(part_rubric=dict(
            id=row["q_id"], label=row["number"], stem=row["stem"], total_pts=row["max_score"],
            parts=[dict(id="p1", total_pts=row["max_score"], alternative={"deduct_per_error": 1}, steps=[dict(
                id="s1", criterion=row["criterion"], total_pts=row["max_score"])])]))
        return rubric_drafts.generated_rubric(row, definition)[1]
    monkeypatch.setattr(rubric_drafts, "compile_parts_draft", compile_rule)


def persisted(owner, operation):
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    file_id = saved.checkpoint["questions_extracted_artifact_id"]
    envelope = read_base_preparation_artifact(file_id, owner_id=owner,
        task_id=saved.assignment_id, operation_id=saved.id, attempt=saved.attempt,
        input_hash=saved.input_hash, provider_record_id="test-provider", stage=QUESTIONS_EXTRACTED_STAGE)
    return saved, envelope.payload.problem_data


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["auto", "same", "changed", "legacy_same", "legacy_changed"])
async def test_incomplete_question_reaches_provider_again_before_generating(monkeypatch, recovery):
    from backend.llm.providers import LLMResponse
    from backend.tests.part_rubric_fixtures import freeze_reply

    text = "(3) Compute the limit of x / (1 + x) as x approaches zero."
    owner, task, operation, pipeline = await setup(monkeypatch, text=text)
    prompts, providers = [], []
    failures = 1 if recovery == "auto" else 3

    async def extract(provider, messages):
        prompts.append(messages[-1].content)
        providers.append(provider)
        partial = len(prompts) <= failures
        item = row("" if partial else "3", stem=text[:-8] if partial else text)
        return LLMResponse(content=json.dumps({"problems": [{**item, "source_incomplete": partial}]}),
                           model="test", provider="test", duration_ms=1)

    monkeypatch.setattr(_RecoveryRegistry.Provider, "ainvoke", extract)
    def candidates(data, targets, *_args, **_kwargs):
        assert data["q1"]["stem"] == text
        return freeze_reply(data["q1"], [generated(t["target"], "Verified material.") for t in targets])
    generate = AsyncMock(side_effect=candidates)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    if recovery != "auto":
        with pytest.raises(ValidationError, match="Some question text is incomplete"):
            await pipeline.run()
        assert len(prompts) == 3
        generate.assert_not_awaited()
        progress = await pipeline.reporter.snapshot()
        assert not progress.failed_question_ids
        _, partial = persisted(owner, operation)
        assert [i["details"].get("reason") for i in partial["q1"]["preparation_issues"]
                if i["code"] == "recognition_partial"] == ["incomplete_question_text"]
        assert not any(i["code"] == "generation_failed" for i in partial["q1"]["preparation_issues"])
        key, = (k for k in pipeline.incomplete_units if k.startswith("extract:"))
        if recovery.startswith("legacy_"):
            # Old releases saved the fragment with a successful chunk marker.
            await pipeline.finish(key, pipeline.units[key])
            assert key not in pipeline.incomplete_units
        task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
            "question_source_incomplete", expected_lease_token=operation.lease_token)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        changed = recovery.endswith("changed")
        registry = _RecoveryRegistry("new-model" if changed else "test-model")
        if changed:
            registry.provider = _RecoveryRegistry.Provider()
        monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
        retry = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
            request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision,
                use_current_configuration=changed), background_tasks=None, current=SimpleNamespace(id=owner),
            registry=registry)
        operation = _claim(owner, retry["job_id"], "retry")

    await api.run_durable_question_preparation(operation)
    finished, rows = persisted(owner, operation)
    assert finished.status == "done", finished.error_code
    assert len(prompts) == failures + 1
    assert "previous extraction left a question incomplete" in prompts[-1]
    assert (providers[0] is providers[-1]) is (not recovery.endswith("changed"))
    generate.assert_awaited_once()
    assert list(rows) == ["q1"] and rows["q1"]["stem"] == text
    assert rows["q1"]["rubric_draft"] and rows["q1"]["reference_answer"]
    assert not any(i["code"] == "recognition_partial" for i in rows["q1"]["preparation_issues"])
    assert not finished.checkpoint["incremental_errors"] and not finished.checkpoint["incremental_incomplete_units"]


@pytest.mark.asyncio
async def test_omitting_a_fragment_during_repair_keeps_it_and_the_verified_sibling(monkeypatch):
    _, _, _, pipeline = await setup(monkeypatch)
    complete = row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")
    fragment = {**row(2, stem="First half"), "source_incomplete": True}
    fresh = row(2, stem="Now includes the missing second half.", criterion="Rule.", reference_answer="Answer.")
    extract = AsyncMock(side_effect=[inc._rows([complete, fragment]), inc._rows([complete]), inc._rows([fresh])])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    generate = AsyncMock(side_effect=AssertionError("Both questions have teacher material"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    rows = await pipeline.run()
    assert extract.await_count == 3
    assert [(r["number"], r["stem"]) for r in rows.values()] == [("1", complete["stem"]), ("2", fresh["stem"])]
    assert not any(i["code"] == "recognition_partial" for r in rows.values() for i in r["preparation_issues"])
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_unlabelled_fragment_cannot_claim_a_verified_siblings_reply(monkeypatch):
    _, _, _, pipeline = await setup(monkeypatch)
    fragment = {**row("", stem="x"), "source_incomplete": True}
    sibling = row(2, stem="Solve x + 1 = 0.", criterion="Teacher rule.", reference_answer="x = -1.")
    fixed = row(1, stem="Find the derivative of x.", criterion="Rule.", reference_answer="1.")
    extract = AsyncMock(side_effect=[
        inc._rows([fragment, sibling]), inc._rows([sibling]), inc._rows([fixed, sibling]),
    ])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    rows = await pipeline.run()
    assert extract.await_count == 3
    assert [(r["number"], r["stem"]) for r in rows.values()] == [("1", fixed["stem"]), ("2", sibling["stem"])]
    assert not any(inc._incomplete_stem(r) for r in rows.values())


@pytest.mark.asyncio
async def test_repaired_programming_stem_keeps_teacher_examples_and_code_omitted_on_reread(monkeypatch):
    code = "print(int(input()) * 2)"
    text = 'Double the integer from standard input.\n{"input":"2","expected_output":"4"}\n' + code
    _, _, _, pipeline = await setup(monkeypatch, text=text)
    fragment = {**row(1, stem="Double the integer"), "type": "编程题", "source_incomplete": True,
                "solution_code": code, "test_cases": [
                    {"input": "2", "expected_output": "4", "source": "teacher"},
                    {"input": "3", "expected_output": "7", "source": "teacher"},
                    {"input": "5", "expected_output": "10", "source": "llm_generated"},
                ]}
    completed = {**row(1, stem=text.splitlines()[0]), "type": "编程题"}
    extract = AsyncMock(side_effect=[inc._rows([fragment]), inc._rows([completed])])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    draft = pipeline.sources[0][0]
    key = pipeline._chunk_key(draft, text, 0, "")
    rows = await pipeline._extract_chunk(key, draft, text, None, "", (), ())
    assert extract.await_count == 2
    assert rows["q1"].get("solution_code") == code
    assert [(c["input"], c["expected_output"], c["source"]) for c in rows["q1"]["test_cases"]] == [
        ("2", "4", "teacher")]


@pytest.mark.asyncio
async def test_full_reread_replaces_anonymous_fragments_without_reverting_a_verified_sibling(monkeypatch):
    _, _, _, pipeline = await setup(monkeypatch)
    def supplied(number, stem):
        return row(number, stem=stem, criterion="Teacher rubric.", reference_answer="Teacher answer.")
    sibling = supplied(1, "Independent question.")
    partials = [{**row("", stem=fragment), "source_incomplete": True} for fragment in ("(a) x", "(b) y")]
    repaired = [supplied(2, "Find the integer roots."), supplied(3, "Compute the derivative.")]
    extract = AsyncMock(side_effect=[inc._rows([sibling, *partials]), inc._rows([sibling, *repaired])])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    rows = await pipeline.run()
    assert extract.await_count == 2
    assert [r["stem"] for r in rows.values()] == [sibling["stem"], *(r["stem"] for r in repaired)]
    assert not any(inc._incomplete_stem(r) for r in rows.values())


@pytest.mark.asyncio
async def test_retry_of_entirely_empty_extraction_rereads_the_source(monkeypatch):
    _, _, operation, pipeline = await setup(monkeypatch)
    extract = AsyncMock(side_effect=[{}, inc._rows([row(1, criterion="Rule.", reference_answer="Answer.")])])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    with pytest.raises(ValidationError, match="No questions"):
        await pipeline.run()
    resumed = inc.IncrementalQuestionPreparation(operation=operation, sources=pipeline.sources,
        provider=_RecoveryRegistry().provider, provider_id="test-provider", reporter=ProgressReporter(operation.operation_id),
        score_policy=pipeline.policy, read_source=pipeline.read_source)
    rows = await resumed.run()
    assert rows["q1"]["stem"] == "Explain item 1."
    assert extract.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", ["before_reply", "after_partial_write", "after_complete_write"])
async def test_interrupted_fragment_repair_uses_only_this_calls_durable_outcome(monkeypatch, stopped):
    _, _, operation, pipeline = await setup(monkeypatch, text="A self-contained exercise.")
    original_read = inc.read_base_preparation_artifact
    complete = row(1, criterion="Rule.", reference_answer="Answer.")
    partial = {**complete, "source_incomplete": True}
    calls = 0
    async def extract(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and stopped == "before_reply":
            raise asyncio.CancelledError
        return inc._rows([partial if calls == 1 or (calls == 2 and stopped == "after_partial_write") else complete])
    def crash_after_write(*args, **kwargs):
        saved = original_read(*args, **kwargs)
        if calls == 2 and stopped != "before_reply":
            raise asyncio.CancelledError
        return saved
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "read_base_preparation_artifact", crash_after_write)
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run()
    monkeypatch.setattr(inc, "read_base_preparation_artifact", original_read)
    resumed = inc.IncrementalQuestionPreparation(operation=operation, sources=pipeline.sources,
        provider=_RecoveryRegistry().provider, provider_id="test-provider", reporter=ProgressReporter(operation.operation_id),
        score_policy=pipeline.policy, read_source=pipeline.read_source)
    if stopped == "before_reply":
        with pytest.raises(RuntimeError, match="provider_submit_uncertain"):
            await resumed.run()
        assert calls == 2
    else:
        rows = await resumed.run()
        assert not inc._incomplete_stem(rows["q1"])
        assert calls == (3 if stopped == "after_partial_write" else 2)
        assert not resumed.incomplete_units


@pytest.mark.asyncio
async def test_one_chunk_is_durable_while_another_waits_and_manual_never_calls_model(monkeypatch):
    owner, task, operation, pipeline = await setup(monkeypatch)
    blocked = asyncio.Event()
    saved_first = asyncio.Event()
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["one", "two"])
    async def extract(text, *args, **kwargs):
        if text == "two":
            await blocked.wait()
        return inc._rows([row(1 if text == "one" else 2)])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    async def generate(*args, **kwargs):
        saved_first.set()
        await blocked.wait()
        return []
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(saved_first.wait(), 3)
    _, rows = persisted(owner, operation)
    assert len(rows) == 1 and rows["q1"]["stem"] == "Explain item 1."
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    from backend.services.task_execution import stop_task_run
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                  expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    provider = AsyncMock(side_effect=AssertionError("manual handoff must never invoke AI"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", provider)
    result = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert result["status"] == "ready_for_review", getattr(result, "body", result)
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_reply", [False, True])
async def test_failed_field_keeps_valid_answer_and_retries_only_rubric(monkeypatch, malformed_reply):
    from backend.tools.structured_llm import StructuredOutputInvalidError
    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(1)])))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        calls.append([target["target"] for target in targets])
        if len(calls) == 1:
            return [generated("reference_answer", "A valid explanation.")]
        if malformed_reply:
            raise StructuredOutputInvalidError("Invalid material response.")
        return []
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert rows["q1"]["reference_answer"] == "A valid explanation."
    assert not rows["q1"]["criterion"]
    assert calls == [["reference_answer", "criterion"], ["criterion"], ["criterion"]]
    assert saved.checkpoint["preparation_counts"] == dict(questions=1, stems=1, reference_answers=1, rubrics=0)
    assert not saved.checkpoint["incremental_inflight_units"]


@pytest.mark.asyncio
async def test_missing_programming_supplements_publish_review_without_manual_handoff(monkeypatch):
    mock_supplied_rule_interpretation(monkeypatch)
    owner, task, operation, _ = await setup(monkeypatch)
    original = {**row(1, criterion="Correct implementation earns full credit.", reference_answer="Supplied answer."),
                "type": "编程题", "stem": "Implement stable_softmax(xs). Return [] for an empty list."}
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([original])))
    generate = AsyncMock(return_value=[])
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert generate.await_count == 3
    assert saved.status == "done", saved.error_code
    task_view = task_facade.get_task(task_id=task, owner_id=owner)
    assert task_view["status"] == "problems_ready"
    assert not task_view.get("error")
    current = task_view["problem_data"]["q1"]
    assert current["reference_answer"] == original["reference_answer"]
    assert current["criterion"] == original["criterion"]
    assert not current.get("test_cases")
    warnings = [issue for issue in current["preparation_issues"] if issue["field"] == "programming_tests"]
    assert len(warnings) == 1 and warnings[0]["severity"] == "warning"
    assert any(issue["field"] == "programming_tests" for issue in rows["q1"]["preparation_issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("provider_submit_uncertain"),
    ValidationError("Transport failed", code="provider_upstream_unavailable")])
async def test_optional_material_transport_failure_is_not_reported_as_ready(monkeypatch, failure):
    _, _, _, pipeline = await setup(monkeypatch)
    original = {**row(1, criterion="Supplied rubric", reference_answer="Supplied answer"), "type": "编程题"}
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([original])))
    monkeypatch.setattr(inc, "generate_missing_question_materials", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await pipeline.run()


@pytest.mark.asyncio
async def test_automatic_handoff_does_not_publish_malformed_generated_unicode(monkeypatch):
    from backend.tests.test_question_material_format_repair import Provider

    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="Teacher rubric.", reference_answer="Teacher answer."),
        row(2, criterion="Supplied rubric."),
    ])))
    broken = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
              r'"target":"reference_answer","text_value":"\u8bbe $\\mathbb{Z}$ \uffa"}]}')
    pipeline.provider = Provider([broken] * 3)
    await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert len(pipeline.provider.calls) == 3
    assert rows["q1"]["reference_answer"] == "Teacher answer."
    assert rows["q2"]["reference_answer"] == ""
    assert rows["q2"]["criterion"] == "Supplied rubric."
    assert saved.checkpoint["preparation_counts"] == dict(questions=2, stems=2, reference_answers=1, rubrics=2)
    assert not any(key.startswith("generated:") for key in saved.checkpoint["incremental_unit_artifacts"])

    assert any(issue["field"] == "answer" and issue["severity"] == "warning"
               for issue in rows["q2"]["preparation_issues"])


@pytest.mark.asyncio
async def test_recovered_u_tex_is_saved_once_without_changing_teacher_material(monkeypatch):
    from backend.tests.test_question_material_format_repair import Provider

    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([
        row(1, criterion="Teacher rubric."),
        row(2, criterion="Second rubric.", reference_answer="Teacher answer."),
    ])))
    pipeline.provider = Provider([
        r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
        r'"target":"reference_answer","text_value":"\u8bbe $\underbrace{x}_{n} + \\frac{1}{2}$"}]}',
    ])
    await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert len(pipeline.provider.calls) == 1
    assert rows["q1"]["reference_answer"] == r"设 $\underbrace{x}_{n} + \frac{1}{2}$"
    assert rows["q1"]["criterion"] == "Teacher rubric."
    assert rows["q2"]["reference_answer"] == "Teacher answer."
    assert saved.checkpoint["preparation_counts"] == dict(questions=2, stems=2, reference_answers=2, rubrics=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [9, 10, 28])
async def test_real_worker_roundtrip_multiple_snapshot_sizes(monkeypatch, count):
    mock_supplied_rule_interpretation(monkeypatch)
    owner, task, operation, _ = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: [str(i) for i in range(1, count + 1)])
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(
        side_effect=lambda text, *a, **kw: inc._rows([row(int(text), criterion="Full credit for a correct explanation.",
                                                       reference_answer="Supplied answer.")])))
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert saved.status == "done", (saved.error_code, saved.checkpoint)
    assert list(rows) == [f"q{i}" for i in range(1, count + 1)]
    assert [rows[f"q{i}"]["number"] for i in range(1, count + 1)] == [str(i) for i in range(1, count + 1)]
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_count"] == count
    assert len(saved.checkpoint["incremental_unit_artifacts"]) == 2 * count


@pytest.mark.asyncio
async def test_one_chunk_failure_does_not_cancel_other_questions(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["8", "9", "10"])
    async def extract(text, *a, **kw):
        if text == "8":
            raise ValidationError("Invalid response", code="provider_response_invalid")
        return inc._rows([row(int(text), criterion="Teacher rubric", reference_answer="Teacher answer")])
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    with pytest.raises(ValidationError):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert [item["number"] for item in rows.values()] == ["9", "10"]


@pytest.mark.asyncio
async def test_retired_worker_rejects_before_resolving_any_provider(monkeypatch):
    owner, _, operation, _ = await setup(monkeypatch)
    operation.payload["manual_saved_sources"] = {"artifacts": {}}
    registry = AsyncMock(side_effect=AssertionError("retired path must not resolve provider"))
    monkeypatch.setattr(task_facade, "_registry_for_owner", registry)
    await api.run_durable_question_preparation(operation)
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    assert saved.error_code == "saved_text_recovery_retired"
    registry.assert_not_called()


@pytest.mark.asyncio
async def test_two_hundred_questions_save_independent_fields_with_bounded_current_manifest(monkeypatch):
    mock_supplied_rule_interpretation(monkeypatch)
    owner, task, operation, _ = await setup(monkeypatch)
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(i) for i in range(1, 201)])))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        from backend.services.rubric_drafts import RubricDefinition, generated_rubric, retarget_draft
        source = data["q1"]
        calls.append(source["number"])
        definition = RubricDefinition(part_rubric=dict(id="q1", stem=source["stem"], label=source["number"],
            total_pts=10, parts=[dict(id="p", total_pts=10, alternative={"deduct_per_error": 1}, steps=[dict(id="s", criterion="Explain.", total_pts=10)])]))
        criterion, draft = generated_rubric(source, definition)
        completed = {**source, "reference_answer": "A complete explanation.", "criterion": criterion,
                     "ai_completion_provenance": {target["target"]: {"source_kind":"ai_generated"} for target in targets}}
        candidates = [generated(target["target"], completed[target["target"]]) for target in targets]
        next(c for c in candidates if c.target == "criterion").rubric_draft = retarget_draft(draft, completed).model_dump(mode="json")
        return candidates
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(operation)
    saved, rows = persisted(owner, operation)
    assert saved.status == "done", (saved.error_code, len(saved.artifact_refs))
    assert len(calls) == 200 and len(set(calls)) == 200
    assert len(rows) == 200 and all(value["criterion"] and value["reference_answer"] for value in rows.values())
    assert 400 <= len(saved.artifact_refs) < 450
    assert len(saved.checkpoint["artifact_attempts"]) < 450
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_count"] == 200


@pytest.mark.asyncio
async def test_composite_subparts_from_different_chunks_remain_one_scored_question(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    await pipeline.finish("extract:" + pipeline.sources[0][0].source_token + ":0:a",
                          inc._rows([row("1(a)", stem="Compute A.")]))
    await pipeline.finish("extract:" + pipeline.sources[0][0].source_token + ":1:b",
                          inc._rows([row("1(b)", stem="Compute B.")]))
    _, rows = persisted(owner, operation)
    assert len(rows) == 1
    assert rows["q1"]["number"] == "1"
    assert "Compute A." in rows["q1"]["stem"] and "Compute B." in rows["q1"]["stem"]
    assert len(rows["q1"]["question_structure"]["subparts"]) == 2


@pytest.mark.asyncio
async def test_page_closed_question_is_saved_while_next_page_is_still_reading(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    saved = asyncio.Event()
    waiting = asyncio.Event()
    async def read(source, *, on_pages):
        await on_pages([dict(page_number=1, text="Question 1\nExplain A."),
                        dict(page_number=2, text="Question 2\nExplain B.")])
        await waiting.wait()
        return source
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(
        return_value=inc._rows([row(1, criterion="Teacher rubric.", reference_answer="Teacher answer.")])))
    original_finish = pipeline.finish
    async def finish(key, *args, **kwargs):
        await original_finish(key, *args, **kwargs)
        if key.startswith("extract:"):
            saved.set()
    pipeline.finish = finish
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(saved.wait(), 3)
        _, rows = persisted(owner, operation)
        assert rows["q1"]["stem"] and rows["q1"]["reference_answer"]
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


@pytest.mark.asyncio
async def test_independent_generation_parser_retains_valid_field_from_mixed_response(monkeypatch):
    from backend.agents import ingest_agent
    from backend.llm.providers import LLMResponse
    provider = SimpleNamespace(provider_id="test")
    response = LLMResponse(content=json.dumps({"candidates": [
        {"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer", "text_value": "Good answer."},
        {"target_id": "q1:criterion", "q_id": "q1", "target": "criterion", "text_value": {"invalid": "object"}},
    ]}), provider="test", model="test", duration_ms=1)
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    result = await ingest_agent.generate_missing_question_materials(
        inc._rows([row(1)]),
        [dict(q_id="q1", target_id="q1:" + target, target=target) for target in ("reference_answer", "criterion")],
        6, provider, allow_partial=True)
    assert [candidate.target for candidate in result] == ["reference_answer"]
    invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_question_item_cannot_erase_valid_siblings_or_claim_success(monkeypatch):
    from backend.agents import ingest_agent
    from backend.llm.providers import LLMResponse
    owner, _, operation, pipeline = await setup(monkeypatch, text="Teacher rubric. Teacher answer.")
    response = LLMResponse(content=json.dumps({"problems": [
        row(1, criterion="Teacher rubric.", reference_answer="Teacher answer."), "invalid question item",
    ]}), provider="test", model="test", duration_ms=1)
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    with pytest.raises(ingest_agent.PartialQuestionExtractionError):
        await pipeline.run()
    saved, rows = persisted(owner, operation)
    assert len(rows) == 1 and rows["q1"]["stem"] == "Explain item 1."
    assert rows["q1"]["reference_answer"] == "Teacher answer."
    assert invoke.await_count == 3
    assert any(key.startswith("extract:") for key in saved.checkpoint["incremental_incomplete_units"])
    assert not saved.checkpoint["incremental_inflight_units"]


@pytest.mark.asyncio
async def test_large_projection_recovers_from_durable_units_without_ai(monkeypatch):
    from backend.services import question_preparation_artifacts as artifacts
    from backend.services.task_execution import stop_task_run
    owner, task, operation, pipeline = await setup(monkeypatch)
    monkeypatch.setattr(artifacts, "MAX_QUESTION_PREPARATION_ARTIFACT_BYTES", 16_000)
    monkeypatch.setattr(inc, "_chunk_problem_text", lambda *_: ["1", "2"])
    extract = AsyncMock(side_effect=lambda text, *a, **kw: inc._rows([
        row(text, stem="A" * 4_500, criterion="Teacher rubric.", reference_answer="Teacher answer.")]))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    result = await pipeline.run()
    saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
    assert len(result) == 2 and len(saved.checkpoint["incremental_unit_artifacts"]) == 2
    assert saved.checkpoint["incremental_view_from_units"] is True
    assert saved.checkpoint["questions_extracted_artifact_id"] is None
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=saved.id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    extract.side_effect = AssertionError("Manual recovery must never call AI")
    response = await api.manually_complete_question_preparation(task, saved.id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner, full=True)["problem_data"]
    assert [value["number"] for value in rows.values()] == ["1", "2"]
    assert all(value["stem"] == "A" * 4_500 for value in rows.values())
    assert extract.await_count == 2


@pytest.mark.asyncio
async def test_interrupted_batch_restores_uncompacted_units_without_a_model(monkeypatch):
    from backend.services.task_execution import stop_task_run

    owner, task, operation, pipeline = await setup(monkeypatch)
    source_rows = inc._rows([row(i) for i in range(1, 35)])
    extract = AsyncMock(return_value=source_rows)
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    generation_started = asyncio.Event()

    async def wait_for_response(*args, **kwargs):
        generation_started.set()
        await asyncio.Future()

    monkeypatch.setattr(inc, "generate_missing_question_materials", wait_for_response)
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(generation_started.wait(), 10)
        saved = workflow_repository.get_operation(operation.operation_id, owner_id=owner)
        assert saved.checkpoint["incremental_view_from_units"] is True
        assert saved.checkpoint["questions_extracted_artifact_id"] is None
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
                  expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    recovered = task_facade.get_task(task_id=task, owner_id=owner, full=True)["problem_data"]
    assert [item["stem"] for item in recovered.values()] == [item["stem"] for item in source_rows.values()]
    assert extract.await_count == 1


@pytest.mark.asyncio
async def test_source_gap_without_proven_boundaries_saves_text_but_cannot_generate(monkeypatch):
    from backend.domain.errors import RecognitionError
    owner, _, operation, pipeline = await setup(monkeypatch)
    async def read(source, **kwargs):
        draft, text, payload = source
        return draft, text, {**payload, "source_error": "recognition_response_invalid",
            "recognition": {"coverage": {"processed_pages": [7, 9], "failed_pages": [8]}}}
    pipeline.read_source = read
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row(1)])))
    generate = AsyncMock(side_effect=AssertionError("An unbounded partial source cannot unlock generation"))
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises(RecognitionError):
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert rows["q1"]["stem"] == "Explain item 1."
    assert any(issue["code"] == "recognition_partial" for issue in rows["q1"]["preparation_issues"])
    assert not rows["q1"].get("reference_answer") and not rows["q1"].get("criterion")
    generate.assert_not_awaited()
