"""Attribute safe archive paths; an archive member is evidence, not necessarily a student."""
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
import re

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from backend.services.programming_submission_mapping import is_source_file, may_contain_source, native_source
from backend.tools.file_processing import decode_text_bytes

MULTIPLE_SOURCE_FILES = "submission_programming_multiple_files"
PROJECT_MESSAGE = ("Provide one self-contained source file per student. Identify each student in the filename "
                   "or folder (student ID and name), or use the roster. Multi-file projects require reupload.")
_MODULE_NAMES = {"main", "solve", "solution", "helper", "helpers", "util", "utils", "common", "__init__"}
_ID = re.compile(r"(?:\d{5,20}|(?:s|stu|student|id)\d{1,20})", re.I)


@dataclass(frozen=True)
class MemberOwner:
    student_id: str
    name: str


async def archive_code_paths(sources, problems):
    """Use the normal native-source boundary for code supplied as TXT/Markdown."""
    names = set()
    for member in sources:
        if member.pre_error_code:
            continue
        if is_source_file(member.filename):
            names.add(member.filename)
        elif may_contain_source(member.filename, problems):
            text = getattr(member, "text", None)
            if text is None and getattr(member, "content", None):
                try:
                    text = await decode_text_bytes(member.content)
                except HTTPException:
                    continue  # The regular reader owns its specific failure.
            if text and await run_in_threadpool(native_source, member.filename, text, problems):
                names.add(member.filename)
    return names


def archive_layout(paths, code_paths, roster, *, programming):
    """Resolve independent evidence from the leaf and its ancestors, never folder position.

    Unlabelled siblings could be a project or separate students. Neither choice
    is safe automatically. A shared class folder alone is never identity evidence.
    """
    if not programming:
        paths = [name for name in paths if name in code_paths]
    by_label = defaultdict(set)
    for row in roster or ():
        owner = MemberOwner(row["stu_id"], row.get("stu_name") or "")
        for label in (owner.student_id, owner.name):
            if label:
                by_label[label.strip().casefold()].add(owner)

    def identities(label):
        parts = re.split(r"[_\s-]+", label.strip(), maxsplit=1)
        name = re.split(r"[_\s-]+", parts[1], maxsplit=1)[0] if len(parts) == 2 else ""
        if roster:
            # Complete labels / leading ID only. A substring such as S1 in S10
            # or class-S1-2026 is insufficient to identify a student.
            whole = by_label.get(label.casefold(), set())
            ids = {v for v in by_label.get(parts[0].casefold(), ()) if v.student_id.casefold() == parts[0].casefold()}
            if ids and len(parts) == 2:
                tail = parts[1].casefold()
                named = {v for v in by_label.get(tail, ()) if v.name.casefold() == tail}
                if not named:
                    named = {v for v in by_label.get(name.casefold(), ()) if v.name.casefold() == name.casefold()}
                if named and ids.isdisjoint(named):
                    return whole | ids | named
            return whole | ids
        if _ID.fullmatch(parts[0]) and len(name) <= 160:
            return {MemberOwner(parts[0], name)}
        return set()

    owners, uncertain, by_student = {}, set(), defaultdict(list)
    for filename in paths:
        path = PurePosixPath(filename)
        folder = set().union(*(identities(part) for part in path.parts[:-1]))
        if not folder and filename not in code_paths:
            continue  # Other documents retain their normal identity extraction.
        hints = folder | identities(path.stem)
        ids = {o.student_id.casefold() for o in hints}
        names = {o.name.casefold() for o in hints if o.name}
        if len(ids) == 1 and len(names) <= 1:
            owners[filename] = min(hints, key=lambda o: (not o.name, o.student_id, o.name))
        else:
            uncertain.add(filename)
        if filename in code_paths and filename in owners:
            by_student[owners[filename].student_id.casefold()].append(filename)

    blocked = {name for names in by_student.values() if len(names) > 1 for name in names}
    # Unidentified filenames can represent separate students. Keep their labels
    # as review candidates, without certifying them as roster identities. Common
    # module names in the same directory give no basis to split a project.
    unknown_dirs, leaf_locations = defaultdict(list), defaultdict(Counter)
    for filename in paths:
        if filename not in uncertain or filename not in code_paths:
            continue
        path = PurePosixPath(filename)
        unknown_dirs[path.parent].append(path)
        leaf_locations[path.stem.casefold()].update(set(path.parent.parts))
    for directory, members in unknown_dirs.items():
        if len(members) > 1 and any(p.stem.casefold() in _MODULE_NAMES for p in members):
            blocked.update(str(path) for path in members)
        elif len(members) == 1 and directory.parts:
            path = members[0]
            # Two anonymous folders may each contain main.py. Use a distinct
            # real folder label for *review*, never as a confirmed student.
            if path.stem.casefold() in _MODULE_NAMES:
                counts = leaf_locations[path.stem.casefold()]
                label = next((part for part in reversed(directory.parts) if counts[part] == 1), None)
                if label:
                    owners[str(path)] = MemberOwner(label[:160], "")
    return owners, uncertain, blocked


def apply_member_identity(result, owner, uncertain, mode, *, native):
    if not result.student or (owner is None and not uncertain):
        return result
    student = dict(result.student)
    sid = str(student["stu_id"])
    # A conflicting identity transcribed from a document must stay visible for
    # review. A code module has no transcribed identity; its leaf may be main.py.
    old_name = str(student.get("stu_name") or "")
    conflict = (not native and owner and student.get("identity_status") == "matched"
                and (sid.casefold() != owner.student_id.casefold() or
                     (owner.name and old_name not in {"", "[Unknown Student]"} and owner.name.casefold() != old_name.casefold())))
    if owner and not conflict:
        sid = owner.student_id
        student.update(stu_id=sid, stu_name=owner.name or "[Unknown Student]")
    elif native and uncertain:
        # In particular, discard a substring-only roster guess from a class folder.
        sid = PurePosixPath(result.filename).stem[:160]
        student.update(stu_id=sid, stu_name="[Unknown Student]")
    matched = bool(owner and owner.name and mode != "manual_review" and not conflict)
    student["identity_status"] = "matched" if matched else "needs_review"
    result = replace(result, student=student, student_candidate=sid)
    if result.status not in {"parsed", "identity_conflict"}:
        return result
    return replace(result, status="parsed" if matched else "identity_conflict",
                   stable_error_code=None if matched else "identity_needs_review",
                   failure_phase=None if matched else "identity")


def reconcile_archive_results(results, mode, roster, *, programming):
    # Unsafe paths and unreadable members have a dedicated source diagnostic.
    # Their sanitized fallback leaf names cannot implicate a healthy sibling.
    usable = [r for r in results if r.failure_phase not in {"source_read", "source_persistence", "archive"}]
    code_paths = {r.filename for r in usable if is_source_file(r.filename)
                  or ((r.student or {}).get("recognition") or {}).get("native_source_mapping_version")}
    owners, uncertain, blocked = archive_layout([r.filename for r in usable], code_paths, roster, programming=programming)
    resolved = []
    for result in results:
        if result.filename in blocked:
            if result.status != "parse_failed":
                result = replace(result, status="parse_failed", stable_error_code=MULTIPLE_SOURCE_FILES,
                                 failure_phase="archive", retryable=False)
            resolved.append(replace(result, student=None, student_candidate=None, matched_answer_count=0))
        else:
            resolved.append(apply_member_identity(result, owners.get(result.filename),
                result.filename in uncertain, mode, native=result.filename in code_paths))
    return resolved
