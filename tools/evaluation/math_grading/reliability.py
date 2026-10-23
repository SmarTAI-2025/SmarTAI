"""Offline raw-JSON acceptance runner; no live-provider mode exists.

Only provider.ainvoke and absent knowledge retrieval are replaced. Wrappers at
plan/compiler and verifier entry points observe and await the original function.
JSON parsing/repair, source validation, scoring, fan-out, and public DTO creation
are production code. A malformed fixture is evidence, not an automatic success.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import AsyncMock, patch

from .candidate import registry_for
from .reliability_cases import BASE, CASES, corpus_hash
from .reliability_v2 import CANDIDATE, wire_v2, feedback_v2


class UnexpectedPrompt(AssertionError):
    pass


def stage_of(messages):
    """Classify the candidate's actual prompt, never infer from call ordinal."""
    humans = [str(m.content) for m in messages if getattr(m, "type", "") == "human"]
    system = "\n".join(str(m.content) for m in messages if getattr(m, "type", "") == "system")
    if not humans:
        raise UnexpectedPrompt("no human prompt")
    initial, latest = humans[0], humans[-1]
    if len(humans) > 1 and "could not be parsed" in latest:
        return ("source_repair" if "preserve_claim_quotes" in latest and "APPLY SCOPED EVIDENCE" not in initial
                else "format_repair"), initial
    if "APPLY SCOPED EVIDENCE" in initial:
        return "tool_feedback", initial
    if "INPUT DATA:" in initial:
        return ("direct" if "Directly grade" in system else "assessment"), initial
    try:
        data = json.loads(initial)
    except ValueError:
        raise UnexpectedPrompt("unrecognized raw-JSON baseline prompt") from None
    if "question" in data:
        return "plan", initial
    if "experts" in data:
        return "expert_review", initial
    raise UnexpectedPrompt("unrecognized prompt object")


class RawProvider:
    """Hand-authored wire responses. Deliberately has no access to the oracle."""
    def __init__(self, wire, index, events):
        self.wire = wire_v2(wire)
        self.index = index
        self.provider_id = f"synthetic:raw-json-{index}"
        self.events = events

    async def ainvoke(self, messages, **kwargs):
        from backend.llm.providers import LLMResponse
        from backend.tools.structured_llm import PermanentLLMError
        try:
            stage, prompt = stage_of(messages)
        except UnexpectedPrompt as exc:
            self.events.append(dict(stage="harness_error", error=str(exc)))
            raise
        event = dict(stage=stage, provider=self.provider_id, sequence=len(self.events))
        self.events.append(event)
        if stage == "plan":
            question = json.loads(prompt)["question"]
            event["question_keys"] = sorted(question)
            payload = self.wire["plan"]
        elif stage == "expert_review":
            payload = self.wire.get("review", {"revisions": []})
        elif stage in {"format_repair", "source_repair"}:
            payload = self.wire.get(stage if stage == "source_repair" else "repair", "{invalid fixture on repair too")
        else:
            data = json.loads(prompt.split("\nINPUT DATA:\n", 1)[1].split("\nAPPLY SCOPED EVIDENCE", 1)[0])
            event["plan_id"] = data["rubric_plan"]["plan_id"]
            if stage == "assessment" and self.wire.get("fault"):
                event["fault"] = self.wire["fault"]
                if self.wire["fault"] == "cancel":
                    raise asyncio.CancelledError("synthetic cancellation")
                raise PermanentLLMError("synthetic provider failure; no upstream request")
            if stage == "direct":
                # Constant useful fallback, NOT taken from expected grade. Its
                # presence fails any oracle requiring detailed/ledger grading.
                payload = dict(score=self.wire.get("direct_score", 4), confidence=.7,
                    rationale="合成直接评分兜底，仅供区分有分与结构能力。", comment="直接评分。")
            else:
                initial = self.wire.get("experts", [self.wire["assessment"]])[self.index]
                payload = deepcopy(self.wire.get("feedback", initial) if stage == "tool_feedback" else initial)
                routes = data["rubric_plan"]["routes"]
                if (isinstance(payload, dict) and len(routes) == 1 and routes[0]["mode"] == "holistic"
                        and self.wire["plan"]["routes"][0]["mode"] != "holistic"):
                    # Honest response to a production-downgraded plan. This
                    # adapter is explicit in evidence and cannot pass mode gates.
                    payload.update(route_id=routes[0]["id"], awards=[], deductions=[], score=4)
                    for c in payload.get("claims", []):
                        c["item_ids"] = []
                    event["fixture_envelope"] = "respond_to_holistic_fallback"
        if stage == "tool_feedback":
            scoped = json.loads(prompt.split("\nAPPLY SCOPED EVIDENCE (one pass):\n", 1)[1])
            payload = feedback_v2(payload, scoped["evidence"])
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        if self.wire.get("fence") and not isinstance(payload, str):
            content = "```json\n" + content + "\n```"
        event["raw_response"] = content
        event["raw_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        return LLMResponse(content=content, provider=self.provider_id, model="synthetic-json",
                           duration_ms=0, input_tokens=0, output_tokens=0)


