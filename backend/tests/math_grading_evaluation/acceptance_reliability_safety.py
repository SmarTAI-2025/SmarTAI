"""B2 preparation: real disposable persistence, cancellation and fault boundaries.

No other test module, scoring algorithm or parser is imported as a fake. The
provider returns the B1 raw JSON fixture; only absent KB, registry selection and
explicit storage/reporter failure injection are substituted. Production is v1
until CORE supplies an integration_ready candidate. Failures remain real failures.
"""
import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

# Fail collection before any autouse database reset can touch ambient storage.
# Normal test discovery excludes this explicit acceptance file.
if not (os.environ.get("SMARTAI_RUNTIME_ENVIRONMENT") == "test"
        and os.environ.get("SMARTAI_DATABASE_URL", "").startswith("sqlite:///")
        and "smartai-math-eval-" in os.environ.get("SMARTAI_DATABASE_URL", "")):
    raise RuntimeError("Run through tools.evaluation.math_grading.reliability_safety for disposable storage")

from backend.agents import grading_agent
from backend.config import settings
from backend.db import assignment_repository as assignments, course_repository as courses
from backend.db import grading_repository as grades, submission_repository as submissions
from backend.db import workflow_repository as workflows
from backend.db.models import AssignmentQuestionRecord, GradingRunEventRecord, UserRecord
from backend.db.provider_repository import upsert_provider_config
from backend.db.session import session_scope
from backend.domain.errors import VersionConflict
from backend.models import ProviderConfig, StudentAnswerInfo, TaskGradingSetup
from backend.progress.tracker import ProgressReporter
from backend.services import grading_adapter, grading_runs, math_grading_context as context, task_facade
from backend.services.grading_checkpoints import (
    CheckpointWriteError, GradingCheckpoints, MATH_TRACE_EVENT, current_checkpoints, saved_experts,
)
from backend.services.task_execution import stop_task_run
from tools.evaluation.math_grading.reliability import RawProvider, no_network
from tools.evaluation.math_grading.reliability_cases import CASES


ANSWER_CANARY = "SYNTHETIC_STUDENT_PRIVATE_EVIDENCE_91F6"
RULE_CANARY = "SYNTHETIC_TEACHER_PRIVATE_RULE_38A2"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # The CLI initializes settings in an empty temp directory before collection.
    # Refuse ambient storage even if this file is invoked using another command.
    assert os.environ["SMARTAI_RUNTIME_ENVIRONMENT"] == "test"
    assert "smartai-math-eval-" in os.environ["SMARTAI_DATABASE_URL"]
    monkeypatch.setattr("socket.socket.connect", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)
    monkeypatch.setattr("backend.tools.knowledge.retrieve_for_grading", AsyncMock(return_value=[]))


class Registry:
    def __init__(self, provider):
        self.provider = provider
    def select(self, *_args, **_kwargs):
        return self
    def count(self):
        return 1
    def list_available(self):
        return [self.provider]
    def pick_default(self):
        return self.provider
    def get(self, key):
        return self.provider if key == self.provider.provider_id else None


