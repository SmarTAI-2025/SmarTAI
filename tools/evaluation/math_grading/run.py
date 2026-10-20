"""CLI for frozen replay, math verification and opt-in live skill evaluation."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from .corpus import CASES, corpus_hash
from .metrics import compare, summarize


def revision():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def isolated_imports():
    """Never load repository .env, persisted users, provider registry or real DB."""
    temporary = tempfile.TemporaryDirectory(prefix="smartai-math-eval-")
    os.environ.update(
        SMARTAI_RUNTIME_ENVIRONMENT="test",
        SMARTAI_DATABASE_URL=f"sqlite:///{temporary.name}/test.db",
        SMARTAI_STORAGE_ROOT=f"{temporary.name}/uploads",
        SMARTAI_PROVIDER_ENCRYPTION_KEY="evaluation-only-fake-master-key-0123456789",
        SMARTAI_JWT_SECRET="evaluation-only-fake-jwt-secret-0123456789",
    )
    previous = os.getcwd()
    try:
        os.chdir(temporary.name)
        # Force settings initialization in the empty temp directory.
        import backend.config  # noqa: F401
    finally:
        os.chdir(previous)
    return temporary


class CallBudgetExceeded(RuntimeError):
    pass


class LimitedProvider:
    """Cap provider entries and outputs; SDK transport retries are not dollar caps."""
    def __init__(self, provider, calls, tokens):
        self.provider = provider
        self.limit, self.tokens, self.calls = calls, tokens, 0
        self.responses = []
        self.requests = []

    def __getattr__(self, key):
        return getattr(self.provider, key)

    async def ainvoke(self, messages, *, max_output_tokens=None):
        if self.calls >= self.limit:
            raise CallBudgetExceeded("evaluation provider-entry limit reached")
        self.calls += 1
        self.requests.append([{"type": getattr(m, "type", type(m).__name__),
                               "content": m.content} for m in messages])
        result = await self.provider.ainvoke(
            messages, max_output_tokens=min(max_output_tokens or self.tokens, self.tokens))
        self.responses.append({"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                               "duration_ms": result.duration_ms})
        return result

    async def ainvoke_vision(self, *args, **kwargs):
        raise RuntimeError("text-only synthetic evaluation does not enable vision")


async def live_case(case, provider):
    from .replay import arguments, observation, skill_for
    from unittest.mock import AsyncMock, patch
    problem, answer = arguments(case)
    start, calls = time.perf_counter(), provider.calls
    # This benchmark has no knowledge documents. Do not consult ambient task KB.
    with patch("backend.tools.knowledge.retrieve_for_grading", AsyncMock(return_value=[])):
        expert = await skill_for(problem.type)(provider, language="zh").grade(
            problem, answer, student_id="synthetic")
    result = observation(case, expert, (time.perf_counter() - start) * 1000)
    result.update(provider_entry_calls=provider.calls - calls, fixed_response_calls=0)
    return result


async def execute(args):
    from .replay import BASE_SHA, legacy_case, opposite_experts_probe
    sha = revision()
    if args.mode in {"legacy-replay", "live"} and not args.adapter and sha != BASE_SHA:
        # Our evaluation-only commit changes HEAD. Verify the actual production tree.
        diff = subprocess.check_output(["git", "diff", BASE_SHA, "--", "backend",
                                        ":!backend/tests/math_grading_evaluation"], text=True)
        if diff:
            raise ValueError("legacy adapter requires unchanged base production code; use a candidate adapter")
    report = {
        "schema_version": 1, "mode": args.mode, "revision": sha, "base_revision": BASE_SHA,
        "corpus_sha256": corpus_hash(), "label_kind": "synthetic_explicit_rule",
        "accuracy_claim": False, "scope": "single skill; no API/run/persistence/frontend execution",
        "results": [], "candidate_acceptance": "not_executed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {"python": sys.version, "sympy": version("sympy"), "pydantic": version("pydantic")},
    }
    if args.mode == "verify":
        from backend.tools.math_verification import MathVerificationRequest, verify_math
        rows = []
        for case in CASES:
            specification = case.get("verification")
            if not specification:
                continue
            evidence = await verify_math(MathVerificationRequest(**specification["request"]))
            rows.append({"id": case["id"], "request": specification["request"],
                         "evidence": evidence.model_dump(mode="json"),
                         "allowed_statuses": specification["allowed_statuses"],
                         "passed": evidence.status in specification["allowed_statuses"]})
        report["results"] = rows
        report["scope"] = "math verification only; status never awards points"
        write(args.output, report)
        return 0 if all(r["passed"] for r in rows) else 1

    provider = None
    selected = CASES
    if args.mode == "live":
        # Guard checked before constructing any real provider.
        if not args.enable_live or args.acknowledge_cost != "I_ACCEPT_PROVIDER_CHARGES":
            raise ValueError("live requires --enable-live and explicit --acknowledge-cost")
        if not args.provider or not args.model or not args.case_ids:
            raise ValueError("live requires explicit --provider, --model and --case-ids")
        key = os.environ.get("MATH_GRADING_EVAL_API_KEY")
        if not key:
            raise ValueError("set MATH_GRADING_EVAL_API_KEY explicitly; ambient keys are never selected")
        ids = args.case_ids.split(",")
        selected = [c for c in CASES if c["id"] in ids]
        if len(selected) != len(ids) or len(selected) > args.max_cases:
            raise ValueError("unknown/duplicate case IDs or max-cases exceeded")
        if any(c["replay"]["tool_fault"] or c["replay"]["model_fault"] for c in selected):
            raise ValueError("fault injection cases are replay-only")
        from backend.models import ProviderConfig
        from backend.llm.providers import build_provider
        provider = LimitedProvider(build_provider(ProviderConfig(
            provider_type=args.provider, model=args.model, api_key=key)),
            args.max_provider_calls, args.max_output_tokens)
        report.update(provider=args.provider, model=args.model,
                      cost_confirmation="explicit_cli_acknowledgment",
                      max_provider_calls=args.max_provider_calls, max_output_tokens=args.max_output_tokens)
    adapter = None
    if args.adapter:
        if args.mode not in {"candidate-replay", "live"}:
            raise ValueError("adapter is only supported for candidate-replay/live")
        module, name = args.adapter.split(":", 1)
        adapter = getattr(importlib.import_module(module), name)
        report["adapter"] = args.adapter
    if args.mode == "candidate-replay" and adapter is None:
        raise ValueError("4B requires the coordinator's exact candidate adapter; no mock success fallback")
    for case in selected:
        if provider and provider.calls >= provider.limit:
            report["stopped_reason"] = "provider_entry_budget"
            break
        with ExitStack() as stack:
            if args.mode == "candidate-replay":
                def no_network(*_args, **_kwargs):
                    raise AssertionError("candidate replay forbids network")
                stack.enter_context(patch("socket.socket.connect", no_network))
                stack.enter_context(patch("socket.create_connection", no_network))
            result = (await adapter(case, provider) if adapter
                      else await live_case(case, provider) if provider else await legacy_case(case))
        report["results"].append(result)
        # Preserve completed cases if a later request fails/cancels.
        write(args.output, report)
    completed = [c for c in selected if c["id"] in {r["id"] for r in report["results"]}]
    report["metrics"] = summarize(completed, report["results"])
    report["completed_case_count"] = len(completed)
    report["planned_case_count"] = len(selected)
    report["prompt_versions"] = {
        name: __import__("hashlib").sha256(Path("backend/prompts", name).read_bytes()).hexdigest()
        for name in ("calc.txt", "proof.txt", "concept.txt")}
    if args.mode == "legacy-replay":
        report["opposite_experts_probe"] = opposite_experts_probe()
    if provider:
        report["usage"] = provider.responses
        report["requests"] = provider.requests
    write(args.output, report)
    return 0  # This is a measurement artifact, not an assertion that scores pass.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["legacy-replay", "candidate-replay", "verify", "live", "compare"],
                        default="legacy-replay")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter", help="trusted local module:async_function(case, provider) -> observation")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--acknowledge-cost")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--case-ids")
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--max-provider-calls", type=int, default=6)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    args = parser.parse_args()
    try:
        if any(getattr(args, field) < 1 for field in
               ("max_cases", "max_provider_calls", "max_output_tokens")):
            raise ValueError("limits must be positive")
        if args.mode == "compare":
            if not args.before or not args.after:
                raise ValueError("compare needs --before and --after")
            write(args.output, compare(json.loads(args.before.read_text()),
                                       json.loads(args.after.read_text())))
            return 0
        with isolated_imports():
            return asyncio.run(execute(args))
    except (ValueError, ModuleNotFoundError) as exc:
        print(f"Evaluation not executed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