def no_network(*args, **kwargs):
    raise AssertionError("reliability acceptance forbids network")


async def evaluate(case):
    from backend.agents.grading_agent import grade_batch
    from backend.domain.grading_review import score as effective_score
    from backend.services import math_rubric_plan as rubric
    from backend.skills.math_grading import read_math_trace
    from backend.tools import math_verification

    events, plans, tool_calls = [], [], []
    provider_count = len(case["wire"].get("experts", [None]))
    providers = [RawProvider(case["wire"], i, events) for i in range(provider_count)]
    actual_prepare, actual_verify = rubric.prepare_math_rubric_plan, math_verification.verify_math

    async def observe_plan(*args, **kwargs):
        result = await actual_prepare(*args, **kwargs)
        plans.append(result.model_dump(mode="json"))
        events.append(dict(stage="plan_ready", plan_id=result.plan_id, sequence=len(events)))
        return result

    async def observe_tool(request):
        call = dict(request=request.model_dump(mode="json"))
        tool_calls.append(call)
        started = time.perf_counter()
        try:
            result = await actual_verify(request)
            call["evidence"] = result.model_dump(mode="json")
            return result
        finally:
            call["duration_ms"] = (time.perf_counter() - started) * 1000

    row = dict(id=case["id"], group=case["group"], status="failed", score=None,
               correction=None, traces=[], plans=plans, events=events, tool_calls=tool_calls)
    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", no_network))
        stack.enter_context(patch("socket.create_connection", no_network))
        stack.enter_context(patch("backend.tools.knowledge.retrieve_for_grading", AsyncMock(return_value=[])))
        stack.enter_context(patch.object(rubric, "prepare_math_rubric_plan", observe_plan))
        stack.enter_context(patch.object(math_verification, "verify_math", observe_tool))
        try:
            batch = await grade_batch(problem_store={case["id"]: deepcopy(case["problem"])},
                student_store={"synthetic": dict(stu_id="synthetic", stu_name="Synthetic", stu_ans=[dict(
                    q_id=case["id"], number="1", type=case["problem"]["type"], content=case["answer"])])},
                registry=registry_for(providers), language="zh", multi_sample_n=1,
                aggregation_method="single" if provider_count == 1 else "weighted_average")
            correction = batch[0]["corrections"][0]
            row["correction"] = correction.model_dump(mode="json")
            row["score"] = effective_score(row["correction"])
            row["traces"] = [read_math_trace(e) for e in correction.expert_results]
            row["status"] = "scored" if row["score"] is not None else "failed"
        except asyncio.CancelledError:
            row.update(status="cancelled", error="CancelledError")
        except Exception as exc:
            # All selected cases stay in the denominator. Synthetic-only error
            # type is enough; do not log ambient configuration or stack locals.
            row.update(status="exception", error=type(exc).__name__)
    row["duration_ms"] = (time.perf_counter() - started) * 1000
    if any(e["stage"] == "harness_error" for e in events):
        row["status"] = "harness_error"
    row["contract_failures"] = contract_failures(case, row)
    row["observations"] = observe(case, row)
    return row


