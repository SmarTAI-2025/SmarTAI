"""Ingest state — uploaded problems & student answers."""
from __future__ import annotations

from typing import Any

import reflex as rx

from smartai_v2.api import ingest as ingest_api
from smartai_v2.api import human_edit as human_edit_api
from smartai_v2.api.client import APIError
from smartai_v2.state.auth import AuthState


class IngestState(rx.State):
    prob_data: dict[str, dict[str, Any]] = {}
    stu_data: dict[str, dict[str, Any]] = {}

    upload_status: str = "idle"
    upload_message: str = ""
    upload_progress: int = 0

    last_problem_filename: str = ""
    last_homework_filename: str = ""

    edit_q_id: str = ""
    edit_field: str = ""
    edit_value: str = ""

    @rx.var
    def has_problems(self) -> bool:
        return len(self.prob_data) > 0

    @rx.var
    def has_students(self) -> bool:
        return len(self.stu_data) > 0

    @rx.var
    def problem_count(self) -> int:
        return len(self.prob_data)

    @rx.var
    def student_count(self) -> int:
        return len(self.stu_data)

    @rx.var
    def problem_list(self) -> list[dict[str, Any]]:
        from smartai_v2.config import TYPE_EN_TO_CN
        out = []
        for q_id, q in self.prob_data.items():
            entry = dict(q)
            entry.setdefault("q_id", q_id)
            # Map type to CN if it's in English
            t = entry.get("type", "")
            entry["type"] = TYPE_EN_TO_CN.get(t, t)
            out.append(entry)
        out.sort(key=lambda x: x.get("number", x.get("q_id", "")))
        return out

    @rx.var
    def student_list(self) -> list[dict[str, Any]]:
        out = []
        for s_id, s in self.stu_data.items():
            entry = dict(s)
            entry.setdefault("stu_id", s_id)
            entry["name"] = s.get("stu_name", s.get("name", "Unknown"))
            # handle both backend formats (dict or list)
            ans = entry.get("stu_ans", entry.get("answers", {}))
            entry["answer_count"] = len(ans)
            out.append(entry)
        out.sort(key=lambda x: x.get("stu_id", ""))
        return out

    @rx.event
    async def handle_problem_upload(self, files: list[rx.UploadFile]):
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        self.upload_status = "uploading"
        self.upload_message = f"Uploading {f.name}..."
        self.last_problem_filename = f.name
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await ingest_api.upload_problem_file(
                f.name, content, f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            self.prob_data = data if isinstance(data, dict) else {}
            self.upload_status = "done"
            self.upload_message = f"Extracted {len(self.prob_data)} problems"
            return [
                rx.toast.success(f"Extracted {len(self.prob_data)} problems"),
                rx.redirect("/problems"),
            ]
        except APIError as e:
            self.upload_status = "error"
            self.upload_message = e.message
            return rx.toast.error(f"Upload failed: {e.message}")
        except Exception as e:
            self.upload_status = "error"
            self.upload_message = str(e)
            return rx.toast.error(f"Upload failed: {e}")

    @rx.event
    async def handle_homework_upload(self, files: list[rx.UploadFile]):
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        self.upload_status = "uploading"
        self.upload_message = f"Uploading {f.name}..."
        self.last_homework_filename = f.name
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await ingest_api.upload_homework_archive(
                f.name, content, f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            self.stu_data = data if isinstance(data, dict) else {}
            self.upload_status = "done"
            self.upload_message = f"Parsed {len(self.stu_data)} student submissions"
            return [
                rx.toast.success(f"Parsed {len(self.stu_data)} students"),
                rx.redirect("/students"),
            ]
        except APIError as e:
            self.upload_status = "error"
            self.upload_message = e.message
            return rx.toast.error(f"Upload failed: {e.message}")
        except Exception as e:
            self.upload_status = "error"
            self.upload_message = str(e)
            return rx.toast.error(f"Upload failed: {e}")

    @rx.event
    def begin_edit(self, q_id: str, field: str):
        self.edit_q_id = q_id
        self.edit_field = field
        q = self.prob_data.get(q_id, {})
        self.edit_value = str(q.get(field, ""))

    @rx.event
    def cancel_edit(self):
        self.edit_q_id = ""
        self.edit_field = ""
        self.edit_value = ""

    @rx.event
    def set_edit_value(self, v: str):
        self.edit_value = v

    @rx.event
    async def save_edit(self):
        if not self.edit_q_id or not self.edit_field:
            return
        new = dict(self.prob_data)
        if self.edit_q_id in new:
            new[self.edit_q_id] = {**new[self.edit_q_id], self.edit_field: self.edit_value}
        self.prob_data = new
        try:
            auth = await self.get_state(AuthState)
            await human_edit_api.update_problems(self.prob_data, token=auth.token or None)
        except Exception:
            pass
        self.cancel_edit()
        return rx.toast.success("Saved")

    @rx.event
    async def update_question_type(self, q_id: str, new_type: str):
        if q_id not in self.prob_data:
            return
        new = dict(self.prob_data)
        new[q_id] = {**new[q_id], "type": new_type}
        self.prob_data = new
        try:
            auth = await self.get_state(AuthState)
            await human_edit_api.update_problems(self.prob_data, token=auth.token or None)
        except Exception:
            pass

    @rx.event
    async def push_student_edit(self, stu_id: str, q_id: str, value: str):
        if stu_id not in self.stu_data:
            return
        new = dict(self.stu_data)
        ans = dict(new[stu_id].get("answers", {}))
        ans[q_id] = value
        new[stu_id] = {**new[stu_id], "answers": ans}
        self.stu_data = new
        try:
            auth = await self.get_state(AuthState)
            await human_edit_api.update_student_answers(self.stu_data, token=auth.token or None)
        except Exception:
            pass

    @rx.event
    def clear_all(self):
        self.prob_data = {}
        self.stu_data = {}
        self.upload_status = "idle"
        self.upload_message = ""
