"""Strict, restart-safe artifacts for durable question preparation.

The artifact rows are ordinary owner-scoped :class:`StoredFile` records.  The
JSON inside each row is deliberately self-describing and independently
validated because a worker can discover an artifact after a process exits
between saving the object and advancing its operation checkpoint.

Only three bounded payloads are supported:

* the extracted/aligned base ``problem_data`` plus preparation issues;
* all generated candidates for one complete scored major question; and
* the final complete ``problem_data`` immediately before the transactional
  assignment commit.

Provider credentials and complete source documents are never part of these
schemas.  A frozen provider *record ID* is retained because it is safe metadata
and is required to prevent recovery from silently selecting a different
provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.db import file_repository
from backend.db.file_repository import StoredFile
from backend.domain.errors import ValidationError
from backend.models import ProblemInfo, TestCase
from backend.storage import get_storage
from backend.storage.base import StorageBackend


QUESTION_PREPARATION_ARTIFACT_CONTRACT_VERSION = 1
MAX_QUESTION_PREPARATION_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_QUESTION_PREPARATION_QUESTIONS = 200
MAX_QUESTION_PREPARATION_ISSUES = 2_000

QUESTIONS_EXTRACTED_STAGE = "questions_extracted"
UPLOADED_MATERIALS_ALIGNED_STAGE = "uploaded_materials_aligned"
BasePreparationStage: TypeAlias = Literal[
    "questions_extracted", "uploaded_materials_aligned"
]
QUESTION_CANDIDATE_STAGE = "solution_units_generated"
FINAL_QUESTION_PACKAGES_STAGE = "question_packages_prepared"

BASE_PREPARATION_ARTIFACT_KINDS: dict[BasePreparationStage, str] = {
    QUESTIONS_EXTRACTED_STAGE: "question_preparation_questions_extracted_v1",
    UPLOADED_MATERIALS_ALIGNED_STAGE: (
        "question_preparation_uploaded_materials_aligned_v1"
    ),
}
QUESTION_CANDIDATE_ARTIFACT_KIND = "question_preparation_question_v1"
FINAL_QUESTION_PACKAGES_ARTIFACT_KIND = "question_preparation_packages_v1"
QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE = (
    "application/vnd.smartai.question-preparation-artifact+json"
)

_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_INPUT_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "password",
    "secret",
    "providerkey",
    "credential",
    "credentials",
    "fullsourceplaintext",
    "fullsourcetext",
    "sourceplaintext",
    "sourcetext",
    "sourcebytes",
    "rawsource",
    "rawprovidererror",
    "rawproviderresponse",
    "bearertoken",
    "privatekey",
    "clientsecret",
    "secretkey",
    "sessiontoken",
    "idtoken",
}


def _artifact_error(
    message: str = "Question-preparation artifact validation failed.",
    *,
    code: str = "question_preparation_artifact_invalid",
) -> ValidationError:
    return ValidationError(message, code=code)


def _stable_id(value: str, *, field_name: str) -> str:
    candidate = str(value or "").strip()
    if not _STABLE_ID_RE.fullmatch(candidate):
        raise _artifact_error(f"{field_name} is not a stable identifier.")
    return candidate


def _provider_record_identifier(value: str) -> str:
    """Validate the frozen route ID without treating it as a file-path part.

    Stage-provider request DTOs and the workflow columns allow 240 characters,
    and model-backed route IDs may legitimately contain ``/``.  Artifact file
    names remain derived only from operation/attempt/stage, so this value is
    preserved exactly in JSON and is never interpolated into a storage path.
    """

    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > 240
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise _artifact_error("provider_record_id is invalid.")
    return candidate


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _artifact_error() from exc


def _payload_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _load_strict_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _artifact_error() from exc
    if not isinstance(value, dict):
        raise _artifact_error()
    return value


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _is_sensitive_key(value: object) -> bool:
    key = _normalized_key(value)
    return (
        key in _SENSITIVE_KEYS
        or key.endswith("apikey")
        or key.endswith("secret")
        or key.endswith("credential")
        or key.endswith("credentials")
        or "fullsource" in key
        or key.startswith("rawprovider")
    )


def _assert_no_sensitive_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_sensitive_key(key):
                raise _artifact_error(
                    "Question-preparation artifacts cannot contain credentials or full sources.",
                    code="question_preparation_artifact_sensitive_field",
                )
            _assert_no_sensitive_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_sensitive_fields(nested)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactProblemV1(ProblemInfo):
    """Explicit allow-list for one persisted major-question package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    q_id: str = Field(pattern=r"^q[1-9][0-9]{0,2}$", max_length=4)
    max_score_source: str | None = Field(default=None, max_length=128)
    max_score_review_status: Literal[
        "needs_review", "edited", "confirmed"
    ] | None = None
    material_provenance: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )
    ai_completion_provenance: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )
    preparation_issues: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_QUESTION_PREPARATION_ISSUES,
    )


