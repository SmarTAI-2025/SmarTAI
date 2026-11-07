"""Prepare expectations only for newly generated inputs that have no assigned answer."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models import TestCase


class NewTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input: str | None = Field(default=None, max_length=8192)
    args: list[Any] | None = Field(default=None, max_length=100)
    kwargs: dict[str, Any] | None = None

    @model_validator(mode="after")
    def one_interface(self):
        if (self.input is None) == (self.args is None) or (self.input is not None and self.kwargs is not None):
            raise ValueError("Specify literal stdin or a positional argument vector.")
        return self

    def draft(self):
        if self.args is not None:
            return ReferenceInputCase(io_mode="function", function_args=self.args, function_kwargs=self.kwargs, source="llm_generated")
        return ReferenceInputCase(io_mode="stdin", input=self.input, source="llm_generated")


class ReferenceInputCase(TestCase):
    """Internal input-only draft. Never used as a student's execution case."""

    def comparison_probe(self):
        # The ordinary runner captures stdout/return separately from comparison.
        # Supply a placeholder only to the reference, never as an awarded expectation.
        data = self.model_dump(exclude_unset=True)
        data["expected_return" if self.io_mode == "function" else "expected_output"] = "None" if self.io_mode == "function" else ""
        return TestCase.model_validate(data)


def expectations_from_reference(cases, reference_cases, report):
    resolved = []
    for case, reference_case, row in zip(cases, reference_cases, report.results, strict=True):
        if ((row.error and not row.exit_reason) or row.exit_reason not in {"success", "wrong_answer", ""} or not row.executed
                or row.stdout_truncated or row.stderr_truncated):
            raise ValueError("programming_reference_conflict")
        observed = row.return_value if reference_case.function_name else row.stdout
        if observed is None or len(observed.encode("utf-8")) > 8192:
            raise ValueError("programming_cases_unavailable")
        update = {"expected_output": observed}
        if reference_case.function_name:
            update["expected_return"] = observed
        resolved.append(case.model_copy(update=update))
    return resolved


def nearby_legal_integer_inputs(problem, drafts, adapted, domains):
    """Replace only illegal, unfrozen scalar proposals using independently verified neighbors."""
    from backend.services.programming_input_domain import input_domain

    if not all(d.io_mode == "function" and not d.function_kwargs and len(d.function_args or []) == 1
               and type(d.function_args[0]) is int for d in drafts):
        return None
    allowed = {d.function_args[0] for d, domain in zip(drafts, domains, strict=True) if domain == "allowed"}
    if not allowed:
        return None
    # Try around known legal points. Keep the search bounded for huge integers
    # and never infer a range from the reference implementation's output.
    candidates = {n + offset for n in allowed for offset in range(-len(drafts), len(drafts) + 1)} - allowed
    merged = drafts.copy()
    for i, domain in enumerate(domains):
        if domain != "outside":
            continue
        for value in sorted(candidates, key=lambda n: (abs(n - drafts[i].function_args[0]), n)):
            update = {"function_args": [value]}
            if input_domain(problem, adapted[i].model_copy(update=update)) == "allowed":
                merged[i] = drafts[i].model_copy(update=update)
                candidates.remove(value)
                break
        else:
            return None
    return merged
