"""Offline 31 x 7 x 2 replay; no network, production data, or provider spend.

Run this same script against --source BASE and --source CANDIDATE in fresh
processes. Linux pressure mode models a 1906 MiB host with a fixed other-process
footprint, measuring this process's real RSS. Provider replies/latency are fixed;
this is memory/throughput evidence, not a real-provider latency or quality test.
"""
import argparse
import asyncio
from collections import Counter
import ctypes
import gc
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace

import psutil

MIB = 1024 ** 2
PROCESS = psutil.Process()


def rss():
    return PROCESS.memory_info().rss


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fragmented_heap():
    """Touch then free 128 MiB in four native arenas, pinning only their tails."""
    libc = ctypes.CDLL(None)
    libc.malloc.argtypes, libc.malloc.restype = [ctypes.c_size_t], ctypes.c_void_p
    libc.free.argtypes = [ctypes.c_void_p]
    barrier = threading.Barrier(4)
    anchors = []

    def allocate():
        blocks = [libc.malloc(64 * 1024) for _ in range(512)]
        assert all(blocks)
        for block in blocks:
            ctypes.memset(block, 1, 64 * 1024)
        anchors.append(libc.malloc(4096))
        barrier.wait()
        for block in blocks:
            libc.free(block)

    threads = [threading.Thread(target=allocate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return libc, anchors


def allocator_benchmark():
    from backend.llm.memory_reclaim import reclaim_idle_memory
    gc.collect()
    before = rss()
    libc, anchors = fragmented_heap()
    held = rss()
    started = time.perf_counter()
    supported = reclaim_idle_memory()
    seconds = time.perf_counter() - started
    after = rss()
    for anchor in anchors:
        libc.free(anchor)
    return dict(mode="allocator", supported=supported, before_mib=before/MIB,
        held_mib=held/MIB, after_mib=after/MIB, released_mib=(held-after)/MIB,
        pause_seconds=seconds)


def expired_cache():
    from backend.recognition.local_cache import RecognitionByteCache
    from backend.tests.test_recognition_local_cache import context
    now = [100.0]
    cache = RecognitionByteCache(clock=lambda: now[0])
    for i in range(64):
        cache.put(**context(i+1), value=bytes(bytearray(MIB)))
    now[0] += 1801
    return cache


def progress_benchmark():
    """Actual SQLite reads/projection, at the observed event scale, five polls."""
    from sqlalchemy import update
    from backend.db.base import Base
    from backend.db.session import configure_database, session_scope
    from backend.db.models import GradingRunEventRecord, GradingRunRecord
    from backend.db.workflow_repository import GradingRunSetupRecord
    from backend.models import ExpertResult, TaskGradingSetup
    from backend.services import grading_runs, task_facade
    from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case
    from backend.tests.test_task_grading_retry import Registry, start
    Base.metadata.create_all(configure_database(os.environ["SMARTAI_DATABASE_URL"]))
    seeded = _seed_legacy_structured_case(question_count=7)
    grading_runs._registry_for = lambda _owner: Registry()
    grading_runs.settings.e2e_fake_provider = False
    run_id = start((seeded["assignment_id"], seeded["owner_id"]), "offline-progress")["job_id"]
    config = TaskGradingSetup(selected_provider_ids=["p0", "p1"], primary_provider_id="p0")
    events = []
    def add(message, payload):
        n = len(events)+1
        events.append(dict(id=f"offline-event-{n}", grading_run_id=run_id, sequence=100+n,
            created_at=1000+n, level="info", message=message, payload=payload))
    models = [dict(id=f"p{i}", label=f"Model {i}", total=217, succeeded=217, failed=0,
                   completion_times=[1000+j*10 for j in range(8)]) for i in range(2)]
    for i in range(3560):
        add("grading_progress", dict(completed_units=0, model_waits=[], model_progress=models))
    for i in range(434):
        expert = ExpertResult(provider=f"p{i%2}", score=10, max_score=10, confidence=1,
            comment="Offline explanation. "*10, raw_output="Synthetic recorded output. "*400)
        add("grading_expert_checkpoint", dict(student_id=f"s{i//14}", q_id=f"q{i//2%7+1}",
            provider_id=f"p{i%2}", sample_index=0, result=expert.model_dump()))
    for i in range(217):
        add("grading_math_final_trace", {"evidence": "Synthetic final trace. "*800})
    with session_scope() as session:
        session.execute(update(GradingRunRecord).where(GradingRunRecord.id == run_id).values(
            total_submissions=31, status="running"))
        session.execute(update(GradingRunSetupRecord).where(GradingRunSetupRecord.grading_run_id == run_id).values(
            setup=config.model_dump(), input_manifest={}))
        session.execute(GradingRunEventRecord.__table__.insert(), events)
    del events
    gc.collect()
    tracemalloc.start()
    measurements = []
    for _ in range(5):
        tracemalloc.reset_peak()
        started = time.perf_counter()
        progress = task_facade._grading_progress(run_id, seeded["owner_id"])
        assert progress["saved_units"] == progress["fully_saved_units"] == 217
        measurements.append(dict(seconds=time.perf_counter()-started, peak_mib=tracemalloc.get_traced_memory()[1]/MIB))
        del progress
        gc.collect()
    tracemalloc.stop()
    return dict(mode="progress", progress_events=3560, expert_events=434, final_traces=217, polls=measurements)


async def grade_benchmark(pressure):
    from backend.agents.grading_agent import grade_batch
    from backend.config import settings
    from backend.llm.concurrency import ModelScheduler
    from backend.llm.providers import BaseProvider, LLMResponse
    from backend.models import ProviderConfig, TaskGradingSetup
    from backend.progress.tracker import ProgressReporter
    from backend.services.math_grading_context import math_plans_context
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric, retarget_draft
    from backend.skills import math_grading
    from backend.tests.test_automatic_model_scheduling import FakeHost
    from backend.tests.test_frozen_part_rubric import question_row
    from backend.tests.test_proof_rubric import rubric, STEM, REFERENCE, POINTS

    async def empty(*args, **kwargs):
        return []
    math_grading.kb_tool.retrieve_for_grading = empty
    settings.max_concurrent_llm_per_endpoint = 50
    settings.max_concurrent_llm_per_provider = 50
    settings.llm_timeout = 30
    response = json.dumps(dict(comment="Valid compressed proof", confidence=1, part_decisions=[dict(
        part_id="p1", target_quote="part:p1", student_quote="student:1", method="minor_variant",
        relevance="complete", progress="The compact proof establishes all obligations.",
        inferences=[dict(evidence="student:1", reasoning=REFERENCE, result="written",
            step_ids=[key for key, _, _ in POINTS])])]))
    prompt_hashes = Counter()
    prepared = peak_prepared = active = peak_active = 0
    calls = 0
    # Real provider admission is retained. Only the network response is replaced.
    class ReplayProvider(BaseProvider):
        provider_type = "openai"

        def _build_client_sync(self):
            raise AssertionError("The benchmark must never create a network client")

        async def ainvoke(self, messages, **kwargs):
            nonlocal prepared, peak_prepared, active, peak_active, calls
            prepared += 1
            peak_prepared = max(peak_prepared, prepared)
            prompt_hashes[digest([m.content for m in messages])] += 1
            try:
                async with self._call_capacity(messages):
                    calls += 1
                    assert calls <= 434, "Unexpected extra generation/review/retry request"
                    active += 1
                    peak_active = max(peak_active, active)
                    try:
                        await asyncio.sleep(.15)
                    finally:
                        active -= 1
                    return LLMResponse(response, self.provider_id, self.model, 150)
            finally:
                prepared -= 1

    providers = [ReplayProvider(ProviderConfig(provider_type="openai", api_key="offline-only",
        model=f"replay-{i}", base_url="https://example.invalid/v1")) for i in range(2)]
    by_id = {p.provider_id: p for p in providers}
    registry = SimpleNamespace(list_available=lambda: providers, get=by_id.get)
    setup = TaskGradingSetup(selected_provider_ids=list(by_id), primary_provider_id=providers[0].provider_id,
        aggregation_method="weighted_average")
    problems, plans = {}, {}
    for i in range(7):
        raw = rubric().model_dump(mode="json")
        raw.update(id=f"q{i+1}", label=str(i+1))
        row = question_row(raw)
        row.update(q_id=raw["id"], type="推理题", reference_answer=REFERENCE,
            ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})
        row["criterion"], draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
        row["_math_rubric_source"] = {"presentation": {key: row[key]
            for key in ("question_structure", "ai_completion_provenance")}}
        plans[row["q_id"]] = retarget_draft(draft, row).plan
        problems[row["q_id"]] = row
    students = {str(i): dict(stu_id=f"student-{i}", stu_name=f"Synthetic {i}", stu_ans=[
        dict(q_id=qid, number=row["number"], type=row["type"], content=REFERENCE)
        for qid, row in problems.items()]) for i in range(31)}
    gc.collect()
    base_rss = rss()
    cache = expired_cache() if pressure else None
    native = fragmented_heap() if pressure and sys.platform.startswith("linux") else None
    seeded_rss = rss()
    # Simulate one periodic cleanup tick after the existing absolute TTL.
    # The separate lifecycle test verifies the real 30-second timer.
    if cache is not None and hasattr(cache, "expire"):
        cache.expire()
    host = FakeHost(capacity=10)
    scheduler = ModelScheduler(host_capacity=host, pressure_clear=lambda: True,
        memory_reader=(lambda: (max(0, 740*MIB-(rss()-base_rss)), 1906*MIB)) if pressure else lambda: None)
    asyncio.get_running_loop()._smartai_model_scheduler = scheduler
    peak_rss = seeded_rss
    lags = []
    done = False

    async def monitor():
        nonlocal peak_rss
        while not done:
            target = time.perf_counter() + .01
            await asyncio.sleep(.01)
            lags.append(max(0, time.perf_counter()-target))
            peak_rss = max(peak_rss, rss())

    sampler = asyncio.create_task(monitor())
    reporter = ProgressReporter("offline-memory-replay", 31, 7)
    started = time.perf_counter()
    try:
        with math_plans_context(plans):
            results = await asyncio.wait_for(grade_batch(student_store=students, problem_store=problems,
                registry=registry, grading_setup=setup, aggregation_method="weighted_average",
                reporter=reporter), 180)
    finally:
        done = True
        await sampler
    elapsed = time.perf_counter()-started
    corrections = [c for result in results for c in result["corrections"]]
    assert len(corrections) == 217 and calls == 434
    assert all(c.score == 10 and len(c.expert_results) == 2 and
               all(not e.error_kind for e in c.expert_results) for c in corrections)
    scored = [dict(q_id=c.q_id, score=c.score, steps=[s.model_dump(mode="json") for s in c.steps],
        experts=[dict(score=e.score, steps=[s.model_dump(mode="json") for s in e.steps],
                      raw_output=e.raw_output) for e in c.expert_results]) for c in corrections]
    gc.collect()
    result = dict(mode="pressure" if pressure else "normal", students=31, questions=7, experts=2,
        calls=calls, corrections=len(corrections), seconds=elapsed, base_rss_mib=base_rss/MIB,
        seeded_rss_mib=seeded_rss/MIB, peak_rss_mib=peak_rss/MIB, end_rss_mib=rss()/MIB,
        peak_prepared=peak_prepared, peak_active=peak_active, max_loop_lag_seconds=max(lags, default=0),
        expired_cache_mib=cache._size_bytes/MIB if cache else 0,
        prompts_digest=digest(sorted(prompt_hashes.items())), scores_digest=digest(scored),
        plans_digest=digest({key: plan.model_dump(mode="json") for key, plan in plans.items()}))
    if native:
        libc, anchors = native
        for anchor in anchors:
            libc.free(anchor)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "pressure", "allocator", "progress"), default="normal")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    with tempfile.TemporaryDirectory(prefix="smartai-memory-replay-") as directory:
        os.chdir(directory)  # Never load a checkout's private .env or runtime files.
        os.environ.update(SMARTAI_RUNTIME_ENVIRONMENT="test", SMARTAI_DATABASE_URL=f"sqlite:///{directory}/test.db",
            SMARTAI_DATABASE_AUTO_CREATE="true", SMARTAI_SEED_TEST_USERS="false",
            SMARTAI_STORAGE_ROOT=directory, SMARTAI_JWT_SECRET="offline-replay-signing-key-0123456789abcdef",
            SMARTAI_PROVIDER_ENCRYPTION_KEY="offline-replay-provider-key-0123456789abcdef")
        logging.disable(logging.CRITICAL)
        if args.mode == "allocator":
            result = allocator_benchmark()
        elif args.mode == "progress":
            result = progress_benchmark()
        else:
            result = asyncio.run(grade_benchmark(args.mode == "pressure"))
        result.update(platform=sys.platform, python=sys.version.split()[0])
    args.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
