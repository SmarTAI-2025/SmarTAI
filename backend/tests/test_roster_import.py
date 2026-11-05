"""Roster-only delimiter and validation contracts; no OCR or model calls."""
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from backend.api import tasks
from backend.domain.errors import ValidationError
from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
@pytest.mark.parametrize("filename,delimiter", [
    ("roster.csv", ","), ("ROSTER.TSV", "\t"),
    ("roster.txt", ","), ("roster.txt", "\t"),
])
def test_supported_rosters_preserve_ids_and_chinese_names(filename, delimiter, encoding):
    body = f"学号{delimiter}姓名\r\n00123{delimiter}张三\r\n".encode(encoding)
    assert tasks._parse_roster(body, filename=filename) == [{"stu_id": "00123", "stu_name": "张三"}]


@pytest.mark.parametrize("filename,delimiter", [("roster.csv", ","), ("roster.tsv", "\t"), ("roster.txt", "\t")])
def test_quoted_fields_keep_embedded_delimiters_quotes_and_newlines(filename, delimiter):
    body = f'student_id{delimiter}student_name{delimiter}note\nS1{delimiter}"Li,\t""Lee""\nJr"{delimiter}ignored\n'.encode()
    assert tasks._parse_roster(body, filename=filename) == [{"stu_id": "S1", "stu_name": 'Li,\t"Lee"\nJr'}]


def test_csv_default_and_header_aliases_remain_compatible():
    assert tasks._parse_roster(b" STU_ID , Name ,note\nS1,Alice,%PDF-1.7\n\nS2,,empty name\n") == [
        {"stu_id": "S1", "stu_name": "Alice"}, {"stu_id": "S2", "stu_name": ""},
    ]


@pytest.mark.parametrize("filename,delimiter", [("roster.csv", ","), ("roster.tsv", "\t"), ("roster.txt", "\t")])
def test_quoted_headers_and_blank_leading_records(filename, delimiter):
    body = f'\r\n"student_id"{delimiter}"name"\r\nS1{delimiter}Alice\r\n'.encode()
    assert tasks._parse_roster(body, filename=filename) == [{"stu_id": "S1", "stu_name": "Alice"}]


@pytest.mark.parametrize("filename", ["roster.csv", "roster.tsv", "roster.txt"])
def test_id_only_rosters_remain_compatible(filename):
    assert tasks._parse_roster(b"student_id\n00123\n", filename=filename) == [{"stu_id": "00123", "stu_name": ""}]


@pytest.mark.parametrize("parsed,filename", [({"stu_id": "00123"}, "answer.txt"), ({}, "00123_answer.txt")])
def test_id_only_roster_still_matches_by_recognized_id_or_filename(parsed, filename):
    from backend.agents.ingest_agent import _match_roster_identity
    roster = tasks._parse_roster(b"student_id\n00123\n00999\n", filename="roster.tsv")
    assert _match_roster_identity(parsed, filename, roster) == {"stu_id": "00123", "stu_name": ""}


def test_id_only_roster_does_not_invent_a_match_from_name_alone():
    from backend.agents.ingest_agent import _match_roster_identity
    roster = tasks._parse_roster(b"student_id\n00123\n00999\n", filename="roster.txt")
    assert _match_roster_identity({"stu_name": "Alice"}, "Alice_answer.txt", roster) is None


@pytest.mark.parametrize("filename,body,code", [
    ("roster.xlsx", b"student_id,name\nS1,Alice", "submission_roster_unsupported"),
    ("roster.csv", b"student_id\tname\nS1\tAlice", "submission_roster_delimiter_mismatch"),
    ("roster.tsv", b"student_id,name\nS1,Alice", "submission_roster_delimiter_mismatch"),
    ("roster.csv", b'"student_id"\t"name"\nS1\tAlice', "submission_roster_delimiter_mismatch"),
    ("roster.tsv", b'"student_id","name"\nS1,Alice', "submission_roster_delimiter_mismatch"),
    ("roster.tsv", b"", "submission_roster_empty"),
    ("roster.txt", b"\xef\xbb\xbf \n\t\n", "submission_roster_empty"),
    ("roster.tsv", b"student_id\tname\n", "submission_roster_empty"),
    ("roster.txt", b"student_id;name\nS1;Alice", "submission_roster_headers_invalid"),
    ("roster.csv", b"name\nAlice", "submission_roster_headers_invalid"),
    ("roster.csv", b"student_id,name,name\nS1,Alice,Bob", "submission_roster_headers_invalid"),
    ("roster.tsv", b"student_id\tname\nS1\t\"Alice", "submission_roster_invalid"),
    ("roster.txt", b'"student_id\tname\nS1\tAlice', "submission_roster_invalid"),
    ("roster.csv", b"student_id,name\nS1,Alice,extra", "submission_roster_rows_invalid"),
    ("roster.tsv", b"student_id\tname\nS1", "submission_roster_rows_invalid"),
    ("roster.csv", b"student_id,name\n,Alice", "submission_roster_rows_invalid"),
    ("roster.txt", b"\xff", "submission_roster_decode_failed"),
    ("roster.csv", b"student_id,name\nS1,Ali\x00ce", "submission_roster_invalid"),
])
def test_invalid_rosters_have_stable_actionable_errors(filename, body, code):
    with pytest.raises(ValidationError) as caught:
        tasks._parse_roster(body, filename=filename)
    assert caught.value.code == code


