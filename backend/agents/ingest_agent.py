"""
IngestAgent: handles file upload → problem extraction and student answer parsing.

Replaces the business logic in:
  - backend/routers/prob_preview.py  (problem extraction + classification)
  - backend/routers/hw_preview.py    (student answer parsing)

The API routers in backend/api/ingest.py become thin HTTP wrappers over this.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from langchain_core.messages import SystemMessage, HumanMessage
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.models import (
    ProblemSet,
    StudentSubmission,
    ProblemInfo,
    StudentAnswerInfo,
    TestCase,
)
from backend.llm.providers import BaseProvider
from backend.tools.structured_llm import extract_and_parse_json
from backend.tools.file_processing import extract_files_from_archive, decode_text_bytes

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


# ─── Prompt: problem extraction ──────────────────────────────────────────────

PROB_SYSTEM_PROMPT = """You are a professional AI teaching assistant with graduate-level expertise in relevant fields, specializing in analyzing assignment content in plain text format. Your task is:

1. **Problem Segmentation**: Split the identified content into independent problems based on question numbers (e.g., "1.1", "Question 2", "III.", etc.).

2. **Content Extraction**: Extract three key pieces of information for each problem:
    - `number`: The question number.
    - `stem`: The complete question stem content, including all text, formulas, and code blocks.

3. **Problem Classification**: Determine the most appropriate classification (`type`) for each problem. **Use the specific Chinese terms below for the `type` field**:
    - **概念题**: The answer is basically determined or close in meaning to judge correctness.
    - **计算题**: Requires numerical or symbolic calculation to verify accurately.
    - **编程题**: Contains code snippets or requires writing code.
    - **证明题**: Requires logical deduction from known conditions to reach a stated conclusion.
    - **推理题**: Requires logical reasoning to reach a conclusion not provided in the stem.
    - **其他**: Does not fit into the above 5 categories.

    **[Important]: Preserve the stem information completely. Do not delete or translate content.**

4. **Design Grading Criteria (`criterion`)**: If criteria are provided, retain them. If not, design appropriate criteria based on problem type.

5. **Formatted Output**: Return a JSON object with key "problems" containing an array of objects with fields: "q_id", "number", "type", "stem", "criterion".

**[Important]: Output must start with `{` and end with `}`. No preamble, no markdown fences.**
**[Note]: Escape all backslashes in string values as `\\\\`. Critical for LaTeX formulas.**
"""


# ─── Prompt: student answer parsing ──────────────────────────────────────────

HW_SYSTEM_PROMPT = """You are a professional AI teaching assistant. Analyze a single student's submission file and complete:

1. **Identity Recognition**: Look for `stu_id` (student ID) and `stu_name` (name) in the **[Student Submission Content]** first. If not found in the content, try to extract them from the **[Filename]**. If you cannot find them in either place, set `stu_name` to "[Unknown Student]" and `stu_id` to the filename.

2. **Answer Segmentation**: Based on the provided [Question Data], extract each student answer. If a student skipped a question, set "content" to empty string. Preserve content completely — do not delete or translate.

3. **Identify Reliability**: For each question, list any recognition issues in `flag` (empty list if none).

4. **Formatted Output**: Return a JSON object with "stu_id", "stu_name", "stu_ans" (list of {q_id, number, type, content, flag}).

