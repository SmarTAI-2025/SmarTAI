"""Assignment-source adapter shared by formal and compatibility question paths.

Recognition produces saved evidence, never questions or generated solutions.
The caller retains its existing structure/score/review transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import re
import weakref
from contextlib import asynccontextmanager
from typing import Annotated

from pydantic import Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from backend.agents.recognition_agent import RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.local_cache import RecognitionByteCache
from backend.recognition.models import EvidenceModel, RecognitionSourceRefV1
from backend.recognition.runtime import RecognitionCapacity
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.services.recognition_runs import RecognitionRunService
from backend.services.stage_provider_routing import (
    StageProviderRoute, build_owner_baidu_ocr_skill, stage_provider_configuration_fingerprint,
)
from backend.skills.recognition_reader import BaiduRecognitionEngine, LLMRecognitionEngine, PROMPT_VERSION
from backend.storage import get_storage
from backend.tools.file_processing import extract_text_from_upload, inspect_upload_content

_CAPACITIES = weakref.WeakKeyDictionary()
_CACHE = RecognitionByteCache()
_VISUAL_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"}


def recognition_capacity():
    loop = asyncio.get_running_loop()
    if loop not in _CAPACITIES:
        _CAPACITIES[loop] = RecognitionCapacity()
    return _CAPACITIES[loop]


@asynccontextmanager
async def recognition_engine(*, owner_id, route, registry):
    """One reusable engine adapter; the caller freezes and authorizes the route."""
    skill, engine = None, None
    if route is not None and route.is_baidu_ocr:
        fingerprint = stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
        skill = build_owner_baidu_ocr_skill(owner_id, route)
        engine = BaiduRecognitionEngine(skill.client, route_id=route.route_id, fingerprint=fingerprint, max_document_pages=1)
    elif route is not None and route.provider is not None and getattr(route.provider, "supports_vision", False):
        fingerprint = stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
        engine = LLMRecognitionEngine(route.provider, route_id=route.route_id, fingerprint=fingerprint)
    try:
        yield engine
    finally:
        if skill is not None:
            await skill.client.aclose()


Page = Annotated[int, Field(strict=True, ge=1, le=10000)]
Target = Annotated[str, Field(min_length=1, max_length=80)]


class QuestionRecognitionOptionsV1(EvidenceModel):
    pages: list[Page] = Field(default_factory=list, max_length=24)
    targets: list[Target] = Field(default_factory=list, max_length=64)
    search_start_page: Page = 1
    search_window_pages: int = Field(default=500, strict=True, ge=1, le=500)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.pages != sorted(set(self.pages)) or len(self.targets) != len(set(self.targets)):
            raise ValueError("recognition scope must be unique and ordered")
        if any(not target.strip() or any(c in target for c in "\r\n\x00") for target in self.targets):
            raise ValueError("invalid target")
        return self


def _update_options(options, **changes):
    try:
        return QuestionRecognitionOptionsV1.model_validate({**options.model_dump(), **changes})
    except ValidationError:
        raise RecognitionError("recognition_request_invalid") from None


def question_recognition_options(value=None, *, extraction_hint=""):
    try:
        options = (QuestionRecognitionOptionsV1.model_validate_json(value) if isinstance(value, str) and value.strip()
                   else QuestionRecognitionOptionsV1.model_validate(value or {}))
    except (ValidationError, ValueError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None
    # Only explicit hierarchical question IDs are inferred; ordinary prose,
    # decimal quantities and arbitrary integers must not choose source pages.
    explicit = re.search(r"(?:题号|question\s+numbers?|exercises?)\s*[:：]?\s*([0-9.,，\s\-–]+)", extraction_hint, re.I)
    if not options.targets and explicit:
        targets = []
        for part in re.split(r"[,，\s]+", explicit.group(1).strip()):
            if not part:
                continue
            parts = re.split(r"[-–]", part)
            if len(parts) == 2 and all(item.isdigit() for item in parts):
                start, end = map(int, parts)
                if not 1 <= start <= end <= 10000 or end - start >= 64:
                    raise RecognitionError("recognition_request_invalid")
                targets.extend(str(number) for number in range(start, end + 1))
            elif len(parts) == 1 and re.fullmatch(r"\d+(?:\.\d+)*", part):
                targets.append(part)
            else:
                raise RecognitionError("recognition_request_invalid")
        options = _update_options(options, targets=list(dict.fromkeys(targets)))
    if not options.targets and re.search(r"题|exercise|question", extraction_hint, re.I):
        targets = list(dict.fromkeys(re.findall(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])", extraction_hint)))
        options = _update_options(options, targets=targets)
    if not options.pages:
        match = re.search(r"(?:页码|pages?)\s*[:：]?\s*([0-9 ,，\-]+)", extraction_hint, re.I)
        if match:
            pages = set()
            for part in re.split(r"[,，\s]+", match.group(1).strip()):
                if not part:
                    continue
                ends = part.split("-")
                if len(ends) > 2 or not all(end.isdigit() for end in ends):
                    raise RecognitionError("recognition_request_invalid")
                start, end = int(ends[0]), int(ends[-1])
                if not 1 <= start <= end <= 10000 or end - start >= 24:
                    raise RecognitionError("recognition_request_invalid")
                pages.update(range(start, end + 1))
            try:
                options = QuestionRecognitionOptionsV1.model_validate({**options.model_dump(), "pages": sorted(pages)})
            except ValidationError:
                raise RecognitionError("recognition_request_invalid") from None
    return options


@dataclass(frozen=True)
class QuestionSourceRead:
    text: str
    recognition: dict | None = None
    stored_file_id: str | None = None


async def read_question_source(*, owner_id, task_id, content, filename, content_type=None,
                               route: StageProviderRoute, registry, stored_file_id=None,
                               extraction_hint="", options=None, purpose="problems", reporter=None, text_reader=None,
                               allow_vision=None, binding=None):
    from backend.services.source_files import persist_problem_source

    scope = question_recognition_options(options, extraction_hint=extraction_hint)
    allow_vision = purpose not in {"rubric", "test_cases"} if allow_vision is None else allow_vision is True
    reporter = reporter or ProgressReporter(f"recognition-{task_id}")
    inspection = inspect_upload_content(content, filename, content_type)
    media_type = inspection.content_type
    if media_type not in _VISUAL_TYPES:
        text = await (text_reader or extract_text_from_upload)(content, filename, purpose=purpose, reporter=reporter)
        return QuestionSourceRead(text, stored_file_id=stored_file_id)
    if not allow_vision and media_type.startswith("image/"):
        raise RecognitionError("material_ocr_confirmation_required")
    if stored_file_id is None:
        if binding is not None and binding.link != "assignment":
            raise RecognitionError("recognition_source_unavailable")
        stored, _ = await run_in_threadpool(
            persist_problem_source, storage=get_storage(), owner_id=owner_id, task_id=task_id,
            original_name=filename, content=content, content_type=media_type,
        )
        stored_file_id = stored.id
    source = RecognitionSourceRefV1(
        owner_id=owner_id, scope="submission_source" if purpose == "submissions" else "assignment_source",
        business_id=binding.business_id if binding is not None else task_id, stored_file_id=stored_file_id,
        original_name=filename, content_type=media_type, input_sha256=hashlib.sha256(content).hexdigest(),
    )
    request = RecognitionReadRequestV1(
        source=source, purpose=purpose, scope="targets" if scope.targets else "pages" if scope.pages else "document",
        targets=scope.targets, pages=scope.pages if not scope.targets else [],
        page_hints={target: scope.pages for target in scope.targets} if scope.targets and scope.pages else {},
        search_start_page=scope.search_start_page, search_window_pages=scope.search_window_pages,
    )
    async with recognition_engine(owner_id=owner_id, route=route if allow_vision else None, registry=registry) as engine:
        if media_type.startswith("image/") and engine is None:
            raise RecognitionError("provider_vision_not_supported" if route.provider is not None else "visual_capability_unavailable")
        run = await RecognitionRunService(store=RecognitionArtifactStore(get_storage()), capacity=recognition_capacity(),
                                           cache=_CACHE, progress=reporter).run(
            request, content, engine=engine, prompt_version=PROMPT_VERSION, authorized_owner_id=owner_id, binding=binding,
        )
    if run.status == "already_running":
        raise RecognitionError("recognition_already_running")
    assembly = run.assembly
    if assembly is None or assembly.document is None:
        raise RecognitionError(run.safe_error_code or "recognition_artifact_unavailable")
    document = assembly.document
    text = document.final_markdown
    if not text.strip():
        if not allow_vision and run.safe_error_code == "visual_capability_unavailable":
            raise RecognitionError("material_ocr_confirmation_required")
        if run.safe_error_code == "visual_capability_unavailable" and route.provider is not None:
            raise RecognitionError("provider_vision_not_supported")
        raise RecognitionError(run.safe_error_code or assembly.safe_error_code or "ocr_empty_result")
    summary = dict(
        schema_version=1, operation_id=run.operation_id, status=run.status,
        artifact_ids=list(run.artifact_ids), coverage=document.coverage.model_dump(mode="json"),
        confidence=document.confidence, warning_codes=document.warning_codes,
        confidence_reasons=document.confidence_reasons, error_code=run.safe_error_code,
        usage=run.current_usage.model_dump(mode="json"), operation_usage=run.operation_usage.model_dump(mode="json"),
        requires_review=True,
    )
    return QuestionSourceRead(text, summary, stored_file_id)


def recognition_needs_review(summary):
    if not summary:
        return False
    coverage = summary.get("coverage") or {}
    return bool(summary.get("confidence") == "low" or summary.get("error_code") or any(
        coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets", "unverified_targets")
    ))


def attach_recognition_review(packages, sources):
    """Carry recognition uncertainty into the existing final review, idempotently."""
    for source in sources:
        summary = source.get("recognition")
        if not summary:
            continue
        coverage = summary.get("coverage") or {}
        partial = any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets", "unverified_targets"))
        if not partial and summary.get("confidence") != "low" and not summary.get("error_code"):
            continue
        for q_id, problem in packages.items():
            issue_id = "recognition_" + hashlib.sha256(f"{summary.get('operation_id')}:{q_id}".encode()).hexdigest()[:24]
            issues = problem.setdefault("preparation_issues", [])
            if any(item.get("issue_id") == issue_id for item in issues):
                continue
            issues.append(dict(
                issue_id=issue_id, q_id=q_id, field="stem", severity="warning", status="open",
                code="recognition_partial" if partial else "recognition_needs_review",
                source_ids=[summary["operation_id"]], details=dict(coverage=coverage,
                    artifact_ids=summary.get("artifact_ids", []), error_code=summary.get("error_code")),
            ))
