"""Captured synthetic model replies must obey the same source/score contracts."""
import json
from pathlib import Path

import pytest

from backend.domain.errors import ValidationError
from backend.services.rubric_drafts import compile_parts_draft, saved_draft
from backend.tests.test_question_material_format_repair import Provider

FIXTURE = json.loads((Path(__file__).parent / "fixtures/part_rubrics/"
                     "gemini-35-policy-diagnostic-20261112.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["case"])
async def test_captured_generation_does_not_relax_teacher_source_checks(case):
    provider = Provider([case["response"], case["response"]])
    if case["status"] == "passed":
        draft = await compile_parts_draft(case["question"], provider)
        assert [s.total_pts for s in draft.plan.part_rubric.parts[0].steps] == [3, 7]
        assert draft.plan.part_rubric.nonblank_floor.resolve(10) == 2
        assert saved_draft({**case["question"], "rubric_draft": draft.model_dump(mode="json")}) is not None
        assert len(provider.calls) == 1
    else:
        # Changed generation guidance must not make an old ambiguous source,
        # invented condition or unauthorized second ledger executable.
        with pytest.raises(ValidationError):
            await compile_parts_draft(case["question"], provider)
        assert len(provider.calls) == 2
        assert saved_draft(case["question"]) is None