**[Important]: Output must be a single JSON object starting with `{` and ending with `}`. No extra text.**
**[Note]: Escape all backslashes as `\\\\` in string values.**
"""


# ─── Problem extraction ──────────────────────────────────────────────────────

async def extract_problems(
    text: str,
    provider: BaseProvider,
    problem_store: Dict[str, Dict[str, Any]],
    reporter: Optional["ProgressReporter"] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Extract and classify problems from assignment text.
    Stores into problem_store and returns the same dict.

    If `reporter` is provided, emits progress phases:
        extracting → done (or error)
    """
    if not text or not text.strip():
        if reporter:
            await reporter.set_error("Input text is empty.")
        raise ValueError("Input text is empty.")

    if reporter:
        await reporter.set_phase("extracting")

    messages = [SystemMessage(content=PROB_SYSTEM_PROMPT), HumanMessage(content=text)]

    logger.info("extract_problems: calling LLM...")
    if reporter:
        await reporter._emit_message(f"Calling {provider.provider_id}...")
    response = await provider.ainvoke(messages)
    raw_output = response.content
    logger.info(f"extract_problems: LLM returned {len(raw_output)} chars")

    if reporter:
        await reporter._emit_message(f"Parsing JSON ({len(raw_output)} chars)...")

    parsed = extract_and_parse_json(raw_output, ProblemSet)

    if not parsed.problems:
        if reporter:
            await reporter.set_error("LLM did not extract any problems from the text.")
        raise ValueError("LLM did not extract any problems from the text.")

    prob_dict = {q.q_id: q.model_dump() for q in parsed.problems}

    problem_store.clear()
    problem_store.update(prob_dict)
    logger.info(f"extract_problems: stored {len(prob_dict)} problems")

    if reporter:
        await reporter.set_totals(students=0, questions=len(prob_dict))
        await reporter.set_phase("done")

    return prob_dict


# ─── Student answer parsing ──────────────────────────────────────────────────