class Scenario:
    def __init__(self, task_id, owner, registry, events):
        self.task_id, self.owner, self.registry, self.events = task_id, owner, registry, events

    def start(self, key, scope="full_batch"):
        flow = workflows.get_live_workflow(self.task_id, owner_id=self.owner)
        return task_facade.start_task_grading(task_id=self.task_id, owner_id=self.owner,
            request_id=key, retry_scope=scope, expected_workflow_revision=flow.workflow_revision)["job_id"]

    def stop(self, run_id):
        flow = workflows.get_live_workflow(self.task_id, owner_id=self.owner)
        return stop_task_run(task_id=self.task_id, owner_id=self.owner, job_id=run_id,
                             expected_revision=flow.workflow_revision)

    def prepare(self, run_id, worker="b2-safety-worker"):
        grades.claim_lease(run_id, worker_id=worker, lease_seconds=120)
        frozen = workflows.get_run_setup(run_id)
        questions = grading_runs._questions_for_run(assignment_id=self.task_id, frozen_setup=frozen)
        revisions = [(submissions.get_revision(revision_id=row.id, actor_id=self.owner), row.student_id)
                     for row in grades.list_frozen_submissions(run_id)]
        cp = GradingCheckpoints(run_id=run_id, worker_id=worker, questions=questions, frozen_revisions=revisions)
        return SimpleNamespace(cp=cp, questions=questions, revisions=revisions,
                               setup=TaskGradingSetup.model_validate(frozen.setup))

    async def partial(self, prepared):
        async def run():
            plans = await context.prepare_math_plans(grading_adapter.build_problem_store(prepared.questions),
                                                     self.registry, grading_setup=prepared.setup)
            with context.math_plans_context(plans):
                question = prepared.questions[0]
                answer = prepared.revisions[0][0].answers[0]
                result = await grading_agent._grade_single_answer(
                    problem=context.problem_from_question(question),
                    answer=StudentAnswerInfo(q_id=question.q_id, number=question.number or "1",
                        type=question.type, content=answer.content),
                    student_id=prepared.revisions[0][1], registry=self.registry,
                    grading_setup=prepared.setup, language="zh", multi_sample_n=1, aggregation_method="single")
                return plans, result
        return await prepared.cp.run(run())

    async def batch(self, prepared, reporter=None):
        return await prepared.cp.run(grading_agent.grade_batch(
            problem_store=grading_adapter.build_problem_store(prepared.questions),
            student_store=grading_adapter.build_student_store(prepared.revisions),
            registry=self.registry, grading_setup=prepared.setup, reporter=reporter,
            language="zh", multi_sample_n=1, aggregation_method="single"))


@pytest.fixture
def scenario(monkeypatch):
    # Real production repositories, entirely inside the throwaway test DB.
    owner = "synthetic-b2-owner"
    with session_scope() as session:
        session.add(UserRecord(id=owner, username=owner, password_hash="not-a-real-login",
                               role="teacher", is_active=True))
    course = courses.create_course(teacher_id=owner, name="Synthetic B2")
    assignment = assignments.create_assignment(teacher_id=owner, course_id=course.id, name="Synthetic B2")
    fixture = deepcopy(next(c for c in CASES if c["id"] == "R01_real_tool_feedback"))
    for stage in ("assessment", "feedback"):
        fixture["wire"][stage]["claims"][0]["quote"] = "41\n" + ANSWER_CANARY
    for index in range(2):
        assignments.add_question(assignment_id=assignment.id, teacher_id=owner, q_id=f"q{index+1}",
            order_index=index, type="计算题", stem=fixture["problem"]["stem"],
            reference_answer="42", criterion=fixture["problem"]["criterion"] + "；" + RULE_CANARY,
            max_score=10)
    workflow = workflows.ensure_workflow(assignment_id=assignment.id, owner_id=owner)
    task_facade._commit_imported_submissions(task_id=assignment.id, owner_id=owner, course_id=course.id,
        students=[dict(stu_id="synthetic-student", stu_name="Synthetic", source_id="", source_filename="synthetic.txt",
            identity_match_method="filename", identity_status="matched", stu_ans=[
                dict(q_id=f"q{i+1}", number=str(i+1), type="计算题", content="41\n" + ANSWER_CANARY, flag=[])
                for i in range(2)])], expected_workflow_revision=workflow.workflow_revision, submission_file_name=None)
    # A deliberately fake key is created only in the disposable DB. No existing
    # credentials or BYOK registry are loaded, and the registry never constructs an SDK.
    config = upsert_provider_config(owner, ProviderConfig(provider_type="openai",
        api_key="synthetic-not-a-credential", model="synthetic-no-network", base_url="https://api.openai.com/v1"),
        master_key=settings.provider_encryption_key)
    setup = TaskGradingSetup(selected_provider_ids=[config.id], primary_provider_id=config.id,
                             knowledge_scope="none")
    workflows.update_workflow(assignment.id, owner_id=owner, grading_setup=setup.model_dump(mode="json"),
                              grading_setup_fingerprint="synthetic-approved")
    events = []
    provider = RawProvider(fixture["wire"], 0, events)
    provider.provider_id = config.id
    registry = Registry(provider)
    monkeypatch.setattr(grading_runs, "_registry_for", lambda _owner: registry)
    monkeypatch.setattr(grading_runs.settings, "e2e_fake_provider", False)
    return Scenario(assignment.id, owner, registry, events)