def observe(case, row):
    traces = [t for t in row["traces"] if t]
    assessments = [t["assessment"] for t in traces if isinstance(t.get("assessment"), dict)]
    final_claims = [c for a in assessments for c in a.get("claims", [])]
    initial_parsed = [t.get("initial_assessment") or t.get("fallback", {}).get("draft") or t.get("original_assessment")
                      for t in traces]
    initial_claims = [c for a in initial_parsed if isinstance(a, dict) for c in a.get("claims", [])]
    initial_statements = [c for c in initial_claims if c.get("kind", "statement") == "statement"]
    final_statements = [c for c in final_claims if c.get("kind", "statement") == "statement"]
    initial_wire_claims = []
    for e in row["events"]:
        if e["stage"] != "assessment" or "raw_response" not in e:
            continue
        raw = e["raw_response"]
        if raw.startswith("```json\n"):
            raw = raw.removeprefix("```json\n").removesuffix("\n```")
        try:
            initial_wire_claims.extend(json.loads(raw).get("claims", []))
        except (ValueError, AttributeError):
            pass
    stages = Counter(e["stage"] for e in row["events"])
    evidence = [e for t in traces for e in t.get("evidence", [])]
    return dict(
        score_available=row["score"] is not None,
        structured_experts=len(assessments), direct_experts=sum("direct" in t for t in traces),
        plan_modes=[r["mode"] for p in row["plans"] for r in p["routes"]],
        ledger_experts=sum(bool(a.get("awards") or a.get("deductions")) for a in assessments),
        initial_wire_claims=len(initial_wire_claims),
        initial_wire_omissions=sum(c.get("kind") == "omission" for c in initial_wire_claims),
        initial_wire_literal_matches=sum(bool(c.get("quote")) and c["quote"] in case["answer"]
            for c in initial_wire_claims if c.get("kind", "statement") == "statement"),
        first_retained_draft_claims=len(initial_claims),
        first_retained_draft_statement_claims=len(initial_statements),
        first_retained_draft_literal_matches=sum(bool(c.get("quote")) and c["quote"] in case["answer"] for c in initial_statements),
        final_claims=len(final_claims), final_statement_claims=len(final_statements),
        final_literal_source_matches=sum(bool(c.get("quote")) and c["quote"] in case["answer"] for c in final_statements),
        first_retained_draft_available=any(isinstance(a, dict) for a in initial_parsed),
        initial_validation_failures=[f for t in traces for f in t.get("assessment_validation_failures", [])],
        final_omissions=sum(c.get("kind") == "omission" for c in final_claims),
        rejected_checks=[c for t in traces for c in t.get("rejected_checks", [])],
        source_match_scope="literal statement substring diagnostic, not mathematical validity; omission cites original obligation; missing initial draft is not reconstructed from final",
        applicability_decision=next((t.get("tool_applicability") for t in traces if t.get("tool_applicability")), None),
        tool_opportunity_expected=case["oracle"].get("applicable"),
        tools_executed=len(row["tool_calls"]), tool_statuses=dict(Counter(e.get("status") for e in evidence)),
        review_expected=bool(case["oracle"].get("min_review") or case["oracle"].get("conflict_advice")),
        review_provider_entries=stages["tool_feedback"] + stages["expert_review"],
        format_repair_entries=stages["format_repair"],
        review_failure_observed=next((t.get("review_failure") for t in traces if t.get("review_failure")), None),
        feedback_parse_failure_injected=case["oracle"].get("feedback_failed", False),
        provider_entries=sum(e["stage"] not in {"plan_ready", "harness_error"} for e in row["events"]),
        stage_counts=dict(stages), fallback_reasons=[t.get("fallback", {}).get("reason") for t in traces if "direct" in t],
        synthetic_score_expectation=case["oracle"].get("score"), model_reasoning_quality=None,
        upstream_calls=0, billed_cost=0,
    )


