from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.analytics.ask_workspace import AskDataError, QueryWorkspace, Snapshot, resolve_entity
from backend.agents.grounded_ask import ask


def payload():
    names = [("DEMO-001", "Alex Chen", [5, 8, 7, 4]), ("DEMO-002", "Maya Lin", [4.5, 8, 1, 8]),
             ("DEMO-003", "Jordan-Rivera", [3.5, 8, 0, 10]), ("DEMO-004", "Taylor Singh", [5, 8, 0, 10])]
    maxima = [5, 8, 7, 10]
    problems = {f"Q{i+1}": {"q_id": f"Q{i+1}", "number": str(i+1), "max_score": mx, "stem": f"Question {i+1}", "type": "calculation", "criterion": "Give reasons", "reference_answer": "known"} for i, mx in enumerate(maxima)}
    students = {sid: {"stu_id": sid, "stu_name": name, "stu_ans": [{"q_id": q, "content": f"{name}'s response", "review_status": "confirmed", "flag": []} for q in problems]} for sid, name, _ in names}
    results = [{"student_id": sid, "student_name": name, "corrections": [{"q_id": f"Q{i+1}", "score": val, "provisional_score": val, "max_score": maxima[i], "confidence": .99, "comment": "Feedback"} for i, val in enumerate(values)]} for sid, name, values in names]
    task = {"task_id": "task", "name": "Algebra", "workflow_revision": 3, "status": "finalized", "problem_data": problems, "student_data": students}
    return task, {"results": results, "problem_data": problems, "student_data": students}


@pytest.fixture
def snapshot():
    return Snapshot.from_payloads([payload()], "task")


class Provider:
    provider_id = "grounded-test"
    provider_type = "test"
    model = "deterministic"

    def __init__(self, *outputs):
        self.outputs = list(outputs); self.calls = []
    async def ainvoke(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0) if self.outputs else {"action": "clarify", "clarification": "Invalid plan"}
        if isinstance(output, Exception): raise output
        return SimpleNamespace(content=json.dumps(output))


def run(snapshot, question, *outputs, surface="student_analysis", history=None):
    return asyncio.run(ask(task_id="task", owner_id="teacher", question=question, surface=surface,
        provider=Provider(*outputs), snapshot=snapshot, history=history))


def test_question_four_incorrect_means_partial_credit_not_only_zero(snapshot):
    result = run(snapshot, "第4题有错误的学生", {"references": [{"kind": "question", "text": "第4题"}]},
        {"result_kind": "students", "sql": "SELECT s.student_id,s.student_name,g.q_id,g.score FROM students s JOIN grades g ON s.task_id=g.task_id AND s.student_id=g.student_id WHERE g.is_incorrect=1 ORDER BY g.score"})
    assert result["recognized"]
    assert result["selection"]["ids"] == ["DEMO-001", "DEMO-002"]
    assert [r[3] for r in result["data"]["rows"]] == [4, 8]


def test_alex_chart_has_alex_data_and_canonical_name(snapshot):
    result = run(snapshot, "alex各题得分柱状图", {"references": [{"kind": "student", "text": "alex"}]},
        {"result_kind": "chart", "sql": "SELECT g.student_id,g.q_id,g.score FROM grades g JOIN questions q ON q.task_id=g.task_id AND q.q_id=g.q_id ORDER BY q.order_index",
         "chart": {"type": "bar", "x": "q_id", "y": ["score"]}}, surface="chart")
    assert result["chart"]["title"] == "Alex Chen (DEMO-001)"
    assert result["chart"]["traces"][0]["y"] == [5, 8, 7, 4]
    assert "DEMO-002" not in json.dumps(result["data"])


def test_model_cannot_substitute_maya_after_resolving_alex(snapshot):
    result = run(snapshot, "Alex各题得分", {"references": [{"kind": "student", "text": "Alex"}]},
        {"result_kind": "chart", "sql": "SELECT student_id,q_id,score FROM grades WHERE student_id='DEMO-002'", "chart": {"type": "bar", "x": "q_id", "y": ["score"]}}, surface="chart")
    assert result["data"]["rows"] == []
    assert result["chart"]["traces"] == []