def test_durable_stop_two_retries_reuse_exact_plans_and_valid_zero_without_provider(scenario, record_property):
    first = scenario.start("partial")
    initial = scenario.prepare(first)
    plans, correction = asyncio.run(scenario.partial(initial))
    assert correction.score == 0
    assert len(saved_experts(first)) == 1
    assert grades.list_results_for_run(first) == []  # interrupted before aggregation save
    original_events = len(scenario.events)
    scenario.stop(first)
    rows = {r.q_id: r for r in grades.list_results_for_run(first)}
    assert rows["q1"].ai_score == 0 and rows["q2"].ai_score is None
    for index in range(2):
        run_id = scenario.start(f"retry-{index}", "failed_only")
        prepared = scenario.prepare(run_id, f"new-worker-{index}")
        restored, result = asyncio.run(scenario.partial(prepared))
        assert {qid: p.plan_id for qid, p in restored.items()} == {qid: p.plan_id for qid, p in plans.items()}
        assert result.score == 0
        assert len(scenario.events) == original_events  # neither plan nor expert re-requested
        scenario.stop(run_id)
    assert current_checkpoints() is None
    assert context.get_math_plan(context.problem_from_question(initial.questions[0])) is None
    assert {r.q_id: r.ai_score for r in grades.list_results_for_run(first)} == {"q1": 0, "q2": None}
    record_property("synthetic_provider_entries", original_events)
    record_property("retry_provider_entries", 0)


@pytest.mark.parametrize("change", ["criterion", "setup"])
def test_changed_inputs_cannot_reuse_failed_only_checkpoint(scenario, change):
    first = scenario.start("before-change")
    initial = scenario.prepare(first)
    asyncio.run(scenario.partial(initial))
    scenario.stop(first)
    calls = len(scenario.events)
    if change == "criterion":
        with session_scope() as session:
            row = session.get(AssignmentQuestionRecord, initial.questions[0].id)
            row.criterion = "每个独立错误扣1分"
    else:
        flow = workflows.get_live_workflow(scenario.task_id, owner_id=scenario.owner)
        workflows.update_workflow(scenario.task_id, owner_id=scenario.owner,
                                  grading_setup={**flow.grading_setup, "teacher_notes": "Different synthetic policy"})
    with pytest.raises(VersionConflict):
        scenario.start("unsafe-retry", "failed_only")
    fresh = scenario.start("new-whole-batch")
    manifest = workflows.get_run_setup(fresh).input_manifest
    assert not manifest.get("reused_experts") and not manifest.get("reused_results")
    assert not manifest.get("math_rubric_plans")
    assert len(scenario.events) == calls


def test_persisted_obsolete_engine_never_reuses_old_math_score_or_plan(scenario):
    first = scenario.start("obsolete")
    initial = scenario.prepare(first)
    asyncio.run(scenario.partial(initial))
    scenario.stop(first)
    with session_scope() as session:
        frozen = session.get(workflows.GradingRunSetupRecord, first)
        frozen.input_manifest = {**frozen.input_manifest, "math_grading_engine_version": "obsolete-synthetic"}
        events = session.scalars(select(GradingRunEventRecord).where(GradingRunEventRecord.grading_run_id == first))
        for event in events:
            payload = dict(event.payload or {})
            for key in ("version", "engine_version", "math_grading_engine_version"):
                if key in payload:
                    payload[key] = "obsolete-synthetic"
            event.payload = payload
    assert saved_experts(first) == []
    fresh = scenario.start("new-engine", "failed_only")
    manifest = workflows.get_run_setup(fresh).input_manifest
    assert manifest["math_grading_engine_version"] == context.MATH_GRADING_ENGINE_VERSION
    assert not manifest.get("reused_experts") and not manifest.get("reused_results")
    assert not manifest.get("math_rubric_plans")
    assert next(r for r in grades.list_results_for_run(first) if r.q_id == "q1").ai_score == 0


def test_stopped_worker_cannot_append_checkpoints(scenario):
    first = scenario.start("lease")
    initial = scenario.prepare(first)
    _, correction = asyncio.run(scenario.partial(initial))
    scenario.stop(first)
    before = grades.list_events(first)
    with pytest.raises(CheckpointWriteError):
        initial.cp.save_expert(initial.revisions[0][1], "q1", scenario.registry.provider.provider_id, 0,
                               correction.expert_results[0])
    assert grades.list_events(first) == before


