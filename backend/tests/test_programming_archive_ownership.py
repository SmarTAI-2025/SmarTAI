"""Archive member ownership through the same durable import used by the task UI."""
import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from backend.db import file_repository
from backend.domain.errors import DomainError
from backend.services import task_facade
from backend.services.submission_manual_review import continue_manually
from backend.storage import get_storage
from backend.tests.test_programming_source_answers import SUM, seed
from backend.tests.test_submission_incremental import _claim, _registry

CODE = b"def sum_positive(xs):\n    return sum(x for x in xs if x>0)\n"
PROJECT = "submission_programming_multiple_files"


def zipped(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, contents in entries:
            z.writestr(name, contents)
    return out.getvalue()


async def batch(monkeypatch, entries, *, mode="filename", roster=None):
    owner, task = seed(SUM)
    registry = _registry(monkeypatch, AsyncMock(side_effect=AssertionError("No native source needs a model")))
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, content=zipped(entries),
        filename="class.zip", content_type="application/zip", identity_mode=mode, roster_entries=roster, registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    return owner, task, queued["job_id"], task_facade.get_task(task_id=task, owner_id=owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [
    ["classA/S1_Alice/main.py", "classA/S2_Bob/src/main.py"],
    ["S1_Alice.py", "S2_Bob.py"], ["classA/S1_Alice.py", "classA/S2_Bob.py"],
])
async def test_unique_students_preserve_full_archive_paths(monkeypatch, paths):
    _, _, _, task = await batch(monkeypatch, [(name, CODE) for name in paths])
    assert set(task["student_data"]) == {"S1", "S2"}
    assert [(task["student_data"][sid]["source_filename"], task["student_data"][sid]["identity_status"])
            for sid in ("S1", "S2")] == [(name, "matched") for name in paths]


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [
    ["class/S1_Alice/main.py", "class/S1_Alice/lib/helper.py"],
    ["class/main.py", "class/helper.py"], ["main.py", "helper.py"],
])
async def test_multiple_files_never_publish_phantom_students_and_keep_originals(monkeypatch, paths):
    owner, tid, job, task = await batch(monkeypatch, [(name, CODE) for name in paths])
    assert not task["student_data"]
    assert {s["reason_code"] for s in task["submission_sources"]} == {PROJECT}
    assert all(s["requires_reupload"] for s in task["submission_sources"])
    originals = [f for f in file_repository.list_files(owner_id=owner, assignment_id=tid)
                 if f.kind == "submission_archive_member"]
    assert sorted(f.original_name for f in originals) == sorted(paths)
    for f in originals:
        with get_storage().open(f.storage_key) as inp:
            assert inp.read() == CODE
    with pytest.raises(DomainError) as exc:
        await continue_manually(task_id=tid, owner_id=owner, job_id=job, expected_revision=task["workflow_revision"])
    assert exc.value.code == PROJECT
    assert not task_facade.get_task(task_id=tid, owner_id=owner)["student_data"]


@pytest.mark.asyncio
async def test_mixed_batch_manual_recovers_only_independent_student(monkeypatch):
    archive = [("class/S1_Alice/main.py", CODE), ("class/S1_Alice/helpers/util.py", CODE),
               ("class/S2_Bob/main.py", CODE)]
    owner, tid, job, task = await batch(monkeypatch, archive)
    assert not task["student_data"]
    assert {s["file_name"]: s["status"] for s in task["submission_sources"]} == {
        path: ("parsed" if "S2_Bob" in path else "failed") for path, _ in archive}
    await continue_manually(task_id=tid, owner_id=owner, job_id=job, expected_revision=task["workflow_revision"])
    after = task_facade.get_task(task_id=tid, owner_id=owner)
    assert set(after["student_data"]) == {"S2"}
    assert after["student_data"]["S2"]["stu_ans"][0]["content"].encode() == CODE
    assert all(s["requires_reupload"] and s["reason_code"] == PROJECT
               for s in after["submission_sources"] if "S1_Alice" in s["file_name"])
    assert not task_facade.grading_readiness(task_id=tid, owner_id=owner)["ready"]


