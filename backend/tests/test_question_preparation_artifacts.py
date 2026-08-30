from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from backend.db import file_repository
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.services import question_preparation_artifacts as artifacts
from backend.storage.local import LocalStorage


INPUT_HASH = hashlib.sha256(b"question-preparation-input").hexdigest()
PROVIDER_ID = "provider-record-1"
OPERATION_ID = "operation-qprep-1"


def _seed_assignment(*, owner_id: str | None = None) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    owner_id = owner_id or f"teacher_{suffix}"
    course_id = f"course_{suffix}"
    task_id = f"assignment_{suffix}"
    with session_scope() as session:
        if session.get(UserRecord, owner_id) is None:
            session.add(
                UserRecord(
                    id=owner_id,
                    username=owner_id,
                    password_hash="hash",
                    role="teacher",
                    is_active=True,
                )
            )
            session.flush()
        session.add(
            CourseRecord(
                id=course_id,
                name="Artifact Course",
                code=f"ART-{suffix}",
                teacher_id=owner_id,
            )
        )
        session.flush()
        session.add(
            AssignmentRecord(
                id=task_id,
                course_id=course_id,
                teacher_id=owner_id,
                name="Artifact Assignment",
                status="draft",
                version=1,
            )
        )
    return owner_id, task_id


def _context(owner_id: str, task_id: str, **overrides):
    values = {
        "owner_id": owner_id,
        "task_id": task_id,
        "operation_id": OPERATION_ID,
        "attempt": 2,
        "input_hash": INPUT_HASH,
        "provider_record_id": PROVIDER_ID,
    }
    values.update(overrides)
    return values


def _problem_data() -> dict[str, dict]:
    return {
        "q1": {
            "q_id": "q1",
            "number": "1",
            "type": "计算题",
            "stem": "第一大题：(a) 求 x；(b) 验证结果。",
            "criterion": "(a) 4 分\n(b) 6 分",
            "max_score": 10,
            "question_structure": {
                "contract_version": 1,
                "scoring_unit": "major_question",
                "major_number": "1",
                "major_order": 0,
                "shared_stem": "第一大题",
                "subparts": [
                    {
                        "subpart_id": "sp1",
                        "label": "(a)",
                        "order": 0,
                        "stem": "求 x",
                        "type_hint": None,
                        "source_span_ids": [],
                    },
                    {
                        "subpart_id": "sp2",
                        "label": "(b)",
                        "order": 1,
                        "stem": "验证结果",
                        "type_hint": None,
                        "source_span_ids": [],
                    },
                ],
                "structure_source": "deterministic",
                "review_status": "confirmed",
            },
            "reference_answer": "(a) x=1。\n(b) 代入验证。",
            "rubric_point_summary": {
                "contract_version": 1,
                "has_explicit_subpart_points": True,
                "items": [
                    {"subpart_id": "sp1", "label": "(a)", "points": "4"},
                    {"subpart_id": "sp2", "label": "(b)", "points": "6"},
                ],
                "total_points": "10",
                "major_max_score": "10",
                "is_valid": True,
                "issue_code": None,
            },
            "review_status": "needs_review",
            "preparation_issues": [],
        },
        "q2": {
            "q_id": "q2",
            "number": "2",
            "type": "证明题",
            "stem": "第二大题",
            "criterion": "结论与推导完整得满分",
            "max_score": 6,
            "question_structure": {
                "contract_version": 1,
                "scoring_unit": "major_question",
                "major_number": "2",
                "major_order": 1,
                "shared_stem": "第二大题",
                "subparts": [],
                "structure_source": "deterministic",
                "review_status": "confirmed",
            },
            "reference_answer": "证明略。",
            "review_status": "needs_review",
            "preparation_issues": [],
        },
    }


def _candidates() -> list[dict]:
    return [
        {
            "target_id": "q1:reference_answer",
            "q_id": "q1",
            "target": "reference_answer",
            "text_value": "(a) x=1。\n(b) 代入验证。",
            "test_cases": None,
        },
        {
            "target_id": "q1:criterion",
            "q_id": "q1",
            "target": "criterion",
            "text_value": "(a) 4 分\n(b) 6 分",
            "test_cases": None,
        },
    ]