@pytest.mark.parametrize("target", ["plan", "expert", "final"])
def test_storage_fault_propagates_without_publishing_a_fallback(scenario, monkeypatch, target, record_property):
    run_id = scenario.start("write-failure-" + target)
    prepared = scenario.prepare(run_id)
    actual_record, actual_upsert = grades.record_event, grades.upsert_result
    def fail_id():
        raise OperationalError("synthetic checkpoint write", {}, RuntimeError("synthetic storage fault"))
    def record(*args, **kwargs):
        message = kwargs.get("message")
        if message == {"plan": context.PLAN_EVENT, "expert": "grading_expert_checkpoint"}.get(target):
            with patch.object(grades, "_new_event_id", fail_id):
                return actual_record(*args, **kwargs)
        return actual_record(*args, **kwargs)
    def upsert(*args, **kwargs):
        if target == "final":
            with patch.object(grades, "_new_event_id", fail_id):
                return actual_upsert(*args, **kwargs)
        return actual_upsert(*args, **kwargs)
    monkeypatch.setattr(grades, "record_event", record)
    monkeypatch.setattr(grades, "upsert_result", upsert)
    with pytest.raises(CheckpointWriteError):
        asyncio.run(scenario.batch(prepared))
    assert grades.list_results_for_run(run_id) == []
    assert not [e for e in grades.list_events(run_id) if e["message"] == MATH_TRACE_EVENT]
    assert all(e["stage"] != "direct" for e in scenario.events)
    assert current_checkpoints() is None
    if target == "plan":
        assert not prepared.cp.math_plans
        assert all(e["stage"] == "plan" for e in scenario.events)
    record_property("fault_boundary", target)
    record_property("published_results", 0)


@pytest.mark.parametrize("phase", ["before_plan", "after_plan", "before_substep"])
@pytest.mark.parametrize("failure_type", [OSError, CheckpointWriteError])
def test_reporter_failure_is_not_a_holistic_plan(failure_type, phase, record_property):
    fixture = deepcopy(CASES[0])
    events = []
    provider = RawProvider(fixture["wire"], 0, events)
    class BrokenReporter(ProgressReporter):
        async def _emit_message(self, message, *args, **kwargs):
            if ((phase == "before_plan" and message.startswith("Preparing")) or
                    (phase == "after_plan" and message.startswith("Shared"))):
                raise failure_type("synthetic reporter failure")
            return await super()._emit_message(message, *args, **kwargs)
        async def substep(self, *args, **kwargs):
            if phase == "before_substep":
                raise failure_type("synthetic reporter failure")
            return await super().substep(*args, **kwargs)
    record_property("fault_boundary", phase)
    record_property("failure_type", failure_type.__name__)
    async def invoke():
        result = await context.prepare_math_plans({fixture["id"]: fixture["problem"]}, Registry(provider),
                                                 reporter=BrokenReporter("synthetic-reporting", 1, 1))
        record_property("returned_modes_instead_of_error", [r.mode for p in result.values() for r in p.routes])
        record_property("synthetic_provider_entries", len(events))
        return result
    with pytest.raises(failure_type):
        asyncio.run(invoke())
    assert all(e["stage"] == "plan" for e in events)


@pytest.mark.parametrize("failure_type", [OSError, CheckpointWriteError])
def test_assessment_reporter_failure_propagates_through_batch(scenario, failure_type, record_property):
    run_id = scenario.start("assessment-reporter-fault")
    prepared = scenario.prepare(run_id)
    class BrokenReporter(ProgressReporter):
        async def substep(self, unit, step_name, *args, **kwargs):
            if step_name == "llm_grade":
                raise failure_type("synthetic reporter failure before assessment")
            return await super().substep(unit, step_name, *args, **kwargs)
    async def invoke():
        result = await scenario.batch(prepared, BrokenReporter(run_id, 1, 2))
        record_property("returned_synthesis_instead_of_error", [c.synthesis_method
            for student in result for c in student["corrections"]])
        record_property("persisted_scores", [r.ai_score for r in grades.list_results_for_run(run_id)])
        return result
    record_property("failure_type", failure_type.__name__)
    with pytest.raises(failure_type):
        asyncio.run(invoke())
    assert grades.list_results_for_run(run_id) == []
    assert all(e["stage"] == "plan" for e in scenario.events)


