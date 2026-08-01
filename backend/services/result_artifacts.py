"""Deterministic, version-bound exports for one immutable formal result.

The service intentionally performs no LLM work. It rebuilds file bytes from a
teacher-confirmed snapshot so downloads remain auditable without storing a
second mutable copy of student results.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping


ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResultArtifactFile:
    artifact_id: str
    title: str
    title_en: str
    filename: str
    media_type: str
    content: bytes

    def metadata(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "title_en": self.title_en,
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }


def artifact_fingerprint(
    *, task_id: str, task_name: str, snapshot: Mapping[str, Any],
    generated_at: float, files: Iterable[ResultArtifactFile],
) -> str:
    """Fingerprint every identity/content input used by the artifact set."""
    source = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "task_id": task_id,
        "task_name": task_name.strip(),
        "result_version": int(snapshot.get("version") or 0),
        "result_fingerprint": str(snapshot.get("fingerprint") or ""),
        "confirmed_at": snapshot.get("created_at"),
        "generated_at": generated_at,
        "files": [item.metadata() for item in files],
    }
    return hashlib.sha256(
        json.dumps(
            source, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
    ).hexdigest()


def build_artifact_manifest(
    *,
    task_id: str,
    task_name: str,
    snapshot: Mapping[str, Any],
    generated_at: float,
) -> Dict[str, Any]:
    files = build_artifact_files(
        task_id=task_id,
        task_name=task_name,
        snapshot=snapshot,
        generated_at=generated_at,
    )
    file_metadata = [artifact.metadata() for artifact in files]
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "task_id": task_id,
        "task_name": task_name,
        "result_version": int(snapshot.get("version") or 0),
        "result_fingerprint": str(snapshot.get("fingerprint") or ""),
        "artifact_fingerprint": artifact_fingerprint(
            task_id=task_id, task_name=task_name, snapshot=snapshot,
            generated_at=generated_at, files=files,
        ),
        "confirmed_at": snapshot.get("created_at"),
        "generated_at": generated_at,
        "files": file_metadata,
    }


def build_artifact_files(
    *,
    task_id: str,
    task_name: str,
    snapshot: Mapping[str, Any],
    generated_at: float,
) -> List[ResultArtifactFile]:
    version = int(snapshot.get("version") or 0)
    payload = _mapping(snapshot.get("payload"))
    prefix = f"smartai_{_safe_name(task_id)}_v{version}"
    report = _learning_report(task_name, version, snapshot, payload, generated_at)
    answers = _published_answers(task_name, version, payload)
    latex = _published_answers_latex(task_name, version, payload)
    formal_json = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "task_id": task_id,
        "task_name": task_name,
        "result_version": version,
        "confirmed_at": snapshot.get("created_at"),
        "generated_at": generated_at,
        "fingerprint": snapshot.get("fingerprint"),
        "result": payload,
    }
    return [
        ResultArtifactFile(
            "grades_csv", "成绩表", "Grade sheet", f"{prefix}_grades.csv", "text/csv; charset=utf-8",
            _grades_csv(payload),
        ),
        ResultArtifactFile(
            "learning_report_md", "学情报告", "Learning report", f"{prefix}_learning_report.md", "text/markdown; charset=utf-8",
            report.encode("utf-8"),
        ),
        ResultArtifactFile(
            "published_answers_md", "发布版标答（Markdown）", "Published answers (Markdown)", f"{prefix}_answers.md", "text/markdown; charset=utf-8",
            answers.encode("utf-8"),
        ),
        ResultArtifactFile(
            "published_answers_tex", "发布版标答（LaTeX）", "Published answers (LaTeX)", f"{prefix}_answers.tex", "application/x-tex; charset=utf-8",
            latex.encode("utf-8"),
        ),
        ResultArtifactFile(
            "formal_result_json", "正式结果数据", "Formal result data", f"{prefix}_formal_result.json", "application/json; charset=utf-8",
            json.dumps(formal_json, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        ),
    ]


def build_artifact_bundle(files: Iterable[ResultArtifactFile], manifest: Mapping[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for artifact in files:
            _write_deterministic_zip_member(
                archive, artifact.filename, artifact.content,
            )
        _write_deterministic_zip_member(
            archive,
            "manifest.json",
            json.dumps(
                manifest, ensure_ascii=False, indent=2, default=str
            ).encode("utf-8"),
        )
    return buffer.getvalue()


def _write_deterministic_zip_member(
    archive: zipfile.ZipFile, filename: str, content: bytes,
) -> None:
    info = zipfile.ZipInfo(filename=filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


def _grades_csv(payload: Mapping[str, Any]) -> bytes:
    problems = _problems(payload)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    header = ["student_id", "student_name", "total_score", "total_max", "score_rate_percent"]
    for problem in problems:
        label = _csv_safe(_problem_label(problem))
        header.extend([f"{label}_score", f"{label}_max", f"{label}_score_rate_percent"])
    writer.writerow(header)
    for result in _results(payload):
        corrections = {_string(item.get("q_id")): item for item in _corrections(result)}
        total_score = sum(_effective_score(item) for item in corrections.values())
        total_max = sum(_number(item.get("max_score")) for item in corrections.values())
        row: List[Any] = [
            _csv_safe(_string(result.get("student_id"))),
            _csv_safe(_string(result.get("student_name"))),
            _format_number(total_score),
            _format_number(total_max),
            _format_percent(total_score, total_max),
        ]
        for problem in problems:
            correction = corrections.get(_string(problem.get("q_id")))
            if correction is None:
                row.extend(["", "", ""])
                continue
            score = _effective_score(correction)
            maximum = _number(correction.get("max_score"))
            row.extend([_format_number(score), _format_number(maximum), _format_percent(score, maximum)])
        writer.writerow(row)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _learning_report(
    task_name: str,
    version: int,
    snapshot: Mapping[str, Any],
    payload: Mapping[str, Any],
    generated_at: float,
) -> str:
    students = _results(payload)
    problems = _problems(payload)
    student_rows = []
    all_rates: List[float] = []
    for student in students:
        corrections = _corrections(student)
        score = sum(_effective_score(item) for item in corrections)
        maximum = sum(_number(item.get("max_score")) for item in corrections)
        rate = (score / maximum * 100) if maximum > 0 else None
        if rate is not None:
            all_rates.append(rate)
        student_rows.append((student, score, maximum, rate))
    mean = sum(all_rates) / len(all_rates) if all_rates else None
    median = _median(all_rates)
    pass_count = sum(rate >= 60 for rate in all_rates)
    lines = [
        f"# {task_name} — 学情报告 / Learning Report",
        "",
        f"- 正式结果版本 / Formal result: v{version}",
        f"- 确认时间 / Confirmed at: {_timestamp(snapshot.get('created_at'))}",
        f"- 报告生成时间 / Generated at: {_timestamp(generated_at)}",
        "- 说明：本报告由正式结果确定性汇总，不包含 AI 自动推断的教学结论。",
        "",
        "## 班级概况 / Class Summary",
        "",
        f"- 学生数 / Students: {len(students)}",
        f"- 题目数 / Questions: {len(problems)}",
        f"- 平均得分率 / Mean: {_percent_text(mean)}",
        f"- 中位得分率 / Median: {_percent_text(median)}",
        f"- 及格率（≥60%）/ Pass rate: {_percent_text(pass_count / len(all_rates) * 100 if all_rates else None)}",
        "",
        "## 逐题表现 / Question Performance",
        "",
        "| 题目 | 题型 | 作答数 | 平均得分率 | 必审题次 |",
        "|---|---|---:|---:|---:|",
    ]
    for problem in problems:
        q_id = _string(problem.get("q_id"))
        entries = [item for student in students for item in _corrections(student) if _string(item.get("q_id")) == q_id]
        rates = [(_effective_score(item) / _number(item.get("max_score")) * 100) for item in entries if _number(item.get("max_score")) > 0]
        review_count = sum(_needs_review(item) for item in entries)
        lines.append(f"| {_problem_label(problem)} | {_string(problem.get('type')) or '—'} | {len(entries)} | {_percent_text(sum(rates) / len(rates) if rates else None)} | {review_count} |")
    lines.extend(["", "## 学生成绩 / Student Scores", "", "| 学号 | 姓名 | 得分 | 得分率 |", "|---|---|---:|---:|"])
    for student, score, maximum, rate in student_rows:
        lines.append(f"| {_string(student.get('student_id'))} | {_escape_markdown(_string(student.get('student_name')))} | {_format_number(score)} / {_format_number(maximum)} | {_percent_text(rate)} |")
    return "\n".join(lines) + "\n"


def _published_answers(task_name: str, version: int, payload: Mapping[str, Any]) -> str:
    lines = [f"# {task_name} — 发布版标答 / Published Answers", "", f"> Formal result v{version}", ""]
    for problem in _problems(payload):
        lines.extend([
            f"## {_problem_label(problem)} · {_string(problem.get('type')) or '—'}",
            "",
            "### 题干 / Problem",
            "",
            _string(problem.get("stem")) or "（未提供 / Not provided）",
            "",
            "### 评分标准 / Rubric",
            "",
            _string(problem.get("criterion")) or "（未提供 / Not provided）",
            "",
            "### 参考答案 / Reference Answer",
            "",
            _string(problem.get("reference_answer")) or "（未提供 / Not provided）",
            "",
        ])
    return "\n".join(lines)


def _published_answers_latex(task_name: str, version: int, payload: Mapping[str, Any]) -> str:
    lines = [
        r"\documentclass[UTF8]{ctexart}",
        r"\usepackage[margin=2.2cm]{geometry}",
        r"\begin{document}",
        f"\\title{{{_latex_escape(task_name)} -- 发布版标答}}",
        r"\author{SmarTAI}",
        f"\\date{{Formal result v{version}}}",
        r"\maketitle",
    ]
    for problem in _problems(payload):
        lines.extend([
            f"\\section*{{{_latex_escape(_problem_label(problem))}}}",
            r"\subsection*{题干 / Problem}",
            _verbatim(_string(problem.get("stem")) or "Not provided"),
            r"\subsection*{评分标准 / Rubric}",
            _verbatim(_string(problem.get("criterion")) or "Not provided"),
            r"\subsection*{参考答案 / Reference Answer}",
            _verbatim(_string(problem.get("reference_answer")) or "Not provided"),
        ])
    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n"


def _results(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    value = payload.get("results")
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _corrections(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    value = result.get("corrections")
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _problems(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    value = payload.get("problem_data")
    if not isinstance(value, Mapping):
        return []
    problems = [dict(item) for item in value.values() if isinstance(item, Mapping)]
    return sorted(problems, key=lambda item: _natural_key(_problem_label(item)))


def _effective_score(correction: Mapping[str, Any]) -> float:
    teacher = correction.get("teacher_score")
    return _number(teacher) if teacher is not None else _number(correction.get("score"))


def _needs_review(correction: Mapping[str, Any]) -> bool:
    confidence = _number(correction.get("confidence"))
    if confidence > 1:
        confidence /= 100
    return bool(
        correction.get("requires_human_review")
        or (confidence > 0 and confidence < 0.65)
        or correction.get("review_reasons")
    )


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _format_percent(score: float, maximum: float) -> str:
    return _format_number(score / maximum * 100) if maximum > 0 else ""


def _percent_text(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None and math.isfinite(value) else "—"


def _median(values: List[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _problem_label(problem: Mapping[str, Any]) -> str:
    number = _string(problem.get("number"))
    return f"Q{number}" if number else (_string(problem.get("q_id")) or "Question")


def _natural_key(value: str) -> List[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _timestamp(value: Any) -> str:
    number = _number(value)
    if number <= 0:
        return "—"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _csv_safe(value: str) -> str:
    """Prevent spreadsheet formula execution in teacher-downloaded CSV files."""
    if value and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:80] or "task"


def _escape_markdown(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")


def _latex_escape(value: str) -> str:
    table = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(table.get(char, char) for char in value)


def _verbatim(value: str) -> str:
    safe = value.replace(r"\end{verbatim}", "[end verbatim]")
    return "\\begin{verbatim}\n" + safe + "\n\\end{verbatim}"