def contract_failures(case, row):
    o = case["oracle"]
    failures = []
    def require(condition, message):
        if not condition:
            failures.append(message)
    require(row["status"] != "harness_error", "harness_prompt_not_supported")
    if o.get("available", True):
        require(row["score"] is not None, "useful_score_missing")
    if row["status"] == "exception":
        failures.append("unexpected_execution_exception:" + row.get("error", "unknown"))
    traces = [t for t in row["traces"] if t]
    assessments = [t["assessment"] for t in traces if isinstance(t.get("assessment"), dict)]
    modes = [r["mode"] for p in row["plans"] for r in p["routes"]]
    evidence = [e for t in traces for e in t.get("evidence", [])]
    stages = Counter(e["stage"] for e in row["events"])
    review_count = stages["tool_feedback"] + stages["expert_review"]
    if "available" in o:
        require((row["score"] is not None) == o["available"], "score_availability")
    if "cancelled" in o:
        require((row["status"] == "cancelled") == o["cancelled"], "cancellation_not_propagated")
    if "score" in o:
        require(row["score"] == o["score"], f"score_expected_{o['score']}_observed_{row['score']}")
    if o.get("structured"):
        require(bool(assessments) and len(assessments) == len(traces), "detailed_structure_lost")
    if "mode" in o:
        require(modes == [o["mode"]], "required_ledger_mode_lost")
    if "tool" in o:
        require(any(e.get("status") == o["tool"] for e in evidence), "expected_tool_evidence_missing")
    if "feedback" in o:
        require(stages["tool_feedback"] == o["feedback"], "feedback_count")
    for key, actual in [("tools", len(row["tool_calls"])), ("review", review_count)]:
        if "min_" + key in o:
            require(actual >= o["min_" + key], key + "_required_but_not_executed")
        if "max_" + key in o:
            require(actual <= o["max_" + key], key + "_budget_exceeded")
    if o.get("source_exact"):
        require(bool(assessments) and all(c["quote"] in case["answer"] for a in assessments for c in a["claims"]),
                "source_text_not_preserved")
    if o.get("no_supported_evidence"):
        require(not any(e.get("status") == "supported" for e in evidence), "ungrounded_tool_request_trusted")
    if o.get("reject_bad_quote"):
        require(not any(c["quote"] not in case["answer"] for a in assessments for c in a["claims"]),
                "invented_source_accepted")
    if o.get("reject_bad_mapping"):
        for a in assessments:
            claims = {c["id"]: c for c in a["claims"]}
            require(all(aw["item_id"] in claims[cid]["item_ids"] for aw in a["awards"]
                        if aw["score"] > 0 for cid in aw["claim_ids"]), "award_uses_claim_of_different_item")
    if o.get("reject_wrong_amount"):
        require(not any(r["mode"] == "deductions" and any(i["points"] == 9 for i in r["items"])
                        for p in row["plans"] for r in p["routes"]), "teacher_amount_overridden_by_ai_label")
    if o.get("reject_empty_claims"):
        require(not any(not a.get("claims") for a in assessments), "empty_claims_accepted_as_detailed")
    if o.get("missing_obligation"):
        require(any(a.get("missing_obligations") or a.get("omissions") or
                    any(c.get("kind") == "omission" and not c.get("quote") and c.get("source_quote")
                        for c in a.get("claims", [])) for a in assessments),
                "missing_obligation_not_retained")
    if o.get("source_repair"):
        require(stages["source_repair"] == o["source_repair"], "bounded_source_repair_not_executed")
    if o.get("not_applicable"):
        require(any(t.get("tool_applicability") == "not_applicable" for t in traces),
                "explicit_not_applicable_decision_missing")
    if o.get("feedback_failed"):
        require(stages["tool_feedback"] == 1 and stages["format_repair"] == 1,
                "feedback_failure_path_not_exercised")
    if o.get("conflict_advice"):
        c = row.get("correction") or {}
        visible = " ".join([c.get("comment", ""), *[s.get("desc", "") for s in c.get("steps", [])]])
        specific = any(word in visible.lower() for word in ("核验", "反证", "工具", "conflict", "verification", "tool"))
        require(bool(c.get("review_reasons")) and specific and row["score"] is not None,
                "unresolved_tool_conflict_has_no_specific_nonblocking_advice")
    if row["score"] is not None:
        c = row["correction"]
        require(abs(sum(s["score"] for s in c.get("steps", [])) - row["score"]) < 1e-8, "public_steps_do_not_sum")
    return failures


def summarize(rows):
    """Every selected row is in the main denominator, including cancellation."""
    n = len(rows)
    obs = [r["observations"] for r in rows]
    ratio = lambda count: dict(count=count, total=n, rate=count / n if n else None)
    opportunities = [r for r in rows if r["observations"]["tool_opportunity_expected"] is True]
    return dict(total=n, status_counts=dict(Counter(r["status"] for r in rows)),
        score_available=ratio(sum(o["score_available"] for o in obs)),
        detailed_structure=ratio(sum(o["structured_experts"] > 0 for o in obs)),
        item_ledger=ratio(sum(o["ledger_experts"] > 0 for o in obs)),
        detailed_and_case_contract_pass=ratio(sum(r["observations"]["structured_experts"] > 0
                                                 and not r["contract_failures"] for r in rows)),
        contract_pass=ratio(sum(not r["contract_failures"] for r in rows)),
        tool_opportunities=dict(total=len(opportunities), executed=sum(bool(r["tool_calls"]) for r in opportunities),
            definition="independently authored supported opportunities with initial checks=[]"),
        unknown_tool_evidence=sum(o["tool_statuses"].get("unknown", 0) for o in obs),
        not_applicable_observed=sum(o["applicability_decision"] == "not_applicable" for o in obs),
        applicability_missing=sum(o["applicability_decision"] is None for o in obs),
        review_expected_cases=sum(o["review_expected"] for o in obs),
        review_executed_cases=sum(o["review_provider_entries"] > 0 for o in obs),
        review_provider_entries=sum(o["review_provider_entries"] for o in obs),
        feedback_parse_failures_injected=sum(o["feedback_parse_failure_injected"] for o in obs),
        review_failure_diagnostic_present=sum(o["review_failure_observed"] is not None for o in obs),
        format_repair_entries=sum(o["format_repair_entries"] for o in obs),
        provider_entries=sum(o["provider_entries"] for o in obs),
        direct_fallback_reasons=dict(Counter(reason or "unspecified" for o in obs for reason in o["fallback_reasons"])),
        upstream_calls=0, billed_cost=0, model_accuracy=None,
        synthetic_score_comparisons=sum(o["synthetic_score_expectation"] is not None for o in obs),
        elapsed_case_ms=sum(r["duration_ms"] for r in rows))