def _normalize_problem_data(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("problem data must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for q_id, problem in value.items():
        if not isinstance(q_id, str) or not isinstance(problem, Mapping):
            raise ValueError("problem data question identity mismatch")
        parsed = ArtifactProblemV1.model_validate(dict(problem))
        normalized[q_id] = parsed.model_dump(
            mode="json",
            exclude_unset=True,
        )
    return normalized


class BasePreparationPayloadV1(_StrictModel):
    problem_data: dict[str, dict[str, Any]]
    issues: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    @field_validator("problem_data", mode="before")
    @classmethod
    def _normalize_problems(cls, value: Any):
        return _normalize_problem_data(value)

    @model_validator(mode="after")
    def _validate_problem_identity_and_bounds(self):
        _validate_problem_data(self.problem_data)
        if sum(len(items) for items in self.issues.values()) > MAX_QUESTION_PREPARATION_ISSUES:
            raise ValueError("too many preparation issues")
        if not set(self.issues).issubset(self.problem_data):
            raise ValueError("preparation issues reference an unknown question")
        for q_id, rows in self.issues.items():
            for row in rows:
                issue_q_id = row.get("q_id")
                if issue_q_id not in (None, q_id):
                    raise ValueError("preparation issue question identity mismatch")
        return self


class QuestionMaterialCandidateV1(_StrictModel):
    target_id: str = Field(min_length=1, max_length=160)
    q_id: str = Field(pattern=r"^q[1-9][0-9]{0,2}$", max_length=4)
    target: Literal[
        "criterion", "reference_answer", "solution_code", "test_cases"
    ]
    text_value: str | None = Field(default=None, max_length=1_000_000)
    test_cases: list[TestCase] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _validate_target_value(self):
        if self.target_id != f"{self.q_id}:{self.target}":
            raise ValueError("candidate target identity mismatch")
        if self.target == "test_cases":
            if not self.test_cases or (self.text_value or "").strip():
                raise ValueError("test-case candidates require structured cases only")
        elif not (self.text_value or "").strip() or self.test_cases:
            raise ValueError("text candidates require non-empty text only")
        return self


class QuestionCandidatePayloadV1(_StrictModel):
    candidates: list[QuestionMaterialCandidateV1] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _validate_unique_targets(self):
        target_ids = [candidate.target_id for candidate in self.candidates]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("candidate targets must be unique")
        return self


class FinalQuestionPackagesPayloadV1(_StrictModel):
    problem_data: dict[str, dict[str, Any]]

    @field_validator("problem_data", mode="before")
    @classmethod
    def _normalize_problems(cls, value: Any):
        return _normalize_problem_data(value)

    @model_validator(mode="after")
    def _validate_problem_identity_and_bounds(self):
        _validate_problem_data(self.problem_data)
        return self


def _validate_problem_data(problem_data: Mapping[str, Mapping[str, Any]]) -> None:
    if not problem_data or len(problem_data) > MAX_QUESTION_PREPARATION_QUESTIONS:
        raise ValueError("problem data must contain a bounded non-empty question set")
    for expected_order, (q_id, problem) in enumerate(problem_data.items()):
        _stable_id(q_id, field_name="q_id")
        if (
            q_id != f"q{expected_order + 1}"
            or not isinstance(problem, Mapping)
            or str(problem.get("q_id") or "") != q_id
        ):
            raise ValueError("problem data question identity mismatch")
        parsed = ProblemInfo.model_validate(problem)
        if (
            parsed.question_structure is None
            or parsed.question_structure.major_order != expected_order
        ):
            raise ValueError("problem data major-question order mismatch")


class _EnvelopeV1(_StrictModel):
    contract_version: Literal[1] = QUESTION_PREPARATION_ARTIFACT_CONTRACT_VERSION
    owner_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=1_000_000)
    input_hash: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    provider_record_id: str = Field(min_length=1, max_length=240)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("owner_id", "task_id", "operation_id")
    @classmethod
    def _validate_stable_ids(cls, value: str, info):
        return _stable_id(value, field_name=info.field_name)

    @field_validator("provider_record_id")
    @classmethod
    def _validate_provider_record_id(cls, value: str):
        return _provider_record_identifier(value)


class BasePreparationArtifactV1(_EnvelopeV1):
    artifact_type: Literal["base_preparation"] = "base_preparation"
    stage: BasePreparationStage
    payload: BasePreparationPayloadV1


class QuestionCandidateArtifactV1(_EnvelopeV1):
    artifact_type: Literal["major_question_candidates"] = "major_question_candidates"
    stage: Literal["solution_units_generated"] = QUESTION_CANDIDATE_STAGE
    q_id: str = Field(pattern=r"^q[1-9][0-9]{0,2}$", max_length=4)
    question_order: int = Field(ge=0, lt=MAX_QUESTION_PREPARATION_QUESTIONS)
    payload: QuestionCandidatePayloadV1

    @field_validator("q_id")
    @classmethod
    def _validate_question_id(cls, value: str):
        return _stable_id(value, field_name="q_id")

    @model_validator(mode="after")
    def _validate_candidate_question_identity(self):
        if (
            self.q_id != f"q{self.question_order + 1}"
            or any(
                candidate.q_id != self.q_id
                for candidate in self.payload.candidates
            )
        ):
            raise ValueError("candidate question identity mismatch")
        return self


class FinalQuestionPackagesArtifactV1(_EnvelopeV1):
    artifact_type: Literal["final_question_packages"] = "final_question_packages"
    stage: Literal["question_packages_prepared"] = FINAL_QUESTION_PACKAGES_STAGE
    payload: FinalQuestionPackagesPayloadV1


def _question_slug(q_id: str) -> str:
    stable_q_id = _stable_id(q_id, field_name="q_id")
    slug = _SAFE_SLUG_RE.sub("-", stable_q_id).strip(".-") or "question"
    digest = hashlib.sha256(stable_q_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:72]}-{digest}"


