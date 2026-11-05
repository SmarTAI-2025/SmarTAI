"""Full-source imports through durable, authenticated and manual recovery paths."""
import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from backend.db import assignment_repository, course_repository, workflow_repository
from backend.services import submissions, task_facade
from backend.services.programming_submission_mapping import PYTHON_FLAG, source_answers
from backend.services.submission_manual_review import continue_manually
from backend.services.task_execution import stop_task_run
from backend.tests.test_ocr_normalized_ingest import _seed_user
from backend.tests.test_ocr_mounted_routes import client_registry
from backend.tests.test_submission_incremental import _claim, _registry
from backend.tests.test_task_background_workflows import _seed_task


SUM = "Implement def sum_positive(nums): return the sum of positive entries."
COUNT = "Implement def count_positive(nums): count all positive entries."
SOURCE = ("import operator\r\nBOUND = 0\r\n"
          "def count_positive(nums):\r\n    return sum(ok(x) for x in nums)\r\n"
          "def ok(x):\r\n    return operator.gt(x, BOUND)\r\n"
          "def sum_positive(nums):\r\n    return sum(x for x in nums if ok(x))\r\n")


def questions(*stems):
    return {f"q{i}": dict(q_id=f"q{i}", number=str(i), type="programming", stem=stem)
            for i, stem in enumerate(stems, 2)}


def seed(*stems):
    owner, task = _seed_task()
    for i, problem in enumerate(questions(*stems).values()):
        assignment_repository.add_question(task, teacher_id=owner, order_index=i, **problem,
                                           criterion="Award 10 points for satisfying the question.", max_score=10)
    return owner, task


@pytest.mark.parametrize("source,pending", [
    (SOURCE, set()),
    (SOURCE.replace("def sum_positive", "# 第3题\ndef sum_positive"), {"q2", "q3"}),
    ("def first(nums): return 42\ndef second(nums): return 0\n", {"q2", "q3"}),
    ("def sum_positive(nums): return 42\n", {"q3"}),
])
def test_per_question_conflicts_and_missing_functions_retain_original(source, pending):
    known = questions(SUM, COUNT)
    rows = source_answers("source.py", source, known, set(known))
    assert all(row["content"] == source for row in rows)
    assert {row["q_id"] for row in rows if "recognition_manual_pending" in row["flag"]} == pending
    for qid in known:
        assert source_answers("source.py", source, known, {qid}) == [row for row in rows if row["q_id"] == qid]


@pytest.mark.parametrize("code", ["def combine(a,b): return a+b\n", "def add(pair): return sum(pair)\n",
                                  "def add(a,b) return a+b\n"])
def test_single_explicit_signature_violation_remains_student_source(code):
    known = questions("Implement def add(a,b): return their sum.")
    answer, = source_answers("source.py", code, known, set(known))
    assert answer["content"] == code and answer["flag"] == [PYTHON_FLAG]