def test_actual_math_worker_cancellation_reaps_process_and_releases_capacity(record_property):
    from backend.tools import math_verification as mv
    async def run():
        actual_spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []
        class ObservedProcess:
            def __init__(self, process):
                self.process = process
            def __getattr__(self, key):
                return getattr(self.process, key)
            async def communicate(self, data):
                work = asyncio.create_task(self.process.communicate(data))
                await asyncio.sleep(0)  # let real stdin transmission begin, no timed sleep
                started.set()
                return await work
        async def spawn(*args, **kwargs):
            assert "--worker" in args and any(str(a).endswith("math_verification.py") for a in args)
            process = await actual_spawn(*args, **kwargs)
            processes.append(process)
            return ObservedProcess(process)
        with patch("asyncio.create_subprocess_exec", spawn):
            task = asyncio.create_task(mv.verify_math(mv.MathVerificationRequest(
                student_expression="(x+1)^128", reference_expression="(x+1)^128")))
            try:
                await asyncio.wait_for(started.wait(), 3)
                assert processes[0].returncode is None and not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        process = processes[0]
        assert process.returncode is not None and process.returncode < 0
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        next_result = await mv.verify_math(mv.MathVerificationRequest(student_expression="2+2", reference_expression="4"))
        assert next_result.status == "supported"
        return process.returncode
    record_property("worker_returncode", asyncio.run(run()))
    record_property("worker_kind", "real production math subprocess")


def test_public_activity_pagination_and_progress_omit_private_evidence(scenario, record_property):
    run_id = scenario.start("privacy")
    prepared = scenario.prepare(run_id)
    reporter = ProgressReporter(run_id, 1, 2)
    reporter.set_event_sink(lambda event, payload: grades.record_event(run_id, worker_id=prepared.cp.worker_id,
        level=event.level, message=event.message, payload=payload))
    asyncio.run(scenario.batch(prepared, reporter))
    private = [e for e in grades.list_events(run_id) if e["message"] in {context.PLAN_EVENT, MATH_TRACE_EVENT}]
    assert private and RULE_CANARY in json.dumps(private) and ANSWER_CANARY in json.dumps(private)
    # Also probe future/legacy extra keys in an otherwise legitimate progress
    # event. The real public projection must keep its explicit field allowlist.
    grades.record_event(run_id, worker_id=prepared.cp.worker_id, level="info", message="grading_progress",
        payload=dict(prompt=RULE_CANARY, student_answer=ANSWER_CANARY, source_quote=ANSWER_CANARY,
            grading_activity=dict(code="responding", model_id="model-1", quote=ANSWER_CANARY,
                                  request=dict(source=RULE_CANARY)),
            model_progress=[dict(id="model-1", label="Synthetic", prompt=RULE_CANARY)]))
    expected_cursor = grades.list_events(run_id)[-1]["sequence"]
    pages, cursor = [], 0
    for _ in range(len(grades.list_events(run_id)) + 1):
        page = task_facade.grading_activity_history(task_id=scenario.task_id, owner_id=scenario.owner,
                                                    run_id=run_id, after=cursor, limit=1)
        pages.append(page)
        assert page["cursor"] > cursor
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert cursor == expected_cursor and any(p["events"] for p in pages)
    public = dict(history=pages, progress=task_facade._grading_progress(run_id, scenario.owner))
    encoded = json.dumps(public, ensure_ascii=False)
    assert ANSWER_CANARY not in encoded and RULE_CANARY not in encoded
    assert "math_rubric_plans" not in encoded and "raw_output" not in encoded
    for result in grades.list_results_for_run(run_id):
        correction = task_facade._serialize_correction(result)
        assert all(expert.get("raw_output") is None for expert in correction["expert_results"])
    assert all(ANSWER_CANARY not in e.model_dump_json() and RULE_CANARY not in e.model_dump_json()
               for e in reporter._events)
    record_property("private_events", len(private))
    record_property("public_history_pages", len(pages))