def base_preparation_artifact_name(
    operation_id: str,
    attempt: int,
    stage: BasePreparationStage,
) -> str:
    operation_id = _stable_id(operation_id, field_name="operation_id")
    _validate_attempt(attempt)
    stage = _validate_base_stage(stage)
    return f"{operation_id}-attempt-{attempt}-{stage}.json"


def question_candidate_artifact_name(
    operation_id: str,
    attempt: int,
    q_id: str,
    question_order: int,
) -> str:
    operation_id = _stable_id(operation_id, field_name="operation_id")
    _validate_attempt(attempt)
    _validate_question_order(question_order)
    return (
        f"{operation_id}-attempt-{attempt}-{QUESTION_CANDIDATE_STAGE}-"
        f"{question_order:03d}-{_question_slug(q_id)}.json"
    )


def final_question_packages_artifact_name(operation_id: str, attempt: int) -> str:
    operation_id = _stable_id(operation_id, field_name="operation_id")
    _validate_attempt(attempt)
    return f"{operation_id}-attempt-{attempt}-{FINAL_QUESTION_PACKAGES_STAGE}.json"


def _validate_attempt(attempt: int) -> None:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 1_000_000:
        raise _artifact_error("attempt is outside the supported range")


def _validate_question_order(question_order: int) -> None:
    if (
        isinstance(question_order, bool)
        or not isinstance(question_order, int)
        or not 0 <= question_order < MAX_QUESTION_PREPARATION_QUESTIONS
    ):
        raise _artifact_error("question order is outside the supported range")


def _validate_base_stage(stage: str) -> BasePreparationStage:
    if stage not in BASE_PREPARATION_ARTIFACT_KINDS:
        raise _artifact_error("base preparation artifact stage is invalid")
    return stage


def _normalize_context(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
) -> dict[str, Any]:
    _validate_attempt(attempt)
    normalized_hash = str(input_hash or "").strip()
    if not _INPUT_HASH_RE.fullmatch(normalized_hash):
        raise _artifact_error("input_hash must be a SHA-256 digest")
    return {
        "owner_id": _stable_id(owner_id, field_name="owner_id"),
        "task_id": _stable_id(task_id, field_name="task_id"),
        "operation_id": _stable_id(operation_id, field_name="operation_id"),
        "attempt": attempt,
        "input_hash": normalized_hash,
        "provider_record_id": _provider_record_identifier(provider_record_id),
    }