def test_missing_id_error_reports_physical_line_without_exposing_cells():
    with pytest.raises(ValidationError) as caught:
        tasks._parse_roster(b'\nstudent_id,name\nS1,"Multi\nline"\n,Alice', filename="roster.csv")
    assert caught.value.code == "submission_roster_rows_invalid"
    assert caught.value.details == {"line": 5}
    assert "Alice" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,body", [
    ("students.tsv", "学号\t姓名\n001,2\t张三\n".encode("gbk")),
    ("students.csv", b'student_id,name\n"001,2",Alice\n'),
    ("students.txt", b'student_id\tname\n001,2\tAlice\n'),
])
async def test_endpoint_passes_parsed_roster_to_queue(filename, body, monkeypatch):
    owner, task_id = _seed_task(with_question=True)
    captured = {}
    def queue(**kwargs):
        captured.update(kwargs)
        return {"status": "started", "task_id": task_id}
    monkeypatch.setattr(tasks.task_facade, "queue_task_submission_parsing", queue)
    result = await tasks.parse_submissions_endpoint(
        task_id=task_id, background_tasks=_BackgroundTasks(),
        file=UploadFile(filename="answer.txt", file=io.BytesIO(b"Q1: synthetic answer")),
        roster_file=UploadFile(filename=filename, file=io.BytesIO(body)),
        identity_mode="roster", current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    assert result["status"] == "started"
    assert captured["roster_name"] == filename
    assert captured["roster_entries"][0]["stu_id"] == "001,2"
    assert captured["roster_entries"][0]["stu_name"] == ("张三" if filename.endswith("tsv") else "Alice")
    assert captured["content"] == b"Q1: synthetic answer"


@pytest.mark.asyncio
async def test_bad_roster_does_not_queue_or_change_workflow(monkeypatch):
    from backend.db import workflow_repository
    owner, task_id = _seed_task(with_question=True)
    before = workflow_repository.get_live_workflow(task_id, owner_id=owner)
    def forbidden(**kwargs):
        pytest.fail("An invalid roster must not queue recognition")
    monkeypatch.setattr(tasks.task_facade, "queue_task_submission_parsing", forbidden)
    result = await tasks.parse_submissions_endpoint(
        task_id=task_id, background_tasks=_BackgroundTasks(),
        file=UploadFile(filename="answer.txt", file=io.BytesIO(b"synthetic answer")),
        roster_file=UploadFile(filename="students.tsv", file=io.BytesIO(b"student_id,name\nS1,Alice")),
        identity_mode="roster", current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    assert result.status_code == 422
    assert json.loads(result.body)["error"]["code"] == "submission_roster_delimiter_mismatch"
    after = workflow_repository.get_live_workflow(task_id, owner_id=owner)
    assert (after.workflow_revision, after.parse_job_id) == (before.workflow_revision, before.parse_job_id)


@pytest.mark.asyncio
async def test_oversized_roster_is_bounded_and_never_queued(monkeypatch):
    from unittest.mock import AsyncMock
    owner, task_id = _seed_task(with_question=True)
    def forbidden(**kwargs):
        pytest.fail("An oversized roster must not queue recognition")
    monkeypatch.setattr(tasks.task_facade, "queue_task_submission_parsing", forbidden)
    monkeypatch.setattr(tasks, "SUBMISSION_UPLOAD_MAX_BYTES", 32)
    roster = UploadFile(filename="roster.tsv", file=io.BytesIO(b"x" * 33))
    read = AsyncMock(wraps=roster.read)
    monkeypatch.setattr(roster, "read", read)
    result = await tasks.parse_submissions_endpoint(
        task_id=task_id, background_tasks=_BackgroundTasks(),
        file=UploadFile(filename="answer.txt", file=io.BytesIO(b"answer")),
        roster_file=roster, identity_mode="roster", current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    assert result.status_code == 413
    assert json.loads(result.body)["error"]["code"] == "submission_roster_too_large"
    read.assert_awaited_once_with(33)