def test_teacher_cases_and_duplicate_contracts_checked_assignment_wide():
    known = questions("Task A", "Task B", "Task C")
    for qid, name in [("q2", "add"), ("q3", "add"), ("q4", "count")]:
        known[qid]["test_cases"] = [{"source": "teacher", "function_name": name}]
    code = "def add(a): return a\ndef count(a): return 0\n"
    assert [row["flag"] for row in source_answers("x.py", code, known, set(known))] == [
        ["recognition_manual_pending", PYTHON_FLAG], ["recognition_manual_pending", PYTHON_FLAG], [PYTHON_FLAG]]


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_original_module_reaches_every_answer_even_if_stopped_before_worker(monkeypatch, manual):
    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, AsyncMock(side_effect=AssertionError("Native input called a provider")))
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1_Alice.txt",
        content=SOURCE.encode(), content_type="text/plain", registry=registry)
    if manual:
        version = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
        stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=version)
        version = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
        await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=version)
    else:
        await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved = task_facade.get_task(task_id=task, owner_id=owner)
    answers = saved["student_data"]["S1"]["stu_ans"]
    assert {a["q_id"]: a["content"] for a in answers} == {"q2": SOURCE, "q3": SOURCE}
    assert not any("recognition_manual_pending" in a["flag"] for a in answers)
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("suffix", ["txt", "py"])
async def test_authenticated_revision_contains_exact_single_module(monkeypatch, archive, suffix):
    owner, task = seed(SUM, COUNT)
    student = "student-" + owner
    _seed_user(student, "student")
    course = assignment_repository.get_assignment(task, actor_id=owner).course_id
    course_repository.enroll(course, student_id=student, actor_id=owner)
    assignment_repository.publish(task, teacher_id=owner, expected_version=1)
    registry = _registry(monkeypatch, AsyncMock(side_effect=AssertionError("Native input called a provider")))
    filename, content = "source." + suffix, SOURCE.encode()
    if archive:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as out:
            out.writestr(filename, content)
        filename, content = "upload.zip", data.getvalue()
    revision = await submissions.submit_student_file_with_ocr(student_id=student, assignment_id=task,
        filename=filename, content=content, content_type=None, provider=registry.provider, registry=registry)
    assert {a.q_id: a.content for a in revision.answers} == {"q2": SOURCE, "q3": SOURCE}
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("envelope", [False, True])
def test_mounted_txt_import_roundtrips_whole_program(client_registry, monkeypatch, envelope):
    import asyncio
    from backend.tests.test_ocr_mounted_routes import headers
    client, registry = client_registry
    owner, task = seed("Implement sum_positive(nums). A helper is supplied:\ndef ok(x): return x > 0", COUNT)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    monkeypatch.setattr(registry.provider, "ainvoke", AsyncMock(side_effect=AssertionError("Native source reached text generation")), raising=False)
    module = '"""Example documentation:\r\n```\r\nprint(1)\r\n```\r\n"""\r\n' + SOURCE
    text = "```python\n" + module + "```\n" if envelope else module
    queued = client.post(f"/tasks/{task}/parse_submissions", headers=headers(owner),
                         files={"file": ("S1_Alice.txt", text.encode(), "text/plain")})
    assert queued.status_code == 200, queued.text
    asyncio.run(task_facade.run_durable_submission_recognition(_claim(owner, queued.json()["job_id"])))
    saved = client.get(f"/tasks/{task}", headers=headers(owner)).json()
    answers = saved["student_data"]["S1"]["stu_ans"]
    assert {a["q_id"]: a["content"] for a in answers} == {"q2": module, "q3": module}
    assert all("recognition_manual_pending" not in a["flag"] for a in answers)
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("doc", [
    "I explain below.\n" + SOURCE,
    "Analysis\n" + SOURCE,
    "Answers for today\n```python\n" + SOURCE + "```\n",
    "```python\ndef add(a, b): return a+b\n```\n```python\ndef other(x): return x\n```\n",
])
def test_ordinary_or_mixed_document_never_becomes_independent_source(doc):
    known = questions(SUM, COUNT)
    assert source_answers("work.txt", doc, known, set(known)) is None
    known["q2"]["type"] = "concept"
    assert source_answers("work.txt", SOURCE, known, set(known)) is None


