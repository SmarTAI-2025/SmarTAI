"""Explicit, validated rubric fixtures for workflows after question review.

These helpers do not bypass the product gate. Tests of the gate/compiler keep
using incomplete fixtures and their own model responses.
"""
from copy import deepcopy

def _reviewed_source(q_id="q1", number="1", stem="Original", criterion="Award up to 10 points for the method and result.", question_type="概念题", max_score=10, **context):
    """Downstream workflow fixtures start with a valid reviewed scoring ledger."""
    if not criterion:
        return None
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition
    from backend.services.question_structure import build_major_question_structure
    from backend.domain.question_types import normalize_question_type
    question_type = normalize_question_type(question_type)
    row = dict(q_id=q_id, number=number, stem=stem, criterion=criterion, type=question_type, max_score=max_score, **context)
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=dict(
        id=q_id, label=number, stem=stem, total_pts=max_score, parts=[dict(id="p1", total_pts=max_score,
            steps=[dict(id="s1", criterion="Correct method and result", total_pts=max_score)],
            alternative={"deduct_per_error": 1})])))
    return {"presentation": {"rubric_draft": draft.model_dump(mode="json"),
                             "question_structure": row["question_structure"]}}



def add_reviewed_question(*args, **kwargs):
    from backend.db import assignment_repository
    from backend.services.math_grading_context import uses_math_grading
    if uses_math_grading(kwargs["type"]) and kwargs.get("criterion"):
        source = deepcopy(kwargs.get("source") or {})
        presentation = source.setdefault("presentation", {})
        if not presentation.get("rubric_draft"):
            reviewed = _reviewed_source(q_id=kwargs["q_id"], number=kwargs["number"],
                stem=kwargs["stem"], criterion=kwargs["criterion"],
                question_type=kwargs["type"], max_score=kwargs.get("max_score", 10),
                reference_answer=kwargs.get("reference_answer"))
            presentation.update(reviewed["presentation"])
        kwargs["source"] = source
    return assignment_repository.add_question(*args, **kwargs)


def install_review_compiler(monkeypatch):
    """Return a real bound draft in tests of unrelated editor/review behavior."""
    from backend.services import task_facade
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition, retarget_draft
    def compile_edit(owner_id, edit, run_async):
        p = edit.problem
        _, draft = generated_rubric(p.model_dump(mode="json"), RubricDefinition(part_rubric=dict(
            id=p.q_id, label=p.number, stem=p.stem, total_pts=p.max_score,
            parts=[dict(id="p1", total_pts=p.max_score, steps=[dict(id="s1",
                criterion="Correct method and result", total_pts=p.max_score)],
                alternative={"deduct_per_error": 1})])))
        return retarget_draft(draft, p)
    monkeypatch.setattr(task_facade, "_compile_review_edit", compile_edit)


import pytest


@pytest.fixture
def review_compiler(monkeypatch):
    install_review_compiler(monkeypatch)



def refresh_reviewed_questions(case, *, holistic=False):
    """Model question-review completion after a fixture changes scoring inputs."""
    from sqlalchemy import select
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.services.math_grading_context import problem_from_question
    from backend.services.math_rubric_plan import fallback_math_rubric_plan, _question_input
    from backend.services.rubric_drafts import RubricDraft, draft_fingerprint, generated_rubric, RubricDefinition, retarget_draft
    from backend.tests.part_rubric_fixtures import definition_for
    with session_scope() as session:
        for row in session.scalars(select(AssignmentQuestionRecord).where(AssignmentQuestionRecord.assignment_id == case[0])):
            source = deepcopy(row.source or {})
            presentation = source.setdefault("presentation", {})
            presentation.pop("rubric_draft", None)
            row.source = source
            problem = problem_from_question(row)
            if holistic and row.q_id == "q1":
                draft = RubricDraft(content_fingerprint=draft_fingerprint(problem),
                    plan=fallback_math_rubric_plan(problem, bind_preferences=False))
            else:
                context = _question_input(problem)
                if context["criterion_source"] == "ai":
                    context["ai_completion_provenance"] = {"criterion": {}}
                _, draft = generated_rubric(context, RubricDefinition.model_validate(definition_for(context)))
                draft = retarget_draft(draft, problem)
            row.source = {**source, "presentation": {**presentation, "rubric_draft": draft.model_dump(mode="json")}}
