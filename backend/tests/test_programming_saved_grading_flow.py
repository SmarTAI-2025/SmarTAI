"""Mounted teacher edits -> frozen database revisions -> real synthetic source -> persisted grades."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.db.provider_repository import upsert_provider_config
from backend.main import create_app
from backend.models import ProviderConfig, TaskGradingSetup
from backend.services import grading_runs, programming_runs, task_facade
from backend.skills import programming
from backend.tests.test_task_background_workflows import _seed_task


@pytest.mark.parametrize("io_mode", ["function", "stdin"])
def test_teacher_edit_is_used_by_every_expert_and_execution_is_durable(monkeypatch, io_mode):
    exercise_saved_grading_flow(monkeypatch, io_mode=io_mode)


def exercise_saved_grading_flow(monkeypatch, *, io_mode, native_filename=None):
    """Save a teacher edit, grade it and return the row read back from SQLite."""
    owner, task = _seed_task(with_question=True)
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer " + create_token(owner, "teacher")}
    path = f"/tasks/{task}/problems/q1"
    def revision():
        return workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision
    old = {"source": "teacher", "io_mode": io_mode, "input": "2、3", "expected_output": "5"}
    if io_mode == "function":
        old.update(function_name="add", function_args=[2, 3], expected_return="5")
        stem = "实现 def add(a,b):"
        source = "from operator import add as helper\ndef unrelated(x): return 17\ndef add(a,b): return helper(a,b)\n"
    else:
        assert io_mode == "stdin"
        stem = "编写程序，从标准输入读入两个以空格分隔的整数，在标准输出打印和。"
        source = "a, b = map(int, input().split())\nprint(a + b)\n"
    flags = []
    if native_filename:
        from backend.services.programming_submission_mapping import source_answers
        source = ('#include <cstdio>\nint main(){int a,b; if(std::scanf("%d%d",&a,&b)==2) std::printf("%d\\n",a+b);}'
                  if native_filename.endswith(".cc") else
                  '#include <stdio.h>\nint main(void){int a,b; if(scanf("%d%d",&a,&b)==2) printf("%d\\n",a+b);}')
        answer, = source_answers(native_filename, source, {"q1":{"type":"编程题","stem":stem}}, {"q1"})
        flags = answer["flag"]
    save = client.put(path, headers=headers, json={"type":"编程题", "stem":stem,
        "criterion":"Return the sum of the two arguments (10 points).", "review_status":"edited",
        "test_cases":[old], "expected_workflow_revision":revision()})
    assert save.status_code == 200, save.text
    edit = {**old, "input":"3、4", "expected_output":"7"}
    edited = client.put(path, headers=headers, json={"test_cases":[edit],"expected_workflow_revision":revision()})
    assert edited.status_code == 200, edited.text
    stored = client.get(f"/tasks/{task}", headers=headers).json()["problem_data"]["q1"]["test_cases"][0]
    assert (stored["input"], stored["expected_output"]) == ("3、4", "7")
    if io_mode == "function":
        assert stored["function_args"] == [3, 4] and stored["expected_return"] == "7"
    else:
        assert stored.get("function_name") is None
    task_facade._commit_imported_submissions(task_id=task, owner_id=owner,
        course_id=assignment_repository.get_assignment(task,actor_id=owner).course_id,
        students=[{"stu_id":"constructed","stu_name":"Constructed", "source_id":"","identity_status":"matched",
            "identity_match_method":"filename","stu_ans":[{"q_id":"q1","number":"1","type":"编程题",
            "content":source,"flag":flags}]}], expected_workflow_revision=revision(),submission_file_name="constructed.txt")
    provider = upsert_provider_config(owner, ProviderConfig(provider_type="openai", model="fixture",
        api_key="test-no-network"),master_key=os.environ["SMARTAI_PROVIDER_ENCRYPTION_KEY"])
    setup = TaskGradingSetup(selected_provider_ids=[provider.id],primary_provider_id=provider.id,
                             multi_sample_n=2,knowledge_scope="none")
    workflow_repository.update_workflow(task,owner_id=owner,grading_setup=setup.model_dump(mode="json"),
                                        grading_setup_fingerprint="confirmed")
    selected = SimpleNamespace(provider_id="fixture",model="fixture")
    registry = SimpleNamespace(select=lambda *a,**k:registry, count=lambda:1,
        list_available=lambda:[selected],get=lambda key:selected,pick_default=lambda:selected)
    monkeypatch.setattr(grading_runs,"_registry_for",lambda _:registry)
    monkeypatch.setattr(grading_runs.settings,"e2e_fake_provider",False)
    monkeypatch.setattr(programming.kb_tool,"retrieve_for_grading",AsyncMock(return_value=[]))
    dispatch = programming.run_sandbox
    checkpoints = []

    async def observe_dispatch(code, cases, *, on_case_complete, **limits):
        async def checkpoint(row):
            await on_case_complete(row)
            checkpoints.append((row.case_id, row.case_index, row.execution_id))

        return await dispatch(code, cases, on_case_complete=checkpoint, **limits)

    executed = AsyncMock(side_effect=observe_dispatch)
    executed.supports_case_checkpoints = dispatch.supports_case_checkpoints
    monkeypatch.setattr(programming,"run_sandbox",executed)
    generation = AsyncMock(side_effect=AssertionError("saved cases must not be regenerated"))
    monkeypatch.setattr(programming,"_generate_test_cases",generation)
    async def fixed_score(*args,**kwargs):
        result = kwargs["output_model"].model_validate({"score":10,"max_score":10,"confidence":1,
            "comment":"Both arguments are added.","steps":[{"step_no":1,"desc":"sum",
            "is_correct":True,"score":10}],"logs":""})
        return result,SimpleNamespace(content=result.model_dump_json(),duration_ms=1)
    scoring = AsyncMock(side_effect=fixed_score)
    monkeypatch.setattr(programming,"structured_llm_call",scoring)
    job = task_facade.start_task_grading(task_id=task,owner_id=owner,expected_workflow_revision=revision(),
                                         request_id="programming-save-to-execution")["job_id"]
    asyncio.run(grading_runs.process_run(run_id=job,worker_id="programming-fixture"))
    result = task_facade.task_results(task_id=task,owner_id=owner)
    assert result["grading_run_status"] == "completed", result
    assert result["results"][0]["corrections"][0]["score"] == 10
    assert executed.await_count == 1 and generation.await_count == 0 and scoring.await_count == 2
    assert executed.call_args.args[0] == source
    activity = grading_runs.get_run(run_id=job, actor_id=owner, role="teacher")["events"]
    assert activity
    assert not {e["message"] for e in activity}.intersection({
        programming_runs.PREPARED, programming_runs.STARTED, programming_runs.COMPLETED,
    })
    events = grading_repository.list_events(job,actor_id=owner,messages=(programming_runs.COMPLETED,))
    assert len(events) == 1
    evidence = events[0]["payload"]["value"]
    assert evidence["identity"]["source_sha256"] == programming_runs.source_hash(source)
    row, = evidence["rows"]
    assert checkpoints == [(row["case_id"], row["case_index"], row["execution_id"])]
    assert row["passed"] and row["executed"]
    if io_mode == "function":
        assert (row["actual_output"], row["return_value"], row["stdout"]) == ("7", "7", "")
    else:
        assert executed.call_args.args[1][0].input == "3 4"
        assert (row["actual_output"], row["return_value"], row["stdout"]) == ("7\n", None, "7\n")
    return row
