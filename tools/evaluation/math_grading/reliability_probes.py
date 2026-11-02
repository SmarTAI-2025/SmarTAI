"""Independent source-boundary and in-process recovery/version probes.

These call real public/internal boundaries, not copies of production algorithms.
No database or persisted task recovery is exercised; that remains a B2 gate.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from unittest.mock import patch

from .reliability import evaluate, no_network
from .reliability_cases import CASES


SOURCE_CASES = [
    ("S01_exact", "前文x=1后文", "x=1", True, "exact"),
    ("S02_inline_math", r"由\(x^2=1\)得结论", "$x^2=1$", True, "format"),
    ("S03_display_math", r"由\[x^2=1\]得结论", "$x^2=1$", True, "format"),
    ("S04_math_whitespace", r"由\(x ^ 2 = 1\)得结论", "$x^2=1$", True, "format"),
    ("S05_exponent", "x^2=1", "x^3=1", False, None),
    ("S06_quantifier", "存在x使P(x)成立", "任意x使P(x)成立", False, None),
    ("S07_condition", "x为实数", "x>0", False, None),
    ("S08_chain_compression", "A=B=C", "A=C", False, None),
    ("S09_digit_boundary", "1 2", "12", False, None),
    ("S10_word_boundary", "a b", "ab", False, None),
    ("S11_sign", "x=-1", "x=1", False, None),
    ("S12_ambiguous", "x=1; x=1", "x=1", False, None),
    ("S13_context_resolves", "first x=1; second x=1", "second x=1", True, "exact"),
    ("S14_unpaired_delimiter", "value $x=1", r"value \(x=1\)", False, None),
]


def source_probes():
    try:
        matcher = importlib.import_module("backend.services.math_source_evidence").match_source_quote
    except ModuleNotFoundError:
        # Missing implementation must fail positive AND negative probes, never
        # produce false security passes merely because no matcher was called.
        return [dict(id=id, pass_contract=False, reason="source_matcher_missing", observed=None)
                for id, *_ in SOURCE_CASES]
    rows = []
    for id, source, quote, expected, method in SOURCE_CASES:
        try:
            result = matcher(source, quote)
            observed = None if result is None else dict(text=result.text, start=result.start,
                                                        end=result.end, method=result.method)
            passed = (result is not None) == expected
            if result is not None:
                passed = passed and result.text == source[result.start:result.end] and result.method == method
            rows.append(dict(id=id, pass_contract=passed, reason="source_location_contract", observed=observed))
        except Exception as exc:
            rows.append(dict(id=id, pass_contract=False, reason=type(exc).__name__, observed=None))
    return rows


async def parser_probes():
    from pydantic import BaseModel
    from backend.llm.providers import LLMResponse
    from backend.tools.structured_llm import structured_llm_call

    class SourceFields(BaseModel):
        quote: str
        context_quote: str
        source_quote: str
        description: str

    rows = []
    for id, text in [("J01_inline_source", r"由\(x^2=1\)得解"),
                     ("J02_display_source", r"原式\[x=1\]"),
                     ("J03_source_whitespace", "  x = 1\n  y = 2  ")]:
        raw = dict(quote=text, context_quote=text, source_quote=text, description=text)
        calls = []
        class Provider:
            provider_id = "synthetic:source-fields"
            async def ainvoke(self, messages, **kwargs):
                calls.append("raw_json")
                return LLMResponse(content=json.dumps(raw, ensure_ascii=False), provider=self.provider_id,
                                   model="synthetic", duration_ms=0)
        with patch("socket.socket.connect", no_network), patch("socket.create_connection", no_network):
            parsed, _ = await structured_llm_call(Provider(), output_model=SourceFields,
                system_prompt="Preserve source fields verbatim; description is display text.", user_prompt="Synthetic boundary probe.")
        observed = parsed.model_dump()
        rows.append(dict(id=id, pass_contract=all(observed[k] == text for k in ("quote", "context_quote", "source_quote")),
            reason="source_fields_must_bypass_display_formatter", raw=raw, observed=observed, provider_entries=len(calls)))
    return rows


def version_probes(old_plan):
    from backend.services import math_grading_context as context
    from backend.services import math_rubric_plan as rubric
    from backend.skills import math_grading
    rows = [dict(id=id, pass_contract=current != old, reason="old_version_must_be_retired", observed=current)
            for id, current, old in [
                ("V01_engine", context.MATH_GRADING_ENGINE_VERSION, "math_evidence_v1"),
                ("V02_schema", rubric.SCHEMA_VERSION, "math_rubric_v1"),
                ("V03_interpretation", rubric.INTERPRETATION_VERSION, "math_rubric_interpretation_v2")]]
    restored = context.plans_from_snapshots(dict(math_grading_engine_version="math_evidence_v1",
        math_rubric_plans={"legacy": old_plan}), [])
    rows.append(dict(id="V04_legacy_snapshot", pass_contract=not restored,
                     reason="old_snapshot_must_not_restore", observed=list(restored)))
    from backend.models import ExpertResult
    old_expert = ExpertResult(provider="synthetic:old", score=1, max_score=10, confidence=.9,
        comment="old", raw_output=json.dumps(dict(version="math_evidence_v1", plan_id=old_plan["plan_id"])))
    rows.append(dict(id="V05_legacy_trace", pass_contract=math_grading.read_math_trace(old_expert) is None,
                     reason="old_expert_trace_must_not_enter_new_engine"))
    return rows


async def recovery_probes():
    from backend.models import ProblemInfo
    from backend.services.math_grading_context import get_math_plan, expert_matches_plan, plans_from_snapshots
    cancelled_case = next(c for c in CASES if c["id"] == "R31_cancelled")
    cancelled = await evaluate(cancelled_case)
    leaked = get_math_plan(ProblemInfo(**cancelled_case["problem"]))
    retry_case = deepcopy(cancelled_case)
    retry_case["wire"].pop("fault")
    retry_case["oracle"] = dict(score=10, structured=True, mode="points")
    retried = await evaluate(retry_case)
    plan = retried["plans"][0]
    # Deliberately wrong engine, distinct from the v1 retirement gates above.
    incompatible = plans_from_snapshots(dict(math_grading_engine_version="obsolete-synthetic",
        math_rubric_plans={retry_case["id"]: plan}), [])
    checkpoint_accepted = expert_matches_plan(dict(q_id=retry_case["id"],
        math_grading_engine_version="obsolete-synthetic", math_plan_id=plan["plan_id"]), {retry_case["id"]}, {})
    return [
        dict(id="L01_cancel_context", pass_contract=cancelled["status"] == "cancelled" and leaked is None,
             reason="cancelled_batch_context_reset"),
        dict(id="L02_same_question_retry", pass_contract=not retried["contract_failures"] and
             retried["observations"]["stage_counts"].get("plan") == 1, reason="new_batch_recompiles_after_cancel"),
        dict(id="L03_wrong_engine_snapshot", pass_contract=not incompatible, reason="incompatible_engine_rejected"),
        dict(id="L04_wrong_engine_expert", pass_contract=not checkpoint_accepted, reason="incompatible_expert_rejected"),
    ]


async def execute_probes(old_plan):
    return [*await parser_probes(), *source_probes(), *version_probes(old_plan), *await recovery_probes()]