async def parse_student_answers(
    files_data: List[Dict[str, str]],
    problems_data: Dict[str, Dict[str, str]],
    student_store: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    reporter: Optional["ProgressReporter"] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Parse student submissions using LLM. Each file processed independently in parallel.

    If `reporter` is provided, emits per-file progress (`completed_units` increments).
    """
    if not files_data:
        if reporter:
            await reporter.set_error("No student files to process.")
        raise ValueError("No student files to process.")

    if reporter:
        await reporter.set_phase("parsing")
        await reporter.set_totals(students=len(files_data), questions=len(problems_data))

    # Build simplified problem data for the prompt
    prob_for_prompt = []
    for prob in problems_data.values():
        prob_for_prompt.append({
            "q_id": prob["q_id"],
            "number": prob["number"],
            "type": prob["type"],
            "stem": prob["stem"],
        })
    problems_json_str = json.dumps(prob_for_prompt, ensure_ascii=False, indent=1)

    semaphore = asyncio.Semaphore(20)

    async def process_one(file_info: Dict[str, str]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            filename = file_info.get("filename", "")
            content = file_info.get("content", "")
            if not filename or not content:
                logger.warning(f"Skipping empty file: {filename}")
                if reporter:
                    await reporter.increment_completed()
                return None

            logger.info(f"parse_student_answers: processing {filename}")
            user_msg = (
                f"**[Filename]**: {filename}\n\n"
                f"**[Question Data (JSON)]**:\n{problems_json_str}\n\n"
                f"**[Student Submission Content]**:\n---\n{content}\n---"
            )
            messages = [SystemMessage(content=HW_SYSTEM_PROMPT), HumanMessage(content=user_msg)]

            try:
                response = await provider.ainvoke(messages)
                parsed = extract_and_parse_json(response.content, StudentSubmission)
                logger.info(f"parse_student_answers: done {filename} -> {parsed.stu_name}")
                if reporter:
                    await reporter._emit_message(f"Parsed {filename} → {parsed.stu_name}")
                    await reporter.increment_completed()
                return parsed.model_dump()
            except Exception as e:
                logger.error(f"Failed to parse {filename}: {e}")
                if reporter:
                    await reporter._emit_message(f"Failed: {filename} ({e})", level="warn")
                    await reporter.increment_completed()
                return None

    results = await asyncio.gather(*[process_one(f) for f in files_data])

    stu_dict = {r["stu_id"]: r for r in results if r and r.get("stu_id")}
    student_store.clear()
    student_store.update(stu_dict)
    logger.info(f"parse_student_answers: stored {len(stu_dict)} students")

    if reporter:
        await reporter.set_phase("done")

    return stu_dict


# ─── Reference-answer parsing (auxiliary upload) ────────────────────────────

REFERENCE_SYSTEM_PROMPT = """You are an expert at extracting reference answers from teacher-supplied solution documents.

You will be given (1) a list of known problems with their q_id and stem, and (2) a teacher-supplied document that contains REFERENCE ANSWERS / SOLUTIONS for some or all of those problems.

Your task:
1. For each problem, find the matching reference answer in the document and return it.
2. Output JSON: {"mapping": {"q1": "...", "q2": "...", ...}}
3. If a problem has no matching answer, omit that q_id from the mapping.

**[Critical]: Do NOT reproduce the question stem in the answer text. Output ONLY the answer / solution portion (final result + key derivation steps if present). The same document may also contain the questions — strip them.**

**[Critical]: Output must be a single JSON object starting with `{` and ending with `}`. No preamble, no markdown fences.**
**[Note]: Escape backslashes as `\\\\` in string values for LaTeX safety.**
"""


class ReferenceMap(BaseModel):
    """Output schema for parse_reference_to_per_question."""
    mapping: Dict[str, str] = Field(default_factory=dict)


async def parse_reference_to_per_question(
    text: str,
    problems_data: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    reporter: Optional["ProgressReporter"] = None,
) -> Dict[str, str]:
    """Parse a teacher-supplied reference answer document into a {q_id: answer_text} mapping.

    Used by ``POST /tasks/{id}/upload_reference``. The same document may be the
    original problem file (when the teacher checks "题目文件已包含标答" — we re-feed
    the same bytes here) — the prompt explicitly tells the LLM not to reproduce
    the stem, only the answer portion.

    Caller is responsible for merging the returned dict into
    ``problem_data[q_id]["reference_answer"]``.
    """
    if not text or not text.strip():
        if reporter:
            await reporter.set_error("Reference document is empty.")
        raise ValueError("Reference document is empty.")
    if not problems_data:
        if reporter:
            await reporter.set_error("No problems extracted yet — upload problems first.")
        raise ValueError("No problems extracted yet.")

    if reporter:
        await reporter.set_phase("parsing")

    # Slim problem context for the prompt — only fields the LLM needs to identify each q_id.
    prob_context = [
        {"q_id": p["q_id"], "number": p["number"], "type": p["type"], "stem": p["stem"]}
        for p in problems_data.values()
    ]
    user_msg = (
        f"**[Known Problems (JSON)]**:\n{json.dumps(prob_context, ensure_ascii=False, indent=1)}\n\n"
        f"**[Reference Document]**:\n---\n{text}\n---"
    )
    messages = [
        SystemMessage(content=REFERENCE_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    if reporter:
        await reporter._emit_message(f"Calling {provider.provider_id} for reference parsing...")
    response = await provider.ainvoke(messages)
    raw = response.content or ""
    logger.info(f"parse_reference: LLM returned {len(raw)} chars")

    if reporter:
        await reporter._emit_message(f"Parsing JSON ({len(raw)} chars)...")

    parsed = extract_and_parse_json(raw, ReferenceMap)
    mapping = {qid: txt for qid, txt in parsed.mapping.items() if qid in problems_data and txt.strip()}
    logger.info(f"parse_reference: matched {len(mapping)}/{len(problems_data)} problems")

    if reporter:
        await reporter._emit_message(f"Matched {len(mapping)}/{len(problems_data)} reference answers")
        await reporter.set_phase("done")

    return mapping


# ─── Test-case parsing (auxiliary upload) ───────────────────────────────────

TEST_CASES_SYSTEM_PROMPT = """You are an expert at parsing programming-problem test cases from teacher-supplied documents.

The document may be in any format — JSON, Markdown tables, natural-language descriptions ("input is two integers, expected output is their sum"), code comments, or a mix. Convert it into structured stdin/stdout test cases keyed by q_id.

You will be given (1) a list of known programming problems with q_id, number, stem, and (2) the document.

Your task:
1. For each programming problem (type == "编程题"), extract test cases and return them keyed by q_id.
2. Skip non-programming problems.
3. Output JSON shape:
   {"mapping": {"q3": [{"input": "...", "expected_output": "...", "description": "..."}, ...], ...}}
4. The `input` is what the program reads from stdin (multiple lines OK — use literal "\\n").
5. The `expected_output` is what the program is expected to print to stdout.
6. The `description` is a short label (≤ 60 chars). Optional but helpful.
7. Set `source` to "teacher" for every case (these come from the teacher's document).
8. Set `sandbox_feasible` to true unless the case obviously requires GUI / network / huge dataset.

**[Critical]: Output must be a single JSON object starting with `{` and ending with `}`. No preamble, no markdown fences.**
**[Note]: Escape backslashes as `\\\\` in string values.**
"""


class TestCaseMap(BaseModel):
    """Output schema for parse_test_cases_to_per_question."""
    mapping: Dict[str, List[TestCase]] = Field(default_factory=dict)


async def parse_test_cases_to_per_question(
    text: str,
    problems_data: Dict[str, Dict[str, Any]],
    provider: BaseProvider,
    reporter: Optional["ProgressReporter"] = None,
) -> Dict[str, List[TestCase]]:
    """Parse a teacher-supplied test-case document into {q_id: [TestCase, ...]}.

    Accepts any format (JSON / Markdown / natural language / code comments) —
    the LLM normalizes everything into the canonical TestCase shape from
    backend/models.

    Caller is responsible for merging the returned dict into
    ``problem_data[q_id]["test_cases"]`` (each TestCase must be model_dump()ed
    before storage so it survives JSON round-tripping).
    """
    if not text or not text.strip():
        if reporter:
            await reporter.set_error("Test case document is empty.")
        raise ValueError("Test case document is empty.")
    if not problems_data:
        if reporter:
            await reporter.set_error("No problems extracted yet — upload problems first.")
        raise ValueError("No problems extracted yet.")

    if reporter:
        await reporter.set_phase("parsing")

    # Only feed programming problems in the prompt context — saves tokens and
    # discourages the LLM from inventing cases for non-programming questions.
    prog_problems = [
        {"q_id": p["q_id"], "number": p["number"], "stem": p["stem"]}
        for p in problems_data.values()
        if p.get("type") == "编程题"
    ]
    if not prog_problems:
        if reporter:
            await reporter._emit_message("No programming problems found — nothing to parse.", level="warn")
            await reporter.set_phase("done")
        return {}

    user_msg = (
        f"**[Programming Problems (JSON)]**:\n{json.dumps(prog_problems, ensure_ascii=False, indent=1)}\n\n"
        f"**[Test Case Document]**:\n---\n{text}\n---"
    )
    messages = [
        SystemMessage(content=TEST_CASES_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    if reporter:
        await reporter._emit_message(f"Calling {provider.provider_id} for test case parsing...")
    response = await provider.ainvoke(messages)
    raw = response.content or ""
    logger.info(f"parse_test_cases: LLM returned {len(raw)} chars")

    if reporter:
        await reporter._emit_message(f"Parsing JSON ({len(raw)} chars)...")

    parsed = extract_and_parse_json(raw, TestCaseMap)
    # Filter to known programming q_ids; force source="teacher" regardless of LLM output.
    valid_qids = {p["q_id"] for p in prog_problems}
    mapping: Dict[str, List[TestCase]] = {}
    for qid, cases in parsed.mapping.items():
        if qid not in valid_qids:
            continue
        normalized = [
            TestCase(
                input=tc.input,
                expected_output=tc.expected_output,
                description=tc.description,
                source="teacher",
                sandbox_feasible=tc.sandbox_feasible,
            )
            for tc in cases
        ]
        if normalized:
            mapping[qid] = normalized

    total = sum(len(v) for v in mapping.values())
    logger.info(f"parse_test_cases: matched {len(mapping)} problems with {total} total cases")

    if reporter:
        await reporter._emit_message(
            f"Matched {len(mapping)} programming problems with {total} test cases total"
        )
        await reporter.set_phase("done")

    return mapping
