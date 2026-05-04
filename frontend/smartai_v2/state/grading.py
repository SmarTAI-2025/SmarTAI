"""Grading state — jobs, progress (SSE), results."""
from __future__ import annotations

import asyncio
import datetime
from typing import Any

import reflex as rx
from pydantic import BaseModel

from smartai_v2.api import grading as grading_api
from smartai_v2.api.client import APIError
from smartai_v2.state.auth import AuthState


class Job(BaseModel):
    """A grading job record."""
    job_id: str = ""
    job_id_short: str = ""
    job_name: str = "Unnamed Job"
    status: str = "unknown"
    job_type: str = "—"
    created_at_fmt: str = "—"
    created_at: float = 0.0


class GradingState(rx.State):
    jobs: dict[str, dict[str, Any]] = {}
    current_job_id: str = ""

    progress: dict[str, Any] = {}
    polling: bool = False
    sse_active: bool = False

    results: dict[str, Any] = {}
    history: dict[str, dict[str, Any]] = {}

    error: str = ""

    @rx.var
    def has_active_job(self) -> bool:
        return bool(self.current_job_id) and self.polling

    @rx.var
    def progress_phase(self) -> str:
        return self.progress.get("phase", "idle")

    @rx.var
    def completed_students(self) -> int:
        return int(self.progress.get("completed_students", 0))

    @rx.var
    def total_students(self) -> int:
        return int(self.progress.get("total_students", 0))

    @rx.var
    def completed_questions(self) -> int:
        return int(self.progress.get("completed_questions", 0))

    @rx.var
    def total_questions(self) -> int:
        return int(self.progress.get("total_questions", 0))

    @rx.var
    def progress_pct(self) -> int:
        total = self.total_students * max(self.total_questions // max(self.total_students, 1), 1) if self.total_students else 0
        if total == 0:
            return 0
        done = self.completed_questions
        return min(100, int(done / total * 100)) if total else 0

    @rx.var
    def current_step(self) -> str:
        return self.progress.get("current_step", "")

    @rx.var
    def progress_events(self) -> list[dict[str, Any]]:
        evs = self.progress.get("events", [])
        return list(evs) if isinstance(evs, list) else []

    @rx.var
    def job_list(self) -> list[Job]:
        out = []
        for jid, meta in self.jobs.items():
            entry_meta = dict(meta) if isinstance(meta, dict) else {}
            ts = entry_meta.get("created_at", entry_meta.get("submitted_at", 0))
            ts_float = 0.0
            try:
                ts_float = float(ts)
                created_at_fmt = (
                    datetime.datetime.fromtimestamp(ts_float).strftime("%Y-%m-%d %H:%M:%S")
                    if ts_float > 0
                    else "—"
                )
            except (ValueError, TypeError):
                created_at_fmt = str(ts)

            job_name = entry_meta.get("job_name", "")
            if not job_name:
                job_name = f"Job {created_at_fmt}"

            job_id = jid.strip('"')
            out.append(
                Job(
                    job_id=job_id,
                    job_id_short=job_id[:8],
                    job_name=job_name,
                    status=entry_meta.get("status", "unknown"),
                    job_type=entry_meta.get("job_type", entry_meta.get("type", "—")),
                    created_at_fmt=created_at_fmt,
                    created_at=ts_float,
                )
            )
        out.sort(key=lambda x: x.created_at, reverse=True)
        return out

    @rx.var
    def history_list(self) -> list[dict[str, Any]]:
        out = []
        for jid, h in self.history.items():
            entry = dict(h) if isinstance(h, dict) else {}
            entry["job_id"] = jid.strip('"')
            ts = entry.get("created_at", entry.get("submitted_at", 0))
            try:
                import datetime
                entry["created_at_fmt"] = datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if float(ts) > 0 else "—"
            except:
                entry["created_at_fmt"] = str(ts)
            if not entry.get("job_name"):
                entry["job_name"] = f"Job {entry['created_at_fmt']}"
            out.append(entry)
        out.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return out

    @rx.event
    async def start_grade_all(self):
        self.error = ""
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.grade_all(token=auth.token or None)
            jid = data.get("job_id")
            if not jid:
                self.error = data.get("message", "No job_id returned")
                return rx.toast.error(self.error)
            self.current_job_id = jid
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            self.jobs = {**self.jobs, jid: {"name": f"Batch Grade @ {now}", "status": "pending"}}
            return [
                rx.toast.success("Grading started"),
                rx.redirect(f"/grading/{jid}"),
            ]
        except APIError as e:
            self.error = e.message
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def start_grade_student(self, student_id: str):
        self.error = ""
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.grade_student(student_id, token=auth.token or None)
            jid = data.get("job_id")
            if not jid:
                self.error = data.get("message", "No job_id returned")
                return rx.toast.error(self.error)
            self.current_job_id = jid
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            self.jobs = {**self.jobs, jid: {"name": f"Grade {student_id} @ {now}", "status": "pending"}}
            return [
                rx.toast.success("Grading started"),
                rx.redirect(f"/grading/{jid}"),
            ]
        except APIError as e:
            self.error = e.message
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event(background=True)
    async def watch_progress(self, job_id: str):
        async with self:
            self.polling = True
            self.current_job_id = job_id

        try:
            auth = await self.get_state(AuthState)
            token = auth.token or None
        except Exception:
            token = None

        while True:
            try:
                snap = await grading_api.get_progress(job_id, token=token)
                async with self:
                    self.progress = snap or {}
                    if (snap or {}).get("phase") in ("done", "error"):
                        self.polling = False
                        break
            except APIError:
                async with self:
                    self.polling = False
                    self.error = "progress fetch failed"
                break
            try:
                res = await grading_api.get_result(job_id, token=token)
                if res.get("status") == "completed":
                    async with self:
                        self.results = res
                        self.progress = {**self.progress, "phase": "done"}
                        self.polling = False
                    break
            except APIError:
                pass
            await asyncio.sleep(1.5)

    @rx.event(background=True)
    async def stream_progress_sse(self, job_id: str):
        """Optional SSE-based variant. Not used by default; watch_progress polls."""
        async with self:
            self.sse_active = True
            self.current_job_id = job_id
        try:
            auth = await self.get_state(AuthState)
            token = auth.token or None
        except Exception:
            token = None
        try:
            async for ev in grading_api.stream_progress(job_id, token=token):
                async with self:
                    self.progress = ev or {}
                    if (ev or {}).get("phase") in ("done", "error"):
                        self.sse_active = False
                        break
        except Exception:
            async with self:
                self.sse_active = False

    @rx.event
    def stop_polling(self):
        self.polling = False
        self.sse_active = False

    @rx.event
    async def fetch_result(self, job_id: str):
        from smartai_v2.state.ingest import IngestState
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.get_result(job_id, token=auth.token or None)
            self.results = data
            # Also populate IngestState so student detail pages work
            ingest_state = await self.get_state(IngestState)
            if "problem_data" in data:
                ingest_state.prob_data = data["problem_data"]
            if "student_data" in data:
                ingest_state.stu_data = data["student_data"]
            return None
        except APIError as e:
            self.error = e.message
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def load_history(self):
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.all_history(token=auth.token or None)
            self.history = data if isinstance(data, dict) else {}
        except APIError as e:
            self.error = e.message

    @rx.event
    async def load_jobs(self):
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.all_job_metadata(token=auth.token or None)
            self.jobs = data if isinstance(data, dict) else {}
        except APIError as e:
            self.error = e.message

    @rx.event
    async def discard_job(self, job_id: str):
        try:
            auth = await self.get_state(AuthState)
            await grading_api.discard_job(job_id, token=auth.token or None)
            new = dict(self.jobs)
            new.pop(job_id, None)
            self.jobs = new
            return rx.toast.success("Job discarded")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def reset_all(self):
        try:
            auth = await self.get_state(AuthState)
            await grading_api.reset_all_grading(token=auth.token or None)
            self.jobs = {}
            self.progress = {}
            self.current_job_id = ""
            self.polling = False
            return rx.toast.success("All active jobs reset")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    edit_job_id: str = ""
    edit_job_name: str = ""

    @rx.event
    def begin_rename(self, job_id: str, current_name: str):
        self.edit_job_id = job_id
        self.edit_job_name = current_name

    @rx.event
    def cancel_rename(self):
        self.edit_job_id = ""

    @rx.event
    def set_edit_job_name(self, name: str):
        self.edit_job_name = name

    @rx.event
    async def save_rename(self):
        if not self.edit_job_id:
            return
        try:
            auth = await self.get_state(AuthState)
            await grading_api.rename_job(self.edit_job_id, self.edit_job_name, token=auth.token or None)
            new_jobs = dict(self.jobs)
            if self.edit_job_id in new_jobs:
                new_jobs[self.edit_job_id] = {**new_jobs[self.edit_job_id], "job_name": self.edit_job_name}
                self.jobs = new_jobs
            self.edit_job_id = ""
            return rx.toast.success("Job renamed")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")