def test_chart_without_name_reference_keeps_each_actual_identity(snapshot):
    result = run(snapshot, "Alex各题得分柱状图", {},
        {"result_kind": "chart", "sql": "SELECT student_id,q_id,score FROM grades ORDER BY q_id", "chart": {"type": "bar", "x": "q_id", "y": ["score"]}}, surface="chart")
    assert len(result["chart"]["traces"]) == 4
    assert result["chart"]["traces"][0]["name"] == "Alex Chen (DEMO-001) · score"


def test_mismatched_model_student_name_is_not_echoed(snapshot):
    result = run(snapshot, "Maya的成绩", {"references": [{"kind": "student", "text": "Maya"}]},
        {"result_kind": "table", "sql": "SELECT student_id,'Alex' AS student_name,total_score FROM students"})
    assert result["data"]["rows"] == [["DEMO-002", "Maya Lin", 21.5]]


@pytest.mark.parametrize("text,field,operator,expected", [
    ("alex", "student_name", "contains", ["DEMO-001"]),
    ("chen", "student_name", "contains", ["DEMO-001"]),
    ("ale", "student_name", "contains", ["DEMO-001"]),
    ("ale chen", "student_name", "exact", []),
    ("Alex Wu", "student_name", "exact", []),
    ("Alex Chne", "student_name", "exact", []),
    ("Jordan", "student_name", "contains", ["DEMO-003"]),
    ("Rivera", "student_name", "contains", ["DEMO-003"]),
    ("DEMO-001", "student_id", "exact", ["DEMO-001"]),
])
def test_set_search_has_no_nearest_name_fallback(snapshot, text, field, operator, expected):
    match = resolve_entity(snapshot, "student", text, field=field, operator=operator)
    assert [r["student_id"] for r in match.rows] == expected


def test_multiple_chens_are_all_returned_without_clarification(snapshot):
    snapshot.tables["students"].append({**snapshot.tables["students"][0], "student_id": "OTHER", "student_name": "Ava Chen"})
    result = run(snapshot, "chen", {"references": [{"kind": "student", "field": "student_name", "text": "chen", "operator": "contains"}]},
        {"result_kind": "students", "sql": "SELECT student_id,student_name FROM students ORDER BY student_id"})
    assert result["recognized"] and result["selection"]["ids"] == ["DEMO-001", "OTHER"]
    assert result["candidates"] == []


@pytest.mark.parametrize("text", ["Alex Wu", "ale chen"])
def test_absent_complete_name_returns_successful_empty_set(snapshot, text):
    result = run(snapshot, text, {"references": [{"kind": "student", "field": "student_name", "text": text, "operator": "exact"}]},
        {"result_kind": "students", "sql": "SELECT student_id,student_name FROM students"})
    assert result["recognized"] and result["selection"]["ids"] == []
    assert result["candidates"] == []


def test_teacher_score_overrides_ai_and_failed_score_stays_unknown():
    task, result = payload()
    c = result["results"][0]["corrections"][3]; c["teacher_score"] = 10
    result["results"][1]["corrections"][3]["synthesis_method"] = "all_failed"
    snapshot = Snapshot.from_payloads([(task, result)], "task")
    db = QueryWorkspace(snapshot)
    try:
        data = db.execute("SELECT student_id,score,is_incorrect FROM grades WHERE q_id='Q4' ORDER BY student_id")
        assert data["rows"][:2] == [["DEMO-001", 10, 0], ["DEMO-002", None, None]]
    finally:
        db.close()