def _build_envelope(model_type, *, payload: BaseModel, **fields):
    payload_data = payload.model_dump(mode="json")
    _assert_no_sensitive_fields(payload_data)
    try:
        envelope = model_type.model_validate(
            {
                "contract_version": QUESTION_PREPARATION_ARTIFACT_CONTRACT_VERSION,
                **fields,
                "payload_sha256": _payload_sha256(payload_data),
                "payload": payload_data,
            }
        )
    except ValidationError:
        raise
    except Exception as exc:
        raise _artifact_error() from exc
    body = _canonical_json(envelope.model_dump(mode="json"))
    if len(body) > MAX_QUESTION_PREPARATION_ARTIFACT_BYTES:
        raise _artifact_error(
            "Question-preparation artifact exceeds its size limit.",
            code="question_preparation_artifact_too_large",
        )
    return envelope, body


def _storage(storage: StorageBackend | None) -> StorageBackend:
    return storage if storage is not None else get_storage()


def _save_or_reuse(
    *,
    storage: StorageBackend,
    owner_id: str,
    task_id: str,
    kind: str,
    original_name: str,
    envelope: BaseModel,
    body: bytes,
    existing: StoredFile | None,
    read_existing,
    operation_id: str,
    attempt: int,
    operation_lease_token: str | None,
) -> StoredFile:
    if existing is not None:
        existing_envelope = read_existing(existing.id)
        if existing_envelope.model_dump(mode="json") != envelope.model_dump(mode="json"):
            raise _artifact_error(
                "A different artifact already exists for this recovery stage.",
                code="question_preparation_artifact_conflict",
            )
        return existing
    return file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind=kind,
        original_name=original_name,
        content=body,
        content_type=QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE,
        assignment_id=task_id,
        fence_operation_id=(
            operation_id if operation_lease_token is not None else None
        ),
        fence_operation_attempt=(
            attempt if operation_lease_token is not None else None
        ),
        fence_lease_token=operation_lease_token,
    )


def _find_artifact(
    *,
    owner_id: str,
    task_id: str,
    kind: str,
    original_name: str,
    validate,
) -> StoredFile | None:
    matches = [
        item
        for item in file_repository.list_files(
            owner_id=owner_id,
            assignment_id=task_id,
        )
        if item.kind == kind and item.original_name == original_name
    ]
    if not matches:
        return None
    artifact = max(matches, key=lambda item: (item.created_at, item.id))
    validate(artifact.id)
    return artifact


def _read_artifact(
    file_id: object,
    *,
    storage: StorageBackend,
    owner_id: str,
    task_id: str,
    expected_kind: str,
    expected_name: str,
    model_type,
    expected_context: Mapping[str, Any],
    expected_fields: Mapping[str, Any] | None = None,
):
    if file_id is None:
        return None
    if not isinstance(file_id, str) or not file_id:
        raise _artifact_error()
    artifact = file_repository.get_file(file_id=file_id, owner_id=owner_id)
    if (
        artifact is None
        or artifact.assignment_id != task_id
        or artifact.kind != expected_kind
        or artifact.original_name != expected_name
        or artifact.content_type != QUESTION_PREPARATION_ARTIFACT_CONTENT_TYPE
        or artifact.storage_backend != getattr(storage, "name", "unknown")
        or artifact.size_bytes <= 0
        or artifact.size_bytes > MAX_QUESTION_PREPARATION_ARTIFACT_BYTES
    ):
        raise _artifact_error()
    try:
        with storage.open(artifact.storage_key) as stream:
            raw = stream.read(MAX_QUESTION_PREPARATION_ARTIFACT_BYTES + 1)
    except Exception as exc:
        raise _artifact_error() from exc
    if (
        len(raw) != artifact.size_bytes
        or len(raw) > MAX_QUESTION_PREPARATION_ARTIFACT_BYTES
        or hashlib.sha256(raw).hexdigest() != artifact.sha256
    ):
        raise _artifact_error()
    raw_envelope = _load_strict_json(raw)
    raw_payload = raw_envelope.get("payload")
    if not isinstance(raw_payload, dict):
        raise _artifact_error()
    _assert_no_sensitive_fields(raw_payload)
    if raw_envelope.get("payload_sha256") != _payload_sha256(raw_payload):
        raise _artifact_error()
    try:
        envelope = model_type.model_validate(raw_envelope)
    except Exception as exc:
        raise _artifact_error() from exc
    actual_context = {
        "owner_id": envelope.owner_id,
        "task_id": envelope.task_id,
        "operation_id": envelope.operation_id,
        "attempt": envelope.attempt,
        "input_hash": envelope.input_hash,
        "provider_record_id": envelope.provider_record_id,
    }
    if actual_context != dict(expected_context):
        raise _artifact_error()
    for field_name, expected in (expected_fields or {}).items():
        if getattr(envelope, field_name) != expected:
            raise _artifact_error()
    return envelope


