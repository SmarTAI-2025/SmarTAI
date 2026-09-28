"""Owner-scoped business data for Ask. No model ever connects to the application DB.

The query database is a disposable, read-only SQLite projection of the SAME
presentation records used by the UI. It contains no accounts, tokens or provider
configuration. Full rows are loaded before aggregates; limits fail explicitly.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from backend.domain.errors import NotFound
from backend.services import task_facade

MAX_SOURCE_ROWS = 100_000
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_RESULT_ROWS = 5_000
MAX_RESULT_BYTES = 2 * 1024 * 1024


class AskDataError(ValueError):
    """A safe, user-facing failure, not a partial result."""


def normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def confidence(value: Any) -> float | None:
    n = number(value)
    return None if n is None or not 0 <= n <= 100 else n / 100 if n > 1 else n


def score(correction: dict) -> float | None:
    teacher = number(correction.get("teacher_score"))
    if teacher is not None:
        return teacher
    reasons = set(correction.get("review_reasons") or [])
    if correction.get("synthesis_method") in {"all_failed", "quota_exhausted"} or reasons.intersection({
        "llm_failed", "quota_exhausted", "invalid_score_scale", "missing_correction", "missing_student_result",
    }):
        return None
    current = number(correction.get("score"))
    return current if current is not None else number(correction.get("provisional_score"))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# SQL-visible names deliberately exclude internal IDs. Composite relationships
# always include task_id: two different classes can both have student_id="001".
SCHEMA: dict[str, dict[str, str]] = {
    "tasks": {"task_id": "TEXT", "name": "TEXT", "status": "TEXT", "course_id": "TEXT", "course_name": "TEXT", "semester_id": "TEXT", "created_at": "REAL", "updated_at": "REAL", "problem_count": "INTEGER", "student_count": "INTEGER", "workflow_revision": "INTEGER", "result_version": "INTEGER", "metadata": "TEXT"},
    "students": {"task_id": "TEXT", "student_id": "TEXT", "student_name": "TEXT", "identity_status": "TEXT", "source_filename": "TEXT", "total_score": "REAL", "total_max": "REAL", "score_percent": "REAL", "avg_confidence": "REAL", "graded_count": "INTEGER", "unscored_count": "INTEGER", "low_confidence_count": "INTEGER", "review_count": "INTEGER", "pending_review_count": "INTEGER", "metadata": "TEXT"},
    "questions": {"task_id": "TEXT", "q_id": "TEXT", "number": "TEXT", "order_index": "INTEGER", "type": "TEXT", "stem": "TEXT", "reference_answer": "TEXT", "criterion": "TEXT", "solution_code": "TEXT", "max_score": "REAL", "avg_score": "REAL", "avg_percent": "REAL", "avg_confidence": "REAL", "response_count": "INTEGER", "review_count": "INTEGER", "review_status": "TEXT", "metadata": "TEXT"},
    "answers": {"task_id": "TEXT", "student_id": "TEXT", "q_id": "TEXT", "content": "TEXT", "state": "TEXT", "review_status": "TEXT", "flags": "TEXT", "metadata": "TEXT"},
    "grades": {"task_id": "TEXT", "student_id": "TEXT", "q_id": "TEXT", "score": "REAL", "max_score": "REAL", "score_percent": "REAL", "points_lost": "REAL", "is_incorrect": "INTEGER", "is_scored": "INTEGER", "ai_score": "REAL", "teacher_score": "REAL", "confidence": "REAL", "comment": "TEXT", "teacher_comment": "TEXT", "requires_review": "INTEGER", "reviewed": "INTEGER", "review_status": "TEXT", "review_reasons": "TEXT", "synthesis_method": "TEXT", "metadata": "TEXT"},
    "expert_results": {"task_id": "TEXT", "student_id": "TEXT", "q_id": "TEXT", "expert_index": "INTEGER", "expert_name": "TEXT", "score": "REAL", "confidence": "REAL", "comment": "TEXT", "metadata": "TEXT"},
    "grading_steps": {"task_id": "TEXT", "student_id": "TEXT", "q_id": "TEXT", "step_index": "INTEGER", "metadata": "TEXT"},
    "preparation_issues": {"task_id": "TEXT", "q_id": "TEXT", "code": "TEXT", "field": "TEXT", "severity": "TEXT", "status": "TEXT", "message": "TEXT"},
    "test_cases": {"task_id": "TEXT", "q_id": "TEXT", "case_index": "INTEGER", "input": "TEXT", "expected_output": "TEXT", "visibility": "TEXT", "source": "TEXT", "metadata": "TEXT"},
    "knowledge_documents": {"task_id": "TEXT", "document_id": "TEXT", "name": "TEXT", "content": "TEXT", "metadata": "TEXT"},
    "task_tags": {"task_id": "TEXT", "tag_id": "TEXT", "tag_name": "TEXT"},
}


def _mean(values: list[float | None]) -> float | None:
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


def _question_order(value: dict) -> tuple:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", str(value.get("number") or value.get("q_id") or "")))


def project_task(task: dict, result: dict) -> dict[str, list[dict]]:
    """Turn the public task/result response into relational records, without sampling."""
    tables: dict[str, list[dict]] = {name: [] for name in SCHEMA}
    tid = str(task["task_id"])
    questions = result.get("problem_data") or task.get("problem_data") or {}
    submissions = result.get("student_data") or task.get("student_data") or {}
    rows = result.get("results") or []
    if not isinstance(rows, list):
        raise AskDataError("任务结果结构异常，请重新加载。 / Invalid task result structure.")
    by_student = {}
    for item in rows:
        sid = str(item["student_id"])
        if sid in by_student:
            raise AskDataError("学生标识重复，无法可靠关联成绩。 / Duplicate student identity.")
        by_student[sid] = item
    # Grade-only identities remain visible, exactly as in buildResultsModel.
    all_students = dict(submissions)
    for sid, item in by_student.items():
        all_students.setdefault(sid, {"stu_id": sid, "stu_name": item.get("student_name") or sid, "stu_ans": item.get("student_answers") or []})
    task_meta = {k: task.get(k) for k in ("needs_attention", "final_result_dirty", "analysis_status", "problem_file_name", "submission_file_name", "kb_doc_count")}
    tables["tasks"].append({**{k: task.get(k) for k in SCHEMA["tasks"]}, "task_id": tid, "result_version": task.get("final_result_version", 0), "metadata": dumps(task_meta)})
    for sid, student in all_students.items():
        sid = str(student.get("stu_id") or sid)
        item = by_student.get(sid, {})
        name = str(item.get("student_name") or student.get("stu_name") or sid)
        if student.get("stu_name") and item.get("student_name") and student["stu_name"] != item["student_name"]:
            raise AskDataError("姓名映射与成绩不一致，请刷新后重试。 / Student identity changed; reload and retry.")
        corrections = item.get("corrections") or []
        seen = set()
        student_grades = []
        for c in corrections:
            qid = str(c["q_id"])
            if qid in seen:
                raise AskDataError("同一学生同一题有重复成绩。 / Duplicate grade row.")
            seen.add(qid)
            value, maximum, conf = score(c), number(c.get("max_score")), confidence(c.get("confidence"))
            teacher = number(c.get("teacher_score"))
            experts = c.get("expert_results") or []
            expert_scores = [v for e in experts if (v := number(e.get("score"))) is not None]
            disagreement = bool(len(expert_scores) > 1 and maximum is not None and max(expert_scores) - min(expert_scores) > max(1, maximum * .25))
            review = bool(c.get("requires_human_review") or value is None or (conf is not None and conf < .65) or disagreement or set(c.get("review_reasons") or []).intersection({"high_indecisiveness", "score_spread_high"}))
            grade = {"task_id": tid, "student_id": sid, "q_id": qid, "score": value, "max_score": maximum,
                     "score_percent": value / maximum * 100 if value is not None and maximum and maximum > 0 else None,
                     "points_lost": maximum - value if value is not None and maximum is not None else None,
                     "is_incorrect": int(value < maximum - 1e-9) if value is not None and maximum is not None else None,
                     "is_scored": int(value is not None), "ai_score": number(c.get("provisional_score")), "teacher_score": teacher,
                     "confidence": conf, "comment": c.get("comment"), "teacher_comment": c.get("teacher_comment"),
                     "requires_review": int(review), "reviewed": int(teacher is not None), "review_status": c.get("review_status"),
                     "review_reasons": dumps(c.get("review_reasons") or []), "synthesis_method": c.get("synthesis_method"),
                     "metadata": dumps({k: c.get(k) for k in ("hits", "logs", "is_score", "initial_review_reasons", "reviewed_at")})}
            tables["grades"].append(grade); student_grades.append(grade)
            for i, e in enumerate(experts):
                tables["expert_results"].append({"task_id": tid, "student_id": sid, "q_id": qid, "expert_index": i, "expert_name": e.get("expert_name") or e.get("model"), "score": number(e.get("score")), "confidence": confidence(e.get("confidence")), "comment": e.get("comment"), "metadata": dumps(e)})
            for i, step in enumerate(c.get("steps") or []):
                tables["grading_steps"].append({"task_id": tid, "student_id": sid, "q_id": qid, "step_index": i, "metadata": dumps(step)})
        valid = [g for g in student_grades if g["score"] is not None]
        total = sum(g["score"] for g in valid)
        maximum = sum(g["max_score"] or 0 for g in valid)
        tables["students"].append({"task_id": tid, "student_id": sid, "student_name": name,
            "identity_status": student.get("identity_status"), "source_filename": student.get("source_filename"),
            "total_score": total if valid else None, "total_max": maximum if valid else None,
            "score_percent": total / maximum * 100 if maximum > 0 else None,
            "avg_confidence": _mean([g["confidence"] for g in student_grades]), "graded_count": len(valid), "unscored_count": len(student_grades) - len(valid),
            "low_confidence_count": sum(g["confidence"] is not None and g["confidence"] < .65 for g in student_grades),
            "review_count": sum(g["requires_review"] for g in student_grades),
            "pending_review_count": sum(g["requires_review"] and not g["reviewed"] for g in student_grades),
            "metadata": dumps({"identity_match_method": student.get("identity_match_method")})})
        answers = {str(a["q_id"]): a for a in (student.get("stu_ans") or item.get("student_answers") or [])}
        for qid in dict.fromkeys([*questions, *answers]):
            a = answers.get(qid); content = (a or {}).get("content") or ""
            state = "missing" if a is None else "reviewed" if a.get("review_status") == "confirmed" else "empty" if not content.strip() else "flagged" if a.get("flag") else "recognized"
            tables["answers"].append({"task_id": tid, "student_id": sid, "q_id": qid, "content": content, "state": state,
                "review_status": (a or {}).get("review_status"), "flags": dumps((a or {}).get("flag") or []), "metadata": dumps({"number": (a or {}).get("number"), "type": (a or {}).get("type")})})
    for index, q in enumerate(sorted(questions.values(), key=_question_order)):
        qid = str(q["q_id"]); grades = [g for g in tables["grades"] if g["q_id"] == qid]
        tables["questions"].append({**{k: q.get(k) for k in SCHEMA["questions"]}, "task_id": tid, "q_id": qid, "order_index": index,
            "avg_score": _mean([g["score"] for g in grades]), "avg_percent": _mean([g["score_percent"] for g in grades]),
            "avg_confidence": _mean([g["confidence"] for g in grades]), "response_count": len(grades), "review_count": sum(g["requires_review"] for g in grades),
            "metadata": dumps({k: q.get(k) for k in ("max_score_source", "max_score_review_status", "material_provenance", "ai_completion_provenance", "knowledge_points")})})
        for issue in q.get("preparation_issues") or []:
            tables["preparation_issues"].append({"task_id": tid, "q_id": qid, **{k: issue.get(k) for k in ("code", "field", "severity", "status", "message")}})
        for i, case in enumerate(q.get("test_cases") or []):
            tables["test_cases"].append({"task_id": tid, "q_id": qid, "case_index": i, **{k: case.get(k) for k in ("input", "expected_output", "visibility", "source")}, "metadata": dumps(case)})
    for key, doc in (task.get("kb_docs") or {}).items():
        if isinstance(doc, dict):
            tables["knowledge_documents"].append({"task_id": tid, "document_id": str(key), "name": doc.get("name") or doc.get("filename"), "content": doc.get("content") or doc.get("text"), "metadata": dumps({k: doc.get(k) for k in ("kind", "source", "char_count")})})
    for tag in task.get("tag_ids") or []:
        tables["task_tags"].append({"task_id": tid, "tag_id": str(tag), "tag_name": None})
    return tables


@dataclass
class Snapshot:
    tables: dict[str, list[dict]]
    fingerprint: str
    task_id: str | None

    @classmethod
    def from_payloads(cls, payloads: list[tuple[dict, dict]], task_id: str | None):
        tables: dict[str, list[dict]] = {name: [] for name in SCHEMA}
        for task, result in payloads:
            for name, rows in project_task(task, result).items():
                tables[name].extend(rows)
        serialized = dumps(tables).encode()
        if len(serialized) > MAX_SOURCE_BYTES or sum(map(len, tables.values())) > MAX_SOURCE_ROWS:
            raise AskDataError("数据范围过大，请限定任务或课程；没有对抽样数据计算。 / Scope too large; narrow the tasks or course. No sampled aggregate was returned.")
        return cls(tables, hashlib.sha256(serialized).hexdigest(), task_id)


def load_snapshot(task_id: str | None, owner_id: str, *, all_tasks: bool = False) -> Snapshot:
    # Authorization is performed before ANY model call, and again on every task.
    if task_id is not None:
        task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)
    ids = list(task_facade.list_tasks(owner_id=owner_id)) if all_tasks or task_id is None else [task_id]
    if len(ids) > 200:
        raise AskDataError("任务过多，请在具体任务中查询。 / Too many tasks; open a specific task.")
    from backend.services.courses import list_courses_for
    from backend.db import tag_repository
    course_names = {c.id: c.name for c in list_courses_for(owner_id, "teacher")}
    tag_names = {tag.id: tag.name for tag in tag_repository.list_tags(owner_id)}
    payloads = []
    for tid in ids:
        task = task_facade.get_task(task_id=tid, owner_id=owner_id)
        revision = task.get("workflow_revision")
        result = task_facade.task_results(task_id=tid, owner_id=owner_id)
        check = task_facade.get_task(task_id=tid, owner_id=owner_id, full=False)
        if revision != check.get("workflow_revision") or task.get("grading_job_id") != check.get("grading_job_id"):
            raise AskDataError("查询期间任务发生变化，请重试。 / Task changed during the query; retry.")
        task = {**task, "course_name": course_names.get(task.get("course_id"))}
        payloads.append((task, result))
    snapshot = Snapshot.from_payloads(payloads, task_id)
    for row in snapshot.tables["task_tags"]:
        row["tag_name"] = tag_names.get(row["tag_id"])
    snapshot.fingerprint = hashlib.sha256(dumps(snapshot.tables).encode()).hexdigest()
    return snapshot


@dataclass
class EntityMatch:
    kind: str
    text: str
    rows: list[dict]
    field: str = ""
    operator: str = "contains"
    value: str = ""


def resolve_entity(snapshot: Snapshot, kind: str, text: str, *, field: str | None = None,
                   operator: str | None = None, value: str | None = None) -> EntityMatch:
    """Execute a MODEL-CHOSEN field/operator, not a natural-language rule list.

    Matching is set-valued: no ranking, nearest-name correction or top-1 fallback.
    Literal operators preserve the complete phrase, including internal spaces.
    A pattern uses SQL LIKE semantics: _ is ONE character; % is arbitrary length.
    """
    table, key, label = {"student": ("students", "student_id", "student_name"),
                         "question": ("questions", "q_id", "number"),
                         "task": ("tasks", "task_id", "name")}[kind]
    chosen = field or label
    operator = operator or ("exact" if kind == "question" else "contains")
    if chosen not in {key, label} or operator not in {"exact", "contains", "prefix", "suffix", "pattern"}:
        raise AskDataError("对象查询字段或运算符无效。 / Invalid reference field or operator.")
    needle = normalized(text if value is None else value)
    if not needle:
        return EntityMatch(kind, text, [], chosen, operator, needle)
    # Canonical question formatting, not a classifier for arbitrary numbers.
    def canon(v):
        v = normalized(v)
        if kind == "question":
            v = re.sub(r"^(?:question\s*|q\s*|第\s*)|\s*题$", "", v)
        return v
    needle = canon(needle)
    def matches(v):
        v = canon(v)
        if operator == "exact": return v == needle
        if operator == "contains": return needle in v
        if operator == "prefix": return v.startswith(needle)
        if operator == "suffix": return v.endswith(needle)
        # Dynamic programming avoids backtracking on adversarial wildcard masks.
        tokens = []; escaped = False
        for c in needle:
            if escaped: tokens.append(("literal", c)); escaped = False
            elif c == chr(92): escaped = True
            elif c in "_%": tokens.append((c, c))
            else: tokens.append(("literal", c))
        if escaped: tokens.append(("literal", chr(92)))
        states = {0}
        for token, literal in tokens:
            if token == "%":
                states = set(range(min(states), len(v) + 1)) if states else set()
            else:
                states = {i + 1 for i in states if i < len(v) and (token == "_" or v[i] == literal)}
            if not states: return False
        return len(v) in states
    candidates = [r for r in snapshot.tables[table] if matches(r.get(chosen))]
    return EntityMatch(kind, text, [{"task_id": r["task_id"], key: r[key], label: r.get(label)}
                                   for r in candidates], chosen, operator, needle)


SAFE_FUNCTIONS = frozenset("abs avg count sum total min max round coalesce ifnull nullif lower upper trim ltrim rtrim length substr substring instr replace like glob unicode char concat concat_ws group_concat string_agg date time datetime julianday unixepoch strftime timediff typeof cast iif if sign mod pow power sqrt ceil ceiling floor exp ln log log10 log2 sin cos tan acos asin atan atan2 degrees radians row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value json json_array json_object json_extract json_type json_valid json_array_length json_quote json_group_array json_group_object json_each json_tree".split())


class QueryWorkspace:
    """Evaluate one read-only statement, with access, VM, time and output bounds."""
    def __init__(self, snapshot: Snapshot, matches: list[EntityMatch] | None = None):
        self.snapshot = snapshot
        self.connection = sqlite3.connect(":memory:", cached_statements=0)
        self.connection.row_factory = sqlite3.Row
        if hasattr(self.connection, "setconfig"):
            self.connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        self.connection.execute("PRAGMA trusted_schema=OFF")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RESULT_BYTES)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 40)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 12)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_VDBE_OP, 100_000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 500)
        self.connection.create_function("casefold", 1, normalized, deterministic=True)
        for name, columns in SCHEMA.items():
            definitions = ",".join(f'"{c}" {t}' for c, t in columns.items())
            self.connection.execute(f'CREATE TABLE "{name}" ({definitions})')
            names = list(columns)
            marks = ",".join("?" for _ in names)
            self.connection.executemany(f'INSERT INTO "{name}" VALUES ({marks})', ([row.get(c) for c in names] for row in snapshot.tables[name]))
            if "student_id" in columns:
                self.connection.execute(f'CREATE INDEX "{name}_student" ON "{name}" (task_id, student_id)')
            if "q_id" in columns:
                self.connection.execute(f'CREATE INDEX "{name}_question" ON "{name}" (task_id, q_id)')
        self.connection.execute("CREATE TABLE resolved_entities (ref TEXT, kind TEXT, task_id TEXT, entity_id TEXT, label TEXT)")
        for match in matches or []:
            id_field, label_field = {"student": ("student_id", "student_name"), "question": ("q_id", "number"), "task": ("task_id", "name")}[match.kind]
            self.connection.executemany("INSERT INTO resolved_entities VALUES (?,?,?,?,?)", ((match.text, match.kind, row["task_id"], row[id_field], row.get(label_field)) for row in match.rows))
        self.connection.commit()
        self.connection.execute("PRAGMA query_only=ON")
        self.reads: set[str] = set()
        self.connection.set_authorizer(self._authorize)

    def close(self):
        self.connection.close()

    def _authorize(self, action, first, second, db, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and (db == "main" or db is None and second == "") and first in {*SCHEMA, "resolved_entities"}:
            self.reads.add(first)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in SAFE_FUNCTIONS | {"casefold"}:
            return sqlite3.SQLITE_OK
        # JSON virtual-table reads have no external resources and expose only
        # the supplied JSON expression. No file/network/extension functions.
        if action == sqlite3.SQLITE_READ and first in {"json_each", "json_tree"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def execute(self, sql: str, parameters: dict[str, Any] | None = None, *, max_rows: int = MAX_RESULT_ROWS) -> dict:
        if not re.match(r"\s*(?:SELECT\b|WITH\b)", sql, flags=re.I) or len(sql) > 20_000:
            raise AskDataError("仅允许单条只读 SELECT 查询。 / Only one read-only SELECT is allowed.")
        if parameters and (len(parameters) > 100 or any(isinstance(v, (dict, list)) for v in parameters.values())):
            raise AskDataError("查询参数无效。 / Invalid query parameters.")
        started = time.monotonic(); steps = 0
        def progress():
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 or time.monotonic() - started > 2)
        self.connection.set_progress_handler(progress, 1000)
        self.reads.clear()
        try:
            cursor = self.connection.execute(sql, parameters or {})
            columns = [c[0] for c in cursor.description or []]
            if not columns or len(set(columns)) != len(columns):
                raise AskDataError("结果列名必须唯一，请使用别名。 / Use unique result column aliases.")
            rows = []; size = len(dumps(columns).encode())
            for source_row in cursor:
                row = list(source_row)
                if len(rows) >= max_rows:
                    raise AskDataError("结果过多，请限定范围；未返回截断结果。 / Too many rows; narrow the request. No truncated result returned.")
                if any(isinstance(v, bytes) or isinstance(v, float) and not math.isfinite(v) for v in row):
                    raise AskDataError("结果包含无效数值或二进制内容。 / Invalid result value.")
                size += len(dumps(row).encode())
                if size > MAX_RESULT_BYTES:
                    raise AskDataError("结果内容过大，请缩小查询范围。 / Result too large; narrow the query.")
                rows.append(row)
            if not self.reads.intersection(SCHEMA):
                raise AskDataError("查询必须引用真实业务数据。 / Query must read business data.")
            if any(isinstance(v, bytes) or isinstance(v, float) and not math.isfinite(v) for r in rows for v in r):
                raise AskDataError("结果包含无效数值或二进制内容。 / Invalid result value.")
            result = {"columns": columns, "rows": rows}
            if len(dumps(result).encode()) > MAX_RESULT_BYTES:
                raise AskDataError("结果内容过大，请缩小查询范围。 / Result too large; narrow the query.")
            return result
        except (sqlite3.Error, MemoryError, OverflowError) as exc:
            # SQLite messages contain schema/column information useful for repair,
            # never connection strings or raw provider errors.
            message = str(exc)[:200]
            raise AskDataError(f"只读查询未执行：{message} / Read-only query could not execute.") from exc
        finally:
            self.connection.set_progress_handler(None, 0)