def test_numbered_entry_hints_bind_the_function_after_each_explicit_number():
    source = "# Question 3\ndef later(x): return x\n# Question 2\ndef earlier(x): return x+1\n"
    known = questions("Implement a top-level function.", "Return the second result.")
    answers = source_answers("S1.txt", source, known, set(known))
    assert [(a["q_id"], a["flag"]) for a in answers] == [
        ("q2", ["programming_source_entry:earlier"]), ("q3", ["programming_source_entry:later"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 1])
async def test_old_mapping_checkpoint_recovers_original_bytes_and_previous_identity(monkeypatch, version):
    from dataclasses import replace
    from backend.db import file_repository
    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, AsyncMock(side_effect=AssertionError("No provider")))
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1_Alice.txt",
        content=SOURCE.encode(), content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved, = task_facade._load_submission_results(next(f for f in file_repository.list_files(
        owner_id=owner, assignment_id=task) if f.kind == "submission_recognition_result"))
    recognition = {} if version is None else {"native_source_mapping_version": version}
    old = replace(saved, student={**saved.student, "stu_id": "prior-identity", "recognition": recognition,
        "stu_ans": [{**a, "content": "return x" if version is None else SOURCE, "flag": ["recognition_manual_pending"]}
                    for a in saved.student["stu_ans"]]},
        student_candidate="prior-identity")
    restored = await task_facade._restore_legacy_native_result(old, questions(SUM, COUNT), task, owner)
    assert all(not a["flag"] for a in restored.student["stu_ans"])
    assert restored.student["recognition"]["native_source_mapping_version"] == 3
    assert restored.student["stu_id"] == "prior-identity" and restored.student["identity_status"] == "matched"
    assert {a["q_id"]: a["content"] for a in restored.student["stu_ans"]} == {"q2": SOURCE, "q3": SOURCE}
    registry.provider.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_replacement_of_ambiguous_source_survives_retry(monkeypatch):
    from types import SimpleNamespace
    from backend.api import tasks
    from backend.tests.test_task_background_workflows import _BackgroundTasks
    owner, task = seed(SUM, COUNT)
    registry = _registry(monkeypatch, AsyncMock(side_effect=AssertionError("No provider")))
    module = "def first(nums): return 1\ndef second(nums): return 2\n"
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1_Alice.txt",
        content=module.encode(), content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved = task_facade.get_task(task_id=task, owner_id=owner)
    row = next(a for a in saved["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q2")
    assert "recognition_manual_pending" in row["flag"]
    task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
        patch={"answer_id": row["answer_id"], "target_q_id": "q2", "content": SOURCE, "review_status": "confirmed"},
        expected_revision=saved["workflow_revision"])
    confirmed = task_facade.get_task(task_id=task, owner_id=owner)
    confirmed_row = next(a for a in confirmed["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q2")
    assert confirmed_row["content"] == SOURCE and confirmed_row["review_status"] == "confirmed"
    assert "recognition_manual_pending" not in confirmed_row["flag"]
    retried = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=confirmed["workflow_revision"]),
        current=SimpleNamespace(id=owner), background_tasks=_BackgroundTasks(), registry=registry)
    assert retried.status_code == 409
    assert task_facade.get_task(task_id=task, owner_id=owner)["student_data"] == confirmed["student_data"]
    registry.provider.ainvoke.assert_not_awaited()