def save_base_preparation_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    stage: BasePreparationStage,
    problem_data: Mapping[str, Mapping[str, Any]],
    issues: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    storage: StorageBackend | None = None,
    operation_lease_token: str | None = None,
) -> StoredFile:
    stage = _validate_base_stage(stage)
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    _assert_no_sensitive_fields(problem_data)
    _assert_no_sensitive_fields(issues or {})
    try:
        payload = BasePreparationPayloadV1.model_validate(
            {
                "problem_data": dict(problem_data),
                "issues": dict(issues or {}),
            }
        )
    except Exception as exc:
        raise _artifact_error() from exc
    envelope, body = _build_envelope(
        BasePreparationArtifactV1,
        payload=payload,
        artifact_type="base_preparation",
        stage=stage,
        **context,
    )
    selected_storage = _storage(storage)
    existing = find_base_preparation_artifact(
        stage=stage,
        storage=selected_storage,
        **context,
    )
    return _save_or_reuse(
        storage=selected_storage,
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=BASE_PREPARATION_ARTIFACT_KINDS[stage],
        original_name=base_preparation_artifact_name(
            operation_id, attempt, stage
        ),
        envelope=envelope,
        body=body,
        existing=existing,
        read_existing=lambda file_id: read_base_preparation_artifact(
            file_id,
            stage=stage,
            storage=selected_storage,
            **context,
        ),
        operation_id=context["operation_id"],
        attempt=context["attempt"],
        operation_lease_token=operation_lease_token,
    )


def read_base_preparation_artifact(
    file_id: object,
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    stage: BasePreparationStage,
    storage: StorageBackend | None = None,
) -> BasePreparationArtifactV1 | None:
    stage = _validate_base_stage(stage)
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    return _read_artifact(
        file_id,
        storage=_storage(storage),
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        expected_kind=BASE_PREPARATION_ARTIFACT_KINDS[stage],
        expected_name=base_preparation_artifact_name(
            operation_id, attempt, stage
        ),
        model_type=BasePreparationArtifactV1,
        expected_context=context,
        expected_fields={"stage": stage},
    )


def find_base_preparation_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    stage: BasePreparationStage,
    storage: StorageBackend | None = None,
) -> StoredFile | None:
    stage = _validate_base_stage(stage)
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    selected_storage = _storage(storage)
    return _find_artifact(
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=BASE_PREPARATION_ARTIFACT_KINDS[stage],
        original_name=base_preparation_artifact_name(
            operation_id, attempt, stage
        ),
        validate=lambda file_id: read_base_preparation_artifact(
            file_id,
            stage=stage,
            storage=selected_storage,
            **context,
        ),
    )


def save_question_candidate_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    q_id: str,
    question_order: int,
    candidates: Sequence[Mapping[str, Any] | BaseModel],
    storage: StorageBackend | None = None,
    operation_lease_token: str | None = None,
) -> StoredFile:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    q_id = _stable_id(q_id, field_name="q_id")
    _validate_question_order(question_order)
    candidate_rows = [
        candidate.model_dump(mode="json")
        if isinstance(candidate, BaseModel)
        else dict(candidate)
        for candidate in candidates
    ]
    try:
        payload = QuestionCandidatePayloadV1.model_validate(
            {"candidates": candidate_rows}
        )
    except Exception as exc:
        raise _artifact_error() from exc
    envelope, body = _build_envelope(
        QuestionCandidateArtifactV1,
        payload=payload,
        artifact_type="major_question_candidates",
        stage=QUESTION_CANDIDATE_STAGE,
        q_id=q_id,
        question_order=question_order,
        **context,
    )
    selected_storage = _storage(storage)
    existing = find_question_candidate_artifact(
        q_id=q_id,
        question_order=question_order,
        storage=selected_storage,
        **context,
    )
    return _save_or_reuse(
        storage=selected_storage,
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=QUESTION_CANDIDATE_ARTIFACT_KIND,
        original_name=question_candidate_artifact_name(
            operation_id, attempt, q_id, question_order
        ),
        envelope=envelope,
        body=body,
        existing=existing,
        read_existing=lambda file_id: read_question_candidate_artifact(
            file_id,
            q_id=q_id,
            question_order=question_order,
            storage=selected_storage,
            **context,
        ),
        operation_id=context["operation_id"],
        attempt=context["attempt"],
        operation_lease_token=operation_lease_token,
    )