@pytest.mark.parametrize("sql,expected", [
    ("SELECT COUNT(*) AS n FROM grades WHERE is_incorrect=1", [[7]]),
    ("SELECT student_id FROM students WHERE total_score>21.5 AND total_score<=24 ORDER BY total_score DESC", [["DEMO-001"], ["DEMO-004"]]),
    ("SELECT student_id FROM students WHERE NOT student_id='DEMO-001' AND (total_score=23 OR total_score=21.5) ORDER BY student_id", [["DEMO-002"], ["DEMO-003"], ["DEMO-004"]]),
    ("WITH x AS (SELECT student_id,total_score FROM students) SELECT student_id,rank() OVER (ORDER BY total_score DESC) AS ranking FROM x ORDER BY ranking,student_id", [["DEMO-001",1],["DEMO-004",2],["DEMO-002",3],["DEMO-003",3]]),
    ("SELECT q_id,SUM(points_lost) AS lost FROM grades GROUP BY q_id HAVING SUM(points_lost)>5 ORDER BY lost DESC", [["Q3",20],["Q4",8]]),
    ("SELECT student_id FROM students WHERE total_score>(SELECT AVG(total_score) FROM students) ORDER BY total_score DESC", [["DEMO-001"],["DEMO-004"]]),
])
def test_general_sql_not_a_fixed_filter_vocabulary(snapshot, sql, expected):
    db = QueryWorkspace(snapshot)
    try:
        assert db.execute(sql)["rows"] == expected
        assert db.execute(sql)["rows"] == expected  # authorizer must not be lost to statement caching
    finally:
        db.close()


@pytest.mark.parametrize("sql", [
    "DELETE FROM students", "UPDATE grades SET score=10", "DROP TABLE grades", "PRAGMA database_list", "ATTACH DATABASE '/tmp/other.db' AS secret",
    "SELECT * FROM sqlite_master", "SELECT readfile('/etc/passwd') FROM students", "SELECT load_extension('/tmp/x') FROM students",
    "SELECT * FROM users", "SELECT * FROM students; DELETE FROM grades", "SELECT randomblob(2000000000) FROM students",
    "SELECT printf('%2000000000s',student_id) FROM students", "SELECT 1 AS fabricated", "SELECT s.student_id,g.student_id FROM students s JOIN grades g USING(task_id)",
])
def test_untrusted_sql_cannot_write_read_external_or_exhaust_memory(snapshot, sql):
    db = QueryWorkspace(snapshot)
    try:
        with pytest.raises(AskDataError):
            db.execute(sql)
        assert db.execute("SELECT count(*) AS n FROM students")["rows"] == [[4]]
    finally:
        db.close()


def test_no_silent_result_truncation(snapshot):
    db = QueryWorkspace(snapshot)
    try:
        with pytest.raises(AskDataError, match="结果过多"):
            db.execute("SELECT * FROM students", max_rows=2)
    finally:
        db.close()


def test_full_population_over_fifty_is_used_for_aggregation():
    task, result = payload()
    result["results"] = []
    task["student_data"] = {str(i): {"stu_id":str(i), "stu_name":f"Person {i}", "stu_ans":[]} for i in range(81)}
    result["student_data"] = task["student_data"]
    db = QueryWorkspace(Snapshot.from_payloads([(task,result)],"task"))
    try:
        assert db.execute("SELECT count(*) AS n FROM students")["rows"] == [[81]]
    finally:
        db.close()


def test_direct_model_chart_arrays_are_rejected(snapshot):
    result = run(snapshot,"Alex柱状图",{}, *[{"result_kind":"chart","traces":[{"y":[4.5,8,1,8]}]}]*3, surface="chart")
    assert not result["recognized"] and result["chart"] is None


def test_person_chart_without_identity_is_rejected(snapshot):
    bad={"result_kind":"chart","sql":"SELECT q_id,score FROM grades","chart":{"type":"bar","x":"q_id","y":["score"]}}
    result = run(snapshot,"Alex柱状图",{"references":[{"kind":"student","text":"Alex"}]},bad,bad,bad,surface="chart")
    assert not result["recognized"]


def test_inspection_then_final_query_never_uses_sample_as_population(snapshot):
    p = Provider({}, {"action":"inspect","sql":"SELECT * FROM students LIMIT 1"}, {"result_kind":"table","sql":"SELECT count(*) AS n FROM students"})
    result=asyncio.run(ask(task_id="task",owner_id="t",question="有多少学生？",surface="student_analysis",provider=p,snapshot=snapshot))
    assert result["data"]["rows"] == [[4]] and len(p.calls)==3


