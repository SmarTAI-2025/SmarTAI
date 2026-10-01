"""Whole-document ingestion: bounded page batches, not one giant LLM prompt."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys

from starlette.concurrency import run_in_threadpool

from backend.agents.recognition_agent import RecognitionReadRequestV1
from backend.db import knowledge_ingestion_repository as repo
from backend.db.knowledge_repository import get_document
from backend.db.file_repository import get_file
from backend.domain.errors import DomainError, LeaseLost, RecognitionError
from backend.knowledge.evidence import KnowledgeEvidenceStore
from backend.progress.tracker import ProgressReporter
from backend.recognition.models import RecognitionPolicyV1, RecognitionSourceRefV1
from backend.services.recognition_artifacts import RecognitionArtifactBindingV1
from backend.services.recognition_runs import RecognitionRunService
from backend.services.question_sources import recognition_capacity, recognition_engine, _CACHE
from backend.services.stage_provider_routing import resolve_stage_provider_route, stage_provider_configuration_fingerprint
from backend.skills.recognition_reader import PROMPT_VERSION
from backend.storage import get_storage
from backend.tools.pdf_evidence import PdfIndexRequest, read_pdf_evidence
from backend.rag.chunker import MAX_FILE_BYTES

logger = logging.getLogger(__name__)
POLICY = "knowledge-economy-v1"


def frozen_configuration(owner_id, registry, requested_route_id=None):
    route = None
    try:
        route = resolve_stage_provider_route(owner_id=owner_id, registry=registry, requested_route_id=requested_route_id)
    except DomainError:
        if requested_route_id:
            raise
    return dict(policy_version=POLICY, prompt_version=PROMPT_VERSION,
        route_id=route.route_id if route else None,
        route_fingerprint=stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry) if route else None)


def _read_source(job):
    document = get_document(job.document_id, job.owner_id)
    if document is None:
        raise RecognitionError("recognition_source_unavailable")
    stored = get_file(file_id=document.stored_file_id, owner_id=job.owner_id)
    if stored is None or stored.availability_status != "available" or stored.size_bytes > MAX_FILE_BYTES:
        raise RecognitionError("recognition_source_unavailable")
    storage = get_storage()
    if stored.storage_backend != storage.name:
        raise RecognitionError("recognition_source_unavailable")
    with storage.open(stored.storage_key) as stream:
        body = stream.read(MAX_FILE_BYTES + 1)
    if len(body) != stored.size_bytes or hashlib.sha256(body).hexdigest() != document.sha256:
        raise RecognitionError("recognition_source_mismatch")
    return document, body


async def native_units(filename, body):
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "backend.knowledge.native_worker",
        Path(filename).suffix.lower(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(60):
            output, _ = await process.communicate(body)
        if process.returncode or len(output) > 64 * 1024 * 1024:
            raise RecognitionError("recognition_request_invalid")
        return json.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())


def _page_result(run):
    assembly = run.assembly
    document = assembly.document if assembly else None
    evidence = dict(artifact_ids=list(run.artifact_ids), usage=run.operation_usage.model_dump(mode="json"),
                    error_code=run.safe_error_code)
    if document is None:
        return "", "failed", evidence
    warnings = list(document.warning_codes)
    if assembly.raw.read_batch:
        for decision in assembly.raw.read_batch.plan.decisions:
            if "knowledge_native_math_unverified" in decision.reason_codes:
                warnings.append("knowledge_native_math_unverified")
    evidence.update(confidence=document.confidence, warning_codes=sorted(set(warnings)))
    if not document.coverage.complete:
        state = "failed"
        evidence["warning_codes"] = sorted(set(warnings + ["coverage_incomplete"]))
    elif all(page.verified_blank for page in document.pages) and document.pages:
        state = "blank_confirmed"
    elif not document.final_markdown.strip():
        state = "failed"
    else:
        state = "searchable_with_warning" if warnings or document.confidence != "high" or run.safe_error_code else "searchable"
    return document.final_markdown, state, evidence


class KnowledgeIngestionWorker:
    def __init__(self, *, registry_factory=None, page_reader=None):
        if registry_factory is None:
            from backend.services.task_facade import _registry_for_owner
            registry_factory = _registry_for_owner
        self.registry_factory, self.page_reader = registry_factory, page_reader
        self.stopped = False

    def stop(self):
        self.stopped = True

    async def run_once(self, job_id=None):
        job = await run_in_threadpool(repo.claim_next, job_id=job_id)
        if job is None:
            return False
        reporter = ProgressReporter(job.id)
        try:
            document, body = await run_in_threadpool(_read_source, job)
            visual = document.content_type == "application/pdf" or (document.content_type or "").startswith("image/")
            native = None
            if document.content_type == "application/pdf":
                index = await read_pdf_evidence(body, PdfIndexRequest(window_pages=1), timeout_seconds=30, progress=reporter)
                total = index.total_pages
            elif visual:
                total = 1
            else:
                native = await native_units(document.original_name, body)
                total = len(native["units"])
            await run_in_threadpool(repo.initialize, job, total, unit=native["unit"] if native else "page",
                                   warning_codes=native["warning_codes"] if native else ())
            route, registry = None, None
            if visual and job.configuration.get("route_id"):
                registry = await run_in_threadpool(self.registry_factory, job.owner_id)
                route = resolve_stage_provider_route(owner_id=job.owner_id, registry=registry,
                                                     requested_route_id=job.configuration["route_id"])
                fingerprint = stage_provider_configuration_fingerprint(owner_id=job.owner_id, route=route, registry=registry)
                if fingerprint != job.configuration.get("route_fingerprint"):
                    raise RecognitionError("recognition_route_changed")
            source = RecognitionSourceRefV1(owner_id=job.owner_id, scope="knowledge_document", business_id=document.id,
                stored_file_id=document.stored_file_id, input_sha256=document.sha256, original_name=document.original_name,
                content_type=document.content_type) if visual else None
            async with recognition_engine(owner_id=job.owner_id, route=route, registry=registry) as engine:
                extra_left = 2
                for page in await run_in_threadpool(repo.next_pages, job):
                    if self.stopped:
                        break
                    reused = await run_in_threadpool(repo.reusable_page, job, page)
                    if reused is not None:
                        await run_in_threadpool(repo.prepare_page, job, page.id, batch_extra_remaining=0)
                        await run_in_threadpool(repo.finish_page, job, page.id, **reused)
                        continue
                    page = await run_in_threadpool(repo.prepare_page, job, page.id, batch_extra_remaining=extra_left)
                    await reporter.set_current_step("knowledge_page", message=f"Processing page {page.page_number} of {total}")
                    if native:
                        text = native["units"][page.page_number - 1]
                        warnings = native["warning_codes"]
                        # Office images are not evidence of a blank or fully read
                        # page. Native text remains useful but coverage is partial.
                        state = "failed" if warnings else "searchable" if text.strip() else "blank_confirmed"
                        evidence = dict(confidence="medium", warning_codes=warnings, artifact_ids=[],
                                        usage=dict(initial_calls=0, input_tokens=0, output_tokens=0, usage_complete=True))
                    else:
                        repository = repo.KnowledgeRunRepository(job, page.id)
                        policy = RecognitionPolicyV1(version=POLICY, max_detail_pages=1, max_regions=1,
                            max_initial_calls=1, max_calls=1 + int(page.extra_allowed), max_locator_calls=0,
                            max_empty_recoveries=int(page.extra_allowed), max_patches=int(page.extra_allowed),
                            total_seconds=300, read_seconds=280)
                        request = RecognitionReadRequestV1(source=source, purpose="knowledge", scope="pages",
                            pages=[page.page_number], search_start_page=page.page_number, search_window_pages=1, policy=policy)
                        service = RecognitionRunService(store=KnowledgeEvidenceStore(repository, progress=reporter),
                            capacity=recognition_capacity(), cache=_CACHE, progress=reporter,
                            operation_repository=repository, binding_resolver=repository.resolve_binding)
                        run = await (self.page_reader or service.run)(request, body, engine=engine,
                            prompt_version=job.configuration["prompt_version"], authorized_owner_id=job.owner_id,
                            binding=RecognitionArtifactBindingV1(link="knowledge_document", business_id=document.id))
                        text, state, evidence = _page_result(run)
                        extra_left -= min(1, run.operation_usage.patch_calls + run.operation_usage.empty_recovery_calls)
                    await run_in_threadpool(repo.finish_page, job, page.id, text=text, state=state, evidence=evidence)
            await run_in_threadpool(repo.release, job)
        except asyncio.CancelledError:
            # Keep any before-submit marker. Resumption never assumes a cancelled
            # HTTP client cancelled work already accepted by its provider.
            raise
        except (DomainError, ValueError, OSError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, DomainError) else "knowledge_ingestion_failed"
            try:
                await run_in_threadpool(repo.release, job, status="paused", error=code)
            except DomainError:
                pass
        finally:
            from backend.progress.tracker import remove_reporter
            remove_reporter(job.id)
        return True

    async def run_forever(self):
        while not self.stopped:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Knowledge ingestion batch failed; durable lease will expire")
                worked = False
            await asyncio.sleep(.1 if worked else 2)
