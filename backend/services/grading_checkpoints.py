"""Lease-fenced, per-expert checkpoints; no provider calls during stop/review."""
from contextvars import ContextVar
import time

from sqlalchemy import select

from backend.db import grading_repository as repository
from backend.db.models import AssignmentQuestionRecord, GradeResultRecord, GradingRunEventRecord, GradingRunSubmissionRecord, ProviderConfigRecord
from backend.db.workflow_repository import GradingRunSetupRecord
from backend.domain.education import QuestionDTO
from backend.models import Correction, ExpertResult, ProblemInfo, TaskGradingSetup
from backend.services.math_grading_context import (
    MATH_GRADING_ENGINE_VERSION, PLAN_EVENT, expert_matches_plan,
    matching_math_plans, plans_from_snapshots, problem_from_question, uses_math_grading,
)

_current = ContextVar("grading_checkpoints", default=None)
EVENT = "grading_expert_checkpoint"
MATH_TRACE_EVENT = "grading_math_final_trace"


class CheckpointWriteError(RuntimeError):
    """A storage failure must not be mistaken for a model's grading failure."""


def current_checkpoints():
    return _current.get()


def checkpoint_key(item):
    return (item["student_id"], item["q_id"], item["provider_id"], item["sample_index"])


def saved_experts(run_id):
    """Include inherited successes so repeated stops retain the same paid work."""
    from backend.db import workflow_repository
    setup = workflow_repository.get_run_setup(run_id)
    saved = {checkpoint_key(item): item for item in
             (setup.input_manifest if setup else {}).get("reused_experts", [])}
    events = repository.list_events(run_id)
    for event in events:
        if event["message"] == EVENT:
            saved[checkpoint_key(event["payload"])] = event["payload"]
    manifest = (setup.input_manifest if setup else {}) or {}
    math_qids = {q["q_id"] for q in manifest.get("questions", []) if uses_math_grading(q.get("type", ""))}
    plans = plans_from_snapshots(manifest, events)
    if manifest.get("questions") and all("assignment_id" in q for q in manifest["questions"]):
        plans = matching_math_plans(
            [QuestionDTO.model_validate(q) for q in manifest["questions"]], plans,
        )
    return [item for item in saved.values() if expert_matches_plan(item, math_qids, plans)]


def saved_math_plans(run_id, *, questions=None):
    from backend.db import workflow_repository

    setup = workflow_repository.get_run_setup(run_id)
    plans = plans_from_snapshots(
        (setup.input_manifest if setup else {}) or {}, repository.list_events(run_id),
    )
    return matching_math_plans(questions, plans) if questions is not None else plans