def test_follow_up_receives_history_but_new_name_wins(snapshot):
    p=Provider({"references":[{"kind":"student","text":"Maya"}]},{"result_kind":"table","sql":"SELECT student_id,total_score FROM students"})
    result=asyncio.run(ask(task_id="task",owner_id="t",question="再看Maya",history=["Alex的成绩"],surface="student_analysis",provider=p,snapshot=snapshot))
    assert result["data"]["rows"]==[["DEMO-002",21.5]]
    assert "Alex的成绩" in p.calls[0][-1].content


@pytest.mark.parametrize("utterance,operator,value,ids", [
    ("PB20开头学号", "prefix", "PB20", ["PB20001", "PB20156"]),
    ("56结尾学号", "suffix", "56", ["PB20156", "123ABC456", "123AB456", "123ABCD456"]),
    ("123 *** 456", "pattern", "123___456", ["123ABC456"]),
    ("123 xxx 456", "pattern", "123___456", ["123ABC456"]),
    ("123 ... 456", "pattern", "123___456", ["123ABC456"]),
    ("123任意长度456", "pattern", "123%456", ["123ABC456", "123AB456", "123ABCD456"]),
])
def test_model_chosen_id_operators_are_generic(snapshot, utterance, operator, value, ids):
    source = snapshot.tables["students"][0]
    snapshot.tables["students"] = [{**source, "student_id": sid} for sid in ["PB20001", "PB20156", "123ABC456", "123AB456", "123ABCD456"]]
    text = utterance if operator == "pattern" else value
    result = run(snapshot, utterance, {"references": [{"kind": "student", "field": "student_id", "text": text, "operator": operator, "value": value}]},
        {"result_kind": "students", "sql": "SELECT student_id FROM students"})
    assert result["recognized"] and set(result["selection"]["ids"]) == set(ids)
    assert result["candidates"] == []


def test_identity_literal_cannot_be_autocorrected_by_model(snapshot):
    result = run(snapshot, "ale chen", {"references": [{"kind": "student", "field": "student_name", "text": "ale chen", "operator": "exact", "value": "Alex Chen"}]})
    assert not result["recognized"] and result["data"]["rows"] == []


def test_raw_grade_cannot_be_relabelled_to_another_student(snapshot):
    plan = {"result_kind": "chart", "sql": "SELECT 'DEMO-001' AS student_id,q_id,score FROM grades WHERE student_id='DEMO-002'", "chart": {"type": "bar", "x": "q_id", "y": ["score"]}}
    result = run(snapshot, "比较Alex和全班", {"references": [{"kind": "student", "text": "Alex"}], "include_class_context": True}, plan, plan, plan, surface="chart")
    assert not result["recognized"] and result["chart"] is None


def test_machine_json_preserves_sql_quotes_and_like_underscores(snapshot):
    result = run(snapshot, "查DEMO-002", {"references": [{"kind": "student", "text": "DEMO-002", "field": "student_id", "operator": "exact"}]},
        {"result_kind": "students", "sql": "SELECT student_id FROM students WHERE student_id LIKE 'DEMO-00_'"})
    assert result["selection"]["ids"] == ["DEMO-002"]
    assert result["sql"].endswith("'DEMO-00_'")


def test_invented_reference_literal_is_not_used_as_a_name_correction(snapshot):
    result=run(snapshot,"ale chen",{"references":[{"kind":"student","text":"Alex Chen","operator":"exact"}]})
    assert not result["recognized"] and result["data"]["rows"]==[]


def test_default_question_reference_does_not_match_q40(snapshot):
    snapshot.tables["questions"].append({**snapshot.tables["questions"][3],"q_id":"Q40","number":"40"})
    assert [row["q_id"] for row in resolve_entity(snapshot,"question","第4题").rows]==["Q4"]