def read_question_candidate_artifact(
    file_id: object,
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    q_id: str,
    question_order: int,
    storage: StorageBackend | None = None,
) -> QuestionCandidateArtifactV1 | None:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    q_id = _stable_id(q_id, field_name="q_id")
    _validate_question_order(question_order)
    return _read_artifact(
        file_id,
        storage=_storage(storage),
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        expected_kind=QUESTION_CANDIDATE_ARTIFACT_KIND,
        expected_name=question_candidate_artifact_name(
            operation_id, attempt, q_id, question_order
        ),
        model_type=QuestionCandidateArtifactV1,
        expected_context=context,
        expected_fields={"q_id": q_id, "question_order": question_order},
    )


def find_question_candidate_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    q_id: str,
    question_order: int,
    storage: StorageBackend | None = None,
) -> StoredFile | None:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    q_id = _stable_id(q_id, field_name="q_id")
    _validate_question_order(question_order)
    selected_storage = _storage(storage)
    return _find_artifact(
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=QUESTION_CANDIDATE_ARTIFACT_KIND,
        original_name=question_candidate_artifact_name(
            operation_id, attempt, q_id, question_order
        ),
        validate=lambda file_id: read_question_candidate_artifact(
            file_id,
            q_id=q_id,
            question_order=question_order,
            storage=selected_storage,
            **context,
        ),
    )


def save_final_question_packages_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    problem_data: Mapping[str, Mapping[str, Any]],
    storage: StorageBackend | None = None,
    operation_lease_token: str | None = None,
) -> StoredFile:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    _assert_no_sensitive_fields(problem_data)
    try:
        payload = FinalQuestionPackagesPayloadV1.model_validate(
            {"problem_data": dict(problem_data)}
        )
    except Exception as exc:
        raise _artifact_error() from exc
    envelope, body = _build_envelope(
        FinalQuestionPackagesArtifactV1,
        payload=payload,
        artifact_type="final_question_packages",
        stage=FINAL_QUESTION_PACKAGES_STAGE,
        **context,
    )
    selected_storage = _storage(storage)
    existing = find_final_question_packages_artifact(
        storage=selected_storage, **context
    )
    return _save_or_reuse(
        storage=selected_storage,
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        original_name=final_question_packages_artifact_name(operation_id, attempt),
        envelope=envelope,
        body=body,
        existing=existing,
        read_existing=lambda file_id: read_final_question_packages_artifact(
            file_id, storage=selected_storage, **context
        ),
        operation_id=context["operation_id"],
        attempt=context["attempt"],
        operation_lease_token=operation_lease_token,
    )


def read_final_question_packages_artifact(
    file_id: object,
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    storage: StorageBackend | None = None,
) -> FinalQuestionPackagesArtifactV1 | None:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    return _read_artifact(
        file_id,
        storage=_storage(storage),
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        expected_kind=FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        expected_name=final_question_packages_artifact_name(operation_id, attempt),
        model_type=FinalQuestionPackagesArtifactV1,
        expected_context=context,
    )


def find_final_question_packages_artifact(
    *,
    owner_id: str,
    task_id: str,
    operation_id: str,
    attempt: int,
    input_hash: str,
    provider_record_id: str,
    storage: StorageBackend | None = None,
) -> StoredFile | None:
    context = _normalize_context(
        owner_id=owner_id,
        task_id=task_id,
        operation_id=operation_id,
        attempt=attempt,
        input_hash=input_hash,
        provider_record_id=provider_record_id,
    )
    selected_storage = _storage(storage)
    return _find_artifact(
        owner_id=context["owner_id"],
        task_id=context["task_id"],
        kind=FINAL_QUESTION_PACKAGES_ARTIFACT_KIND,
        original_name=final_question_packages_artifact_name(operation_id, attempt),
        validate=lambda file_id: read_final_question_packages_artifact(
            file_id, storage=selected_storage, **context
        ),
    )