class GradingCheckpoints:
    def __init__(self, *, run_id, worker_id, questions, frozen_revisions):
        self.run_id, self.worker_id = run_id, worker_id
        self.questions = {q.q_id: q for q in questions}
        self.revisions = {student: revision.id for revision, student in frozen_revisions}
        self.math_plans = saved_math_plans(run_id, questions=questions)
        math_qids = {q.q_id for q in questions if uses_math_grading(q.type)}
        self.saved = {checkpoint_key(item): item for item in saved_experts(run_id)
                      if expert_matches_plan(item, math_qids, self.math_plans)}

    def get_math_plan(self, problem):
        from backend.services.math_rubric_plan import math_rubric_fingerprint

        plan = self.math_plans.get(problem.q_id)
        if (plan is not None and plan.input_fingerprint == math_rubric_fingerprint(problem)
                and plan.max_score == problem.max_score):
            return plan.model_copy(deep=True)
        return None

    def save_math_plan(self, problem, plan):
        from backend.services.math_rubric_plan import math_rubric_fingerprint

        if (plan.input_fingerprint != math_rubric_fingerprint(problem)
                or plan.max_score != problem.max_score):
            raise CheckpointWriteError("math_plan_input_mismatch")
        existing = self.math_plans.get(problem.q_id)
        if existing is not None:
            if existing.plan_id != plan.plan_id:
                raise CheckpointWriteError("math_plan_already_frozen")
            return
        try:
            repository.record_event(
                self.run_id, worker_id=self.worker_id, level="info", message=PLAN_EVENT,
                payload={"engine_version": MATH_GRADING_ENGINE_VERSION, "q_id": problem.q_id,
                         "plan": plan.model_dump(mode="json")},
            )
        except Exception as exc:
            raise CheckpointWriteError("math_plan_checkpoint_failed") from exc
        self.math_plans[problem.q_id] = plan.model_copy(deep=True)

    async def seed_activity(self, observer, registry, setup):
        """Count completed reused pairs without inventing a new model call/ETA."""
        if setup is None:
            return
        from backend.services.execution_control import provider_key
        from backend.progress.grading_activity import public_models
        providers = {key: registry.get(key) for key in setup.selected_provider_ids}
        sample_count = setup.multi_sample_n if len(providers) == 1 else 1
        observer.configure([(provider, index) for provider in providers.values()
                            for index in range(sample_count)])
        completed_pairs = set()
        for result in repository.list_results_for_run(self.run_id):
            completed_pairs.add((result.student_id, result.q_id))
            for expert in result.ai_expert_results:
                provider = providers.get(expert.get("provider_id"))
                if provider is not None:
                    model = observer.models[provider_key(provider)]
                    kind = expert.get("error_kind")
                    model["cancelled" if kind == "operation_cancelled" else "failed" if kind else "succeeded"] += 1
        from backend.agents.multi_expert import _is_successful_expert
        for item in self.saved.values():
            provider = providers.get(item["provider_id"])
            if (provider is not None and (item["student_id"], item["q_id"]) not in completed_pairs
                    and _is_successful_expert(ExpertResult.model_validate(item["result"]))):
                observer.models[provider_key(provider)]["succeeded"] += 1
        await observer.reporter.publish_grading_activity({
            "model_progress": public_models(list(observer.models.values()))})

    async def run(self, awaitable):
        token = _current.set(self)
        try:
            return await awaitable
        finally:
            _current.reset(token)

    def get(self, student_id, q_id, provider_id, sample_index):
        from backend.agents.multi_expert import _is_successful_expert
        item = self.saved.get((student_id, q_id, provider_id, sample_index))
        if item and not expert_matches_plan(
            item, {qid for qid, q in self.questions.items() if uses_math_grading(q.type)}, self.math_plans,
        ):
            return None
        result = ExpertResult.model_validate(item["result"]) if item else None
        return result if result is not None and _is_successful_expert(result) else None

    def save_expert(self, student_id, q_id, provider_id, sample_index, result):
        item = dict(student_id=student_id, q_id=q_id, provider_id=provider_id,
                    sample_index=sample_index, result=result.model_dump(mode="json"))
        if uses_math_grading(self.questions[q_id].type):
            plan = self.math_plans.get(q_id)
            if plan is None:
                # Direct skill invocations use the same deterministic rubric
                # fallback. Freeze it too; never infer rules from the result.
                from backend.services.math_grading_context import get_math_plan
                from backend.services.math_rubric_plan import fallback_math_rubric_plan
                problem = problem_from_question(self.questions[q_id])
                plan = get_math_plan(problem) or fallback_math_rubric_plan(problem)
                self.save_math_plan(problem, plan)
            item.update(math_grading_engine_version=MATH_GRADING_ENGINE_VERSION, math_plan_id=plan.plan_id)
        try:
            repository.record_event(self.run_id, worker_id=self.worker_id, level="info",
                                    message=EVENT, payload=item)
        except Exception as exc:
            raise CheckpointWriteError("expert_checkpoint_failed") from exc
        self.saved[checkpoint_key(item)] = item

    def save_correction(self, student_id, correction):
        from backend.services.grading_adapter import correction_to_result
        from backend.skills.math_grading import read_math_trace

        result = correction_to_result(run_id=self.run_id,
            revision_id=self.revisions[student_id], question=self.questions[correction.q_id],
            student_id=student_id, correction=correction)
        traces = [
            {"provider_id": expert.provider_id, "provider": expert.provider,
             "math_plan_id": trace.get("plan_id"), "trace": trace}
            for expert in correction.expert_results
            if (trace := read_math_trace(expert)) is not None
        ]
        try:
            if traces:
                # Expert checkpoints precede arbitration. Preserve its final
                # evidence before the public result drops internal raw_output.
                repository.record_event(
                    self.run_id, worker_id=self.worker_id, level="info",
                    message=MATH_TRACE_EVENT,
                    payload={"math_grading_engine_version": MATH_GRADING_ENGINE_VERSION,
                             "student_id": student_id, "q_id": correction.q_id,
                             "score": correction.score, "synthesis_method": correction.synthesis_method,
                             "experts": traces},
                )
            repository.upsert_result(self.run_id, worker_id=self.worker_id, grade_result=result)
        except Exception as exc:
            raise CheckpointWriteError("question_checkpoint_failed") from exc


