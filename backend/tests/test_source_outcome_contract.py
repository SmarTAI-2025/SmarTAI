from __future__ import annotations

import re
from pathlib import Path

from backend.domain.source_outcomes import SAFE_SOURCE_REASON_CODES


def test_every_public_source_reason_has_explicit_teacher_copy():
    source = (
        Path(__file__).resolve().parents[2]
        / "frontend/app/src/lib/submissionSourceOutcomes.ts"
    ).read_text(encoding="utf-8")
    registry = source.split(
        "const REASON_COPY: Record<string, ReasonFactory> = {",
        1,
    )[1].split("\n};\n\nfunction parsedCopy", 1)[0]
    frontend_codes = set(re.findall(r"^  ([a-z0-9_]+):", registry, re.MULTILINE))

    assert frontend_codes == set(SAFE_SOURCE_REASON_CODES)