def test_mounted_mixed_text_requires_teacher_review_before_grading(client_registry, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from backend.tests.test_ocr_mounted_routes import headers
    owner, task = seed(SUM)
    client, registry = client_registry
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    response = dict(stu_id="S1", stu_name="Alice", stu_ans=[dict(q_id="q2", number="2", type="programming",
        content="def sum_positive(nums): return 0", flag=["programming_source_entry:misleading"])])
    model = AsyncMock(return_value=SimpleNamespace(content=json.dumps(response)))
    monkeypatch.setattr(registry.provider, "ainvoke", model, raising=False)
    text = "My explanation of the algorithm follows.\n" + SOURCE
    queued = client.post(f"/tasks/{task}/parse_submissions", headers=headers(owner),
        files={"file": ("S1_Alice.txt", text.encode(), "text/plain")})
    assert queued.status_code == 200
    asyncio.run(task_facade.run_durable_submission_recognition(_claim(owner, queued.json()["job_id"])))
    row, = client.get(f"/tasks/{task}", headers=headers(owner)).json()["student_data"]["S1"]["stu_ans"]
    assert "recognition_manual_pending" in row["flag"] and "programming_source_entry:misleading" not in row["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(
        task_id=task, owner_id=owner)["blocking_issues"]
    model.assert_awaited_once()


@pytest.mark.parametrize("stem,expected_flags", [
    ("Implement solve(x). A provided helper follows:\ndef helper(x): return x", []),
    ("Implement solve(x) and implement helper(x).", ["recognition_manual_pending"]),
])
def test_source_mapping_respects_the_shared_teacher_entry_contract(stem, expected_flags):
    known = questions(stem)
    # Conflicting requirements still need review when the submission has just one def.
    source = "def solve(x): return x\n"
    answer, = source_answers("work.txt", source, known, set(known))
    assert answer["content"] == source
    assert answer["flag"] == expected_flags


@pytest.mark.parametrize("enclosed", [False, True])
def test_module_docstring_and_markdown_in_python_strings_survive_import_and_execution_input(enclosed):
    from backend.services.programming_runs import _source
    known = questions("Implement solve(x).")
    module = ('"""Student module documentation."""\n'
              'EXAMPLE = """Below is a Markdown example.\n```python\nprint(1)\n```\n"""\n'
              'def solve(x): return EXAMPLE + x\n\n')
    text = "```python\n" + module + "```\n" if enclosed else module
    answer, = source_answers("work.txt", text, known, set(known))
    assert not answer["flag"] and answer["content"] == module
    assert _source(answer["content"]) == _source(text) == module


def test_real_fences_and_prose_outside_module_still_require_review():
    from backend.services.programming_runs import _source
    known = questions("Implement solve(x).")
    blocks = '```python\ndef helper(x): return x\n```\n\n```python\ndef solve(x): return helper(x)\n```\n'
    assert source_answers("work.txt", blocks, known, set(known)) is None
    with pytest.raises(ValueError, match="programming_ambiguous_code_blocks"):
        _source(blocks)
    commentary = '"""Module docstring"""\ndef solve(x): return x\n"Another explanation."\n'
    assert source_answers("work.txt", commentary, known, set(known)) is None


def test_photo_of_code_can_be_reviewed_and_then_executed(client_registry, monkeypatch):
    """Real image/upload/storage/review; the visual transcription is a fixed response."""
    import asyncio
    import json
    from types import SimpleNamespace

    from PIL import Image, ImageDraw
    from backend.llm.providers import LLMResponse
    from backend.models import TestCase
    from backend.tests.test_ocr_mounted_routes import headers
    from backend.tools.code_interpreter import run_sandbox

    owner, task = seed(SUM)
    client, registry = client_registry
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    correct = "def sum_positive(nums):\n    return sum(x for x in nums if x > 0)"
    transcription = correct.replace("> 0", "< 0")
    with Image.new("RGB", (520, 80), "white") as photo:
        ImageDraw.Draw(photo).multiline_text((8, 8), correct, fill="black")
        data = io.BytesIO()
        photo.save(data, "PNG")
    async def fixed_vision(*args, **kwargs):
        from backend.llm.request_timeouts import current_response_scope
        async def response():
            return LLMResponse(content=transcription, provider="test-provider", model="test-model",
                               duration_ms=1, input_tokens=50, output_tokens=25)
        return await current_response_scope().run_response(response)
    vision = AsyncMock(side_effect=fixed_vision)
    monkeypatch.setattr(registry.provider, "supports_vision", True, raising=False)
    monkeypatch.setattr(registry.provider, "ainvoke_vision", vision, raising=False)
    structured = dict(stu_id="S1", stu_name="Alice", stu_ans=[dict(q_id="q2", number="2",
        type="programming", content=transcription, flag=[])])
    monkeypatch.setattr(registry.provider, "ainvoke", AsyncMock(return_value=SimpleNamespace(content=json.dumps(structured))), raising=False)
    upload = client.post(f"/tasks/{task}/parse_submissions", headers=headers(owner),
                         files={"file": ("S1_Alice.png", data.getvalue(), "image/png")})
    assert upload.status_code == 200, upload.text
    asyncio.run(task_facade.run_durable_submission_recognition(_claim(owner, upload.json()["job_id"])))
    snapshot = client.get(f"/tasks/{task}", headers=headers(owner)).json()
    row, = snapshot["student_data"]["S1"]["stu_ans"]
    vision.assert_awaited_once()
    assert row["content"] == transcription and "recognition_manual_pending" in row["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q2",
        patch={"answer_id": row["answer_id"], "target_q_id": "q2", "content": correct, "review_status": "confirmed"},
        expected_revision=snapshot["workflow_revision"])
    reviewed, = client.get(f"/tasks/{task}", headers=headers(owner)).json()["student_data"]["S1"]["stu_ans"]
    assert reviewed["content"] == correct and "recognition_manual_pending" not in reviewed["flag"]
    case = TestCase(io_mode="function", function_name="sum_positive", function_args=[[-2, 3, 4]], expected_return="7")
    assert asyncio.run(run_sandbox(reviewed["content"], [case])).passed_count == 1