def test_base_artifact_round_trip_and_orphan_discovery(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    problem_data = _problem_data()
    issues = {
        "q1": [
            {
                "issue_id": "issue-q1",
                "q_id": "q1",
                "field": "rubric",
                "code": "teacher_review",
            }
        ]
    }

    stored = artifacts.save_base_preparation_artifact(
        stage="uploaded_materials_aligned",
        problem_data=problem_data,
        issues=issues,
        storage=storage,
        **context,
    )

    assert stored.kind == artifacts.BASE_PREPARATION_ARTIFACT_KINDS[
        "uploaded_materials_aligned"
    ]
    assert stored.original_name == artifacts.base_preparation_artifact_name(
        OPERATION_ID, 2, "uploaded_materials_aligned"
    )
    envelope = artifacts.read_base_preparation_artifact(
        stored.id,
        stage="uploaded_materials_aligned",
        storage=storage,
        **context,
    )
    assert envelope is not None
    assert envelope.contract_version == 1
    assert envelope.stage == "uploaded_materials_aligned"
    assert list(envelope.payload.problem_data) == ["q1", "q2"]
    assert envelope.payload.problem_data["q1"]["max_score"] == 10
    subparts = envelope.payload.problem_data["q1"]["question_structure"][
        "subparts"
    ]
    assert [part["label"] for part in subparts] == [
        "(a)",
        "(b)",
    ]
    assert envelope.payload.issues == issues

    # Discovery does not rely on a checkpoint reference.  It validates the
    # deterministic file before returning the orphaned StoredFile row.
    discovered = artifacts.find_base_preparation_artifact(
        stage="uploaded_materials_aligned",
        storage=storage,
        **context,
    )
    assert discovered is not None
    assert discovered.id == stored.id


def test_question_candidate_artifact_keeps_one_complete_major_question(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)

    stored = artifacts.save_question_candidate_artifact(
        q_id="q1",
        question_order=0,
        candidates=_candidates(),
        storage=storage,
        **context,
    )

    assert stored.kind == artifacts.QUESTION_CANDIDATE_ARTIFACT_KIND
    assert "q1" in stored.original_name
    assert stored.original_name == artifacts.question_candidate_artifact_name(
        OPERATION_ID, 2, "q1", 0
    )
    envelope = artifacts.read_question_candidate_artifact(
        stored.id,
        q_id="q1",
        question_order=0,
        storage=storage,
        **context,
    )
    assert envelope is not None
    assert envelope.q_id == "q1"
    assert envelope.question_order == 0
    assert [candidate.target for candidate in envelope.payload.candidates] == [
        "reference_answer",
        "criterion",
    ]
    assert {candidate.q_id for candidate in envelope.payload.candidates} == {"q1"}

    discovered = artifacts.find_question_candidate_artifact(
        q_id="q1",
        question_order=0,
        storage=storage,
        **context,
    )
    assert discovered is not None
    assert discovered.id == stored.id


def test_final_packages_artifact_round_trip_and_discovery(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    problem_data = _problem_data()

    stored = artifacts.save_final_question_packages_artifact(
        problem_data=problem_data,
        storage=storage,
        **context,
    )

    assert stored.kind == artifacts.FINAL_QUESTION_PACKAGES_ARTIFACT_KIND
    assert stored.original_name == artifacts.final_question_packages_artifact_name(
        OPERATION_ID, 2
    )
    envelope = artifacts.read_final_question_packages_artifact(
        stored.id, storage=storage, **context
    )
    assert envelope is not None
    assert envelope.stage == "question_packages_prepared"
    assert envelope.payload.problem_data == problem_data
    discovered = artifacts.find_final_question_packages_artifact(
        storage=storage, **context
    )
    assert discovered is not None
    assert discovered.id == stored.id


@pytest.mark.parametrize(
    "provider_record_id",
    [
        "openai/gpt-4o-mini:2024-07-18",
        "provider/" + ("x" * 231),
    ],
)
def test_provider_record_id_matches_request_bound_without_entering_paths(
    tmp_path,
    provider_record_id,
):
    assert len(provider_record_id) <= 240
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(
        owner_id,
        task_id,
        provider_record_id=provider_record_id,
    )

    stored = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(),
        storage=storage,
        **context,
    )
    envelope = artifacts.read_final_question_packages_artifact(
        stored.id,
        storage=storage,
        **context,
    )

    assert envelope is not None
    assert envelope.provider_record_id == provider_record_id
    assert stored.original_name == artifacts.final_question_packages_artifact_name(
        OPERATION_ID,
        2,
    )
    assert provider_record_id not in stored.original_name
    assert provider_record_id not in stored.storage_key


def test_provider_record_id_rejects_control_characters(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")

    with pytest.raises(ValidationError) as exc_info:
        artifacts.save_final_question_packages_artifact(
            problem_data=_problem_data(),
            storage=storage,
            **_context(
                owner_id,
                task_id,
                provider_record_id="openai/gpt-4o\nsecret",
            ),
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("operation_id", "operation-qprep-other"),
        ("attempt", 3),
        ("input_hash", hashlib.sha256(b"other").hexdigest()),
        ("provider_record_id", "provider-record-other"),
    ],
)
def test_envelope_context_mismatch_is_rejected(
    tmp_path, changed_field, changed_value
):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    stored = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    mismatched = dict(context)
    mismatched[changed_field] = changed_value

    with pytest.raises(ValidationError) as exc_info:
        artifacts.read_final_question_packages_artifact(
            stored.id, storage=storage, **mismatched
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


def test_owner_and_assignment_mismatch_use_the_same_safe_error(tmp_path):
    owner_id, task_id = _seed_assignment()
    other_owner_id, other_task_id = _seed_assignment()
    _, same_owner_other_task_id = _seed_assignment(owner_id=owner_id)
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    stored = artifacts.save_base_preparation_artifact(
        stage="uploaded_materials_aligned",
        problem_data=_problem_data(),
        storage=storage,
        **context,
    )

    errors = []
    for read_context in (
        _context(other_owner_id, other_task_id),
        _context(owner_id, same_owner_other_task_id),
    ):
        with pytest.raises(ValidationError) as exc_info:
            artifacts.read_base_preparation_artifact(
                stored.id,
                stage="uploaded_materials_aligned",
                storage=storage,
                **read_context,
            )
        errors.append(exc_info.value.code)

    assert errors == [
        "question_preparation_artifact_invalid",
        "question_preparation_artifact_invalid",
    ]


@pytest.mark.parametrize(
    ("metadata_field", "metadata_value"),
    [
        ("kind", "problem_extraction_structure"),
        ("original_name", "unexpected.json"),
        ("content_type", "application/json"),
    ],
)
def test_wrong_kind_name_or_content_type_is_rejected(
    tmp_path, metadata_field, metadata_value
):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    valid = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    with storage.open(valid.storage_key) as stream:
        body = stream.read()
    metadata = {
        "kind": artifacts.FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        "original_name": artifacts.final_question_packages_artifact_name(
            OPERATION_ID, 2
        ),
        "content_type": artifacts.QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE,
    }
    metadata[metadata_field] = metadata_value
    wrong = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        assignment_id=task_id,
        content=body,
        **metadata,
    )

    with pytest.raises(ValidationError) as exc_info:
        artifacts.read_final_question_packages_artifact(
            wrong.id, storage=storage, **context
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


def test_payload_sha256_and_stored_object_sha256_are_both_verified(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    valid = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    with storage.open(valid.storage_key) as stream:
        envelope = json.loads(stream.read().decode("utf-8"))
    envelope["payload"]["problem_data"]["q1"]["max_score"] = 999
    body_with_stale_payload_digest = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    stale = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        assignment_id=task_id,
        kind=artifacts.FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        original_name=artifacts.final_question_packages_artifact_name(
            OPERATION_ID, 2
        ),
        content=body_with_stale_payload_digest,
        content_type=artifacts.QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE,
    )
    with pytest.raises(ValidationError):
        artifacts.read_final_question_packages_artifact(
            stale.id, storage=storage, **context
        )

    storage.save(valid.storage_key, b"{}")
    with pytest.raises(ValidationError):
        artifacts.read_final_question_packages_artifact(
            valid.id, storage=storage, **context
        )


def test_unknown_artifact_contract_version_is_rejected(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    valid = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    with storage.open(valid.storage_key) as stream:
        envelope = json.loads(stream.read().decode("utf-8"))
    envelope["contract_version"] = 2
    unknown_contract = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        assignment_id=task_id,
        kind=artifacts.FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        original_name=artifacts.final_question_packages_artifact_name(
            OPERATION_ID, 2
        ),
        content=json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        content_type=artifacts.QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE,
    )

    with pytest.raises(ValidationError) as exc_info:
        artifacts.read_final_question_packages_artifact(
            unknown_contract.id, storage=storage, **context
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


def test_oversized_json_is_rejected_before_parsing(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    oversized = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        assignment_id=task_id,
        kind=artifacts.FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        original_name=artifacts.final_question_packages_artifact_name(
            OPERATION_ID, 2
        ),
        content=b"{" + b" " * artifacts.MAX_QUESTION_PREPARATION_ARTIFACT_BYTES + b"}",
        content_type=artifacts.QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE,
    )

    with pytest.raises(ValidationError) as exc_info:
        artifacts.read_final_question_packages_artifact(
            oversized.id, storage=storage, **context
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "api_key",
        "providerKey",
        "client_secret",
        "bearer_token",
        "private_key",
        "full_source_text",
        "source_text",
    ],
)
def test_sensitive_credentials_and_full_source_fields_cannot_be_saved(
    tmp_path, sensitive_key
):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    problem_data = _problem_data()
    problem_data["q1"]["details"] = {sensitive_key: "must-not-be-persisted"}

    with pytest.raises(ValidationError) as exc_info:
        artifacts.save_final_question_packages_artifact(
            problem_data=problem_data,
            storage=storage,
            **_context(owner_id, task_id),
        )

    assert exc_info.value.code == "question_preparation_artifact_sensitive_field"
    assert file_repository.list_files(owner_id=owner_id, assignment_id=task_id) == []


def test_unknown_problem_fields_cannot_be_persisted(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    problem_data = _problem_data()
    problem_data["q1"]["unexpected_nested_payload"] = {"value": "ignored before"}

    with pytest.raises(ValidationError) as exc_info:
        artifacts.save_final_question_packages_artifact(
            problem_data=problem_data,
            storage=storage,
            **_context(owner_id, task_id),
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"
    assert file_repository.list_files(owner_id=owner_id, assignment_id=task_id) == []


def test_artifacts_require_contiguous_compact_major_question_ids(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    long_q_id = "q" + ("a" * 127)
    problem = dict(_problem_data()["q1"])
    problem["q_id"] = long_q_id

    with pytest.raises(ValidationError) as exc_info:
        artifacts.save_base_preparation_artifact(
            **context,
            stage=artifacts.QUESTIONS_EXTRACTED_STAGE,
            problem_data={long_q_id: problem},
            storage=storage,
        )

    assert exc_info.value.code == "question_preparation_artifact_invalid"


def test_question_candidate_identity_and_order_are_strict(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)

    with pytest.raises(ValidationError):
        artifacts.save_question_candidate_artifact(
            q_id="q1",
            question_order=0,
            candidates=[{**_candidates()[0], "q_id": "q2"}],
            storage=storage,
            **context,
        )

    stored = artifacts.save_question_candidate_artifact(
        q_id="q1",
        question_order=0,
        candidates=_candidates(),
        storage=storage,
        **context,
    )
    with pytest.raises(ValidationError):
        artifacts.read_question_candidate_artifact(
            stored.id,
            q_id="q1",
            question_order=1,
            storage=storage,
            **context,
        )


def test_base_stages_have_distinct_kind_and_name_and_never_cross_read(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    extracted = _problem_data()
    extracted["q1"]["criterion"] = ""
    aligned = _problem_data()

    extracted_artifact = artifacts.save_base_preparation_artifact(
        stage="questions_extracted",
        problem_data=extracted,
        storage=storage,
        **context,
    )
    aligned_artifact = artifacts.save_base_preparation_artifact(
        stage="uploaded_materials_aligned",
        problem_data=aligned,
        storage=storage,
        **context,
    )

    assert extracted_artifact.kind != aligned_artifact.kind
    assert extracted_artifact.original_name != aligned_artifact.original_name
    assert artifacts.find_base_preparation_artifact(
        stage="questions_extracted", storage=storage, **context
    ).id == extracted_artifact.id
    assert artifacts.find_base_preparation_artifact(
        stage="uploaded_materials_aligned", storage=storage, **context
    ).id == aligned_artifact.id
    with pytest.raises(ValidationError):
        artifacts.read_base_preparation_artifact(
            extracted_artifact.id,
            stage="uploaded_materials_aligned",
            storage=storage,
            **context,
        )


def test_deterministic_stage_is_append_only_and_idempotent(tmp_path):
    owner_id, task_id = _seed_assignment()
    storage = LocalStorage(tmp_path / "artifacts")
    context = _context(owner_id, task_id)
    first = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    replay = artifacts.save_final_question_packages_artifact(
        problem_data=_problem_data(), storage=storage, **context
    )
    assert replay.id == first.id

    changed = _problem_data()
    changed["q1"]["criterion"] = "different"
    with pytest.raises(ValidationError) as exc_info:
        artifacts.save_final_question_packages_artifact(
            problem_data=changed, storage=storage, **context
        )

    assert exc_info.value.code == "question_preparation_artifact_conflict"
    assert len(
        file_repository.list_files(owner_id=owner_id, assignment_id=task_id)
    ) == 1