@pytest.mark.asyncio
async def test_roster_matches_whole_path_components_and_does_not_disclose_or_guess(monkeypatch):
    roster = [{"stu_id": "S1", "stu_name": "Alice"}, {"stu_id": "S10", "stu_name": "Bob"}]
    _, _, _, task = await batch(monkeypatch, [("class_S1/Alice/main.py", CODE), ("class_S1/S10/main.py", CODE)],
                                 mode="roster", roster=roster)
    assert {k: (v["stu_name"], v["identity_status"]) for k, v in task["student_data"].items()} == {
        "S1": ("Alice", "matched"), "S10": ("Bob", "matched")}
    _, _, _, anonymous = await batch(monkeypatch, [("class_S1/project/main.py", CODE)], mode="roster", roster=roster)
    row, = anonymous["student_data"].values()
    assert row["identity_status"] == "needs_review" and row["stu_id"] not in {"S1", "S10"}
    _, _, _, collision = await batch(monkeypatch, [("class/S1_Bob/main.py", CODE)], mode="roster", roster=roster)
    row, = collision["student_data"].values()
    assert row["identity_status"] == "needs_review" and row["stu_id"] not in {"S1", "S10"}


@pytest.mark.asyncio
async def test_nested_student_archive_is_still_opaque(monkeypatch):
    _, _, _, task = await batch(monkeypatch, [("class/S1_Alice/project.zip", zipped([("secret.py", CODE)])),
                                              ("class/S2_Bob/main.py", CODE)])
    assert {s["file_name"]: s["reason_code"] for s in task["submission_sources"]} == {
        "class/S1_Alice/project.zip": "submission_archive_nested_unsupported", "class/S2_Bob/main.py": None}




@pytest.mark.asyncio
@pytest.mark.parametrize("programming", [False, True])
async def test_folder_identity_is_scoped_to_programming_documents(monkeypatch, programming):
    import json
    from types import SimpleNamespace
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.db import assignment_repository
    owner, tid = _seed_task()
    assignment_repository.add_question(tid, teacher_id=owner, q_id="q1", number="1", order_index=0,
        type="programming" if programming else "calculation", stem=SUM if programming else "Calculate 2+3.",
        criterion="Award 10 points for the correct solution.")
    reply = dict(stu_id="unidentified", stu_name="[Unknown Student]", stu_ans=[
        dict(q_id="q1", number="1", type="programming" if programming else "calculation", content="The answer is five.", flag=[])])
    registry = _registry(monkeypatch, lambda _: SimpleNamespace(content=json.dumps(reply)))
    queued = task_facade.queue_task_submission_parsing(task_id=tid, owner_id=owner, filename="class.zip",
        content=zipped([("class/S2_Bob/work.txt", b"Question 1: The answer is five.")]),
        content_type="application/zip", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved = task_facade.get_task(task_id=tid, owner_id=owner)
    row, = saved["student_data"].values()
    assert row["source_filename"] == "class/S2_Bob/work.txt"
    assert row["stu_ans"][0]["content"] == "The answer is five."
    assert (row["stu_id"], row["identity_status"]) == (("S2", "matched") if programming else ("unidentified", "needs_review"))


@pytest.mark.asyncio
async def test_anonymous_project_does_not_implicate_an_identified_student(monkeypatch):
    entries = [("class/project/main.py", CODE), ("class/project/helper.py", CODE),
               ("class/S2_Bob.py", CODE)]
    _, _, _, task = await batch(monkeypatch, entries)
    assert {s["file_name"]: s["reason_code"] for s in task["submission_sources"]} == {
        name: (None if name.endswith("S2_Bob.py") else PROJECT) for name, _ in entries}


@pytest.mark.asyncio
@pytest.mark.parametrize("paths,labels", [
    (["class/Ada.py", "class/Bo.py"], {"Ada", "Bo"}),
    (["class/Ada/main.py", "class/Bo/main.py"], {"Ada", "Bo"}),
])
async def test_distinct_unverified_file_or_folder_labels_keep_separate_candidates(monkeypatch, paths, labels):
    _, _, _, task = await batch(monkeypatch, [(name, CODE) for name in paths])
    assert set(task["student_data"]) == labels
    assert all(row["identity_status"] == "needs_review" for row in task["student_data"].values())
    assert {row["source_filename"] for row in task["student_data"].values()} == set(paths)