def persist_paused_results(session, run):
    """Called under the stop transaction's run lock, before revoking its lease.

    Completed immutable results are untouched. Every remaining frozen pair gets
    a review row, including pairs with no model result. Late workers cannot add
    checkpoints after this transaction commits.
    """
    from backend.agents.multi_expert import _is_successful_expert, _weighted_average_fallback, _expert_to_correction
    from backend.services.grading_adapter import correction_to_result

    setup = session.get(GradingRunSetupRecord, run.id)
    if setup is None:
        return
    manifest = setup.input_manifest or {}
    config = TaskGradingSetup.model_validate(setup.setup)
    saved = {checkpoint_key(item): item for item in manifest.get("reused_experts", [])}
    events = list(session.scalars(select(GradingRunEventRecord).where(
        GradingRunEventRecord.grading_run_id == run.id,
    ).order_by(GradingRunEventRecord.sequence)))
    for event in events:
        if event.message == EVENT:
            saved[checkpoint_key(event.payload)] = event.payload
    plans = plans_from_snapshots(manifest, [
        {"message": event.message, "payload": event.payload} for event in events
    ])
    math_qids = {q["q_id"] for q in manifest.get("questions", []) if uses_math_grading(q.get("type", ""))}
    saved = {key: item for key, item in saved.items() if expert_matches_plan(item, math_qids, plans)}
    existing = {(row.submission_revision_id, row.question_id) for row in session.scalars(
        select(GradeResultRecord).where(GradeResultRecord.grading_run_id == run.id))}
    # A queued retry can be stopped before the worker imports its completed rows.
    inherited = {(item["submission_revision_id"], item["question_id"]): item
                 for item in manifest.get("reused_results", [])
                 if item.get("q_id") not in math_qids or item.get("q_id") in plans}
    samples = [(provider, index) for provider in config.selected_provider_ids
               for index in (range(1, config.multi_sample_n + 1)
                   if len(config.selected_provider_ids) == 1 and config.multi_sample_n > 1 else [0])]
    labels = {provider_id: display_name or model for provider_id, display_name, model in session.execute(
        select(ProviderConfigRecord.id, ProviderConfigRecord.display_name, ProviderConfigRecord.model).where(
            ProviderConfigRecord.owner_id == run.teacher_id,
            ProviderConfigRecord.id.in_(config.selected_provider_ids),
        ))}
    now = time.time()
    for frozen in session.scalars(select(GradingRunSubmissionRecord).where(
        GradingRunSubmissionRecord.grading_run_id == run.id)):
        for raw in manifest.get("questions", []):
            if "assignment_id" in raw:
                question = QuestionDTO.model_validate(raw)
            else:
                # Old manifests froze ids/versions. Stopping remains possible,
                # but never fabricate a result from a changed question.
                from backend.db.assignment_repository import _question_to_dto
                record = session.get(AssignmentQuestionRecord, raw.get("id"))
                if record is None or record.assignment_id != run.assignment_id or record.version != raw.get("version"):
                    continue
                question = _question_to_dto(record)
            pair = (frozen.submission_revision_id, question.id)
            if pair in existing:
                continue
            question_plans = matching_math_plans([question], plans)
            experts = []
            for provider, index in samples:
                item = saved.get((frozen.student_id, question.q_id, provider, index))
                if item and not expert_matches_plan(
                    item, {question.q_id} if uses_math_grading(question.type) else set(), question_plans,
                ):
                    item = None
                experts.append(ExpertResult.model_validate(item["result"]) if item else ExpertResult(
                    provider=labels.get(provider, provider) + (f"#sample{index}" if index else ""), provider_id=provider,
                    score=0, max_score=question.max_score, confidence=0,
                    comment="尚未完成批改", error_kind=("operation_cancelled" if run.status not in {"failed", "partial_failed"} else "not_completed")))
            successes = [item for item in experts if _is_successful_expert(item)]
            if successes:
                problem = ProblemInfo(q_id=question.q_id, number=question.number, type=question.type,
                                      stem=question.stem, criterion=question.criterion, max_score=question.max_score)
                correction = _weighted_average_fallback(problem, successes, grading_setup=config)
                if len(successes) == 1:
                    correction = _expert_to_correction(question.q_id, question.type, successes[0],
                        synthesis_method="degraded_to_single" if len(samples) > 1 else "single")
            else:
                correction = Correction(q_id=question.q_id, type=question.type, score=0,
                    max_score=question.max_score, confidence=0, comment="已暂停，尚未批改", steps=[],
                    synthesis_method="paused")
            correction.expert_results = experts
            correction.requires_human_review = True
            correction.review_reasons = list(dict.fromkeys([*correction.review_reasons,
                "grading_paused" if run.status not in {"failed", "partial_failed"} else "grading_incomplete",
                *(["partial_experts"] if successes and len(successes) < len(experts) else [])]))
            result = correction_to_result(run_id=run.id, revision_id=frozen.submission_revision_id,
                question=question, student_id=frozen.student_id, correction=correction)
            preserved = inherited.get(pair)
            if uses_math_grading(question.type) and question.q_id not in question_plans:
                preserved = None
            if preserved:
                from backend.domain.education import GradeResultDTO
                result = GradeResultDTO.model_validate(preserved)
            record = GradeResultRecord(id=repository._new_result_id(), grading_run_id=run.id,
                submission_revision_id=frozen.submission_revision_id, question_id=question.id,
                student_id=frozen.student_id, q_id=question.q_id,
                ai_score=result.ai_score, ai_max_score=result.ai_max_score, ai_comment=result.ai_comment,
                ai_steps=result.ai_steps, ai_confidence=result.ai_confidence,
                ai_expert_results=result.ai_expert_results, ai_synthesis_method=result.ai_synthesis_method,
                requires_review=result.requires_review, review_reasons=result.review_reasons,
                initial_requires_review=result.initial_requires_review,
                initial_review_reasons=result.initial_review_reasons, result_status=result.result_status,
                created_at=now, updated_at=now)
            session.add(record)
            if preserved and result.teacher_review:
                from backend.db.models import TeacherReviewRecord
                review = result.teacher_review
                session.add(TeacherReviewRecord(id=repository._new_review_id(), grade_result_id=record.id,
                    teacher_id=run.teacher_id, previous_score=result.ai_score, previous_comment=result.ai_comment,
                    new_score=review["new_score"], new_comment=review.get("new_comment", ""),
                    comment=review.get("new_comment", ""), confirmed=bool(review.get("confirmed")),
                    review_sequence=1, created_at=now))