async def execute(cases=CASES):
    rows = [await evaluate(case) for case in cases]
    return dict(schema_version=1, kind="synthetic_raw_json_program_path", accuracy_claim=False,
        baseline=BASE, candidate=CANDIDATE, adapter="reliability_v2", revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        corpus_sha256=corpus_hash(), results=rows, summary=summarize(rows),
        groups={g: summarize([r for r in rows if r["group"] == g]) for g in sorted({r["group"] for r in rows})},
        limits=["No real model calls or teacher accuracy labels.",
                "Selected cases are adversarial program probes, not a representative distribution.",
                "Immutable B1 fixtures translated only at the v2 wire boundary; fixed CORE candidate.",
                "Source normalized matcher and lifecycle probes are reported separately."])


def markdown(report):
    s = report["summary"]
    lines = ["# B3 raw JSON independent acceptance", "", f"Production baseline: `{report['baseline']}`; candidate: `{report.get('candidate')}`.",
        f"Corpus SHA256: `{report['corpus_sha256']}`.", "",
        "Synthetic program-path observations only; **not model accuracy**. No upstream calls or paid usage.", "",
        f"All-case denominator {s['total']}; scores {s['score_available']['count']}; detailed {s['detailed_structure']['count']}; "
        f"ledger {s['item_ledger']['count']}; contract passes {s['contract_pass']['count']}.",
        f"Checks-empty opportunities executed {s['tool_opportunities']['executed']}/{s['tool_opportunities']['total']}.", "",
        "| Case | Status / score | Modes | Tools | Review entries | Contract failures |", "|---|---|---|---:|---:|---|"]
    for r in report["results"]:
        o = r["observations"]
        lines.append(f"| {r['id']} | {r['status']} / {r['score']} | {','.join(o['plan_modes'])} | "
                     f"{o['tools_executed']} | {o['review_provider_entries']} | {'; '.join(r['contract_failures']) or 'none'} |")
    probes = report.get("boundary_probes", [])
    if probes:
        lines.extend(["", f"Boundary/version/recovery probes: {sum(r['pass_contract'] for r in probes)}/{len(probes)} pass.",
            "Missing matcher and unchanged-version failures are integration gates, not proof that every negative source is currently accepted.",
            "", "| Probe | Pass | Evidence |", "|---|---|---|"])
        lines.extend(f"| {r['id']} | {r['pass_contract']} | {r['reason']} |" for r in probes)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--assert-contract", action="store_true")
    args = parser.parse_args()
    from .run import isolated_imports
    selected = [c for c in CASES if not args.case or c["id"] in args.case]
    if not selected or set(args.case) - {c["id"] for c in selected}:
        parser.error("unknown or empty case selection")
    with isolated_imports():
        report = asyncio.run(execute(selected))
        from .reliability_probes import execute_probes
        frozen_path = Path(__file__).with_name("reports") / "reliability-baseline-109acaec.json"
        old_plan = (json.loads(frozen_path.read_text())["results"][0]["plans"][0]
                    if frozen_path.exists() else report["results"][0]["plans"][0])
        report["boundary_probes"] = asyncio.run(execute_probes(old_plan))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return int(args.assert_contract and (any(r["contract_failures"] for r in report["results"])
               or any(not r["pass_contract"] for r in report["boundary_probes"])))


if __name__ == "__main__":
    raise SystemExit(main())
