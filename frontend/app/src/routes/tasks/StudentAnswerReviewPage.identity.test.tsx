import "fake-indexeddb/auto";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { taskKeys } from "@/api/hooks/keys";
import { DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { PageDraftSession } from "@/hooks/useDraftProtection";
import { clearPageDrafts, readPageDraft, objectDraftCodec } from "@/lib/pageDraftStore";
import * as pageDraftStore from "@/lib/pageDraftStore";
import type { Task } from "@/types";
import { StudentAnswerReviewPage } from "./StudentAnswerReviewPage";

const api = vi.hoisted(() => ({ get: vi.fn(), identity: vi.fn() }));
vi.mock("@/api/tasks", () => ({ getTask: api.get, updateStudentIdentity: api.identity }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/hooks/useSourcePreview", () => ({ useSourcePreview: () => ({}) }));
vi.mock("@/i18n/I18nProvider", async () => {
  const { messages } = await vi.importActual<typeof import("@/i18n/messages")>("@/i18n/messages");
  return { useI18n: () => ({ locale: "zh-CN", t: (key: keyof typeof messages["zh-CN"]) => messages["zh-CN"][key] }) };
});

let task: Task;
const disposals: Array<() => void> = [];
afterEach(() => { cleanup(); disposals.splice(0).forEach(dispose => dispose()); vi.restoreAllMocks(); });
beforeEach(async () => {
  await clearPageDrafts();
  task = {
    task_id: "identity-task", owner_id: "teacher-identity", name: "Identity review", status: "submissions_ready",
    workflow_revision: 3, problem_count: 1, student_count: 1, created_at: 1, updated_at: 1,
    kb_docs: {}, kb_doc_count: 0,
    problem_data: { Q1: { q_id: "Q1", number: "1", type: "Calculation", stem: "Evaluate.", criterion: "Correct result.", max_score: 10, review_status: "confirmed" } },
    student_data: { OLD: { stu_id: "OLD", stu_name: "[Unknown Student]", identity_status: "needs_review", source_id: "source-1",
      stu_ans: [{ q_id: "Q1", number: "1", type: "Calculation", content: "Original answer", review_status: "pending", flag: [] }] } },
  };
  api.get.mockReset().mockImplementation(async () => task);
  api.identity.mockReset().mockImplementation(async (_taskId, oldId, body) => {
    const student = { ...task.student_data[oldId], stu_id: body.student_id, stu_name: body.student_name, identity_status: "matched" as const };
    const students = { ...task.student_data }; delete students[oldId]; students[student.stu_id] = student;
    task = { ...task, student_data: students, workflow_revision: task.workflow_revision + 1 };
    return { status: "ok", previous_student_id: oldId, student, workflow_revision: task.workflow_revision };
  });
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
});

function openPage(search = "?identity=edit&question=Q1&from=matrix") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(taskKeys.detail(task.task_id), task);
  const router = createMemoryRouter([{
    element: <QueryClientProvider client={client}><PageDraftSession ownerId="teacher-identity"><DraftLeaveProvider><Outlet /></DraftLeaveProvider></PageDraftSession></QueryClientProvider>,
    children: [
      { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
      { path: "/elsewhere", element: <div>Another page</div> },
    ],
  }], { initialEntries: [`/tasks/identity-task/students/OLD${search}`] });
  render(<RouterProvider router={router} />);
  disposals.push(() => { router.dispose(); client.clear(); });
  return { router, client, user: userEvent.setup() };
}

describe("identity confirmation with the real query cache and draft session", () => {
  it("explains a nonempty unrecognized name instead of claiming it is blank", async () => {
    const { user, router } = openPage();
    await user.clear(await screen.findByRole("textbox", { name: "学号" }));
    await user.type(screen.getByRole("textbox", { name: "学号" }), "NEW");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    expect(screen.getByRole("alert")).toHaveTextContent("姓名仍为系统默认的「[Unknown Student]」，请修改后再确认");
    expect(screen.getByRole("alert")).not.toHaveTextContent("不能为空");
    expect(api.identity).not.toHaveBeenCalled();
    await user.clear(screen.getByRole("textbox", { name: "姓名" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "姓名" }), "Unknown Student");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks/identity-task/students/NEW"));
    await waitFor(() => expect(screen.getByText("Original answer")).toBeInTheDocument());
    expect(api.identity).toHaveBeenCalledTimes(1);
  });

  it("keeps the detail alive through cache rekey and asynchronous draft cleanup, then replaces the URL", async () => {
    const { user, router, client } = openPage();
    const missingStates: string[] = [];
    const observer = new MutationObserver(() => {
      if (document.body.textContent?.includes("当前链接找不到对应的学生记录")) missingStates.push(document.body.textContent);
    });
    observer.observe(document.body, { subtree: true, childList: true });
    disposals.push(() => observer.disconnect());
    await user.clear(await screen.findByRole("textbox", { name: "学号" }));
    await user.type(screen.getByRole("textbox", { name: "学号" }), "NEW");
    await user.clear(screen.getByRole("textbox", { name: "姓名" }));
    await user.type(screen.getByRole("textbox", { name: "姓名" }), "Lin");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks/identity-task/students/NEW"));
    expect(router.state.historyAction).toBe("REPLACE");
    expect(router.state.location.search).toBe("?question=Q1&from=matrix");
    // The router URL can update before React commits the newly keyed form.
    await screen.findByRole("region", { name: "NEW · 全部题目作答" });
    await waitFor(() => expect(screen.getByText("Original answer")).toBeInTheDocument());
    expect(screen.queryByText("当前链接找不到对应的学生记录")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const saved = client.getQueryData<Task>(taskKeys.detail(task.task_id))!;
    expect(Object.keys(saved.student_data)).toEqual(["NEW"]);
    expect(saved.student_data.NEW.stu_ans).toEqual(task.student_data.NEW.stu_ans);
    const draft = await readPageDraft("teacher-identity", "identity:identity-task:OLD", objectDraftCodec({ identityId: "", identityName: "" }));
    expect(draft.value).toBeNull();
    await user.click(screen.getByRole("button", { name: "修正身份" }));
    await user.clear(screen.getByRole("textbox", { name: "姓名" }));
    await user.type(screen.getByRole("textbox", { name: "姓名" }), "Lin Updated");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "姓名" })).not.toBeInTheDocument());
    expect(api.identity).toHaveBeenLastCalledWith("identity-task", "NEW", expect.objectContaining({ student_name: "Lin Updated", expected_workflow_revision: 4 }));
    expect(missingStates).toEqual([]);
  });

  it.each(["学号", "姓名"])("identifies the empty %s field and keeps the editor", async field => {
    task.student_data.OLD.stu_name = "Lin";
    const { user } = openPage();
    await user.clear(await screen.findByRole("textbox", { name: field }));
    await user.type(screen.getByRole("textbox", { name: field }), "   ");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    expect(screen.getByRole("alert")).toHaveTextContent(`请填写${field}后再确认`);
    expect(api.identity).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps response editing locked until identity draft cleanup and navigation finish (editor open: %s)", async (editing) => {
    task.student_data.OLD.stu_name = "Lin";
    const removeDraft = pageDraftStore.removePageDraft;
    let finishCleanup!: () => void;
    const cleanupPending = new Promise<void>(resolve => { finishCleanup = resolve; });
    const removal = vi.spyOn(pageDraftStore, "removePageDraft").mockImplementationOnce(async (...args) => {
      await cleanupPending;
      return removeDraft(...args);
    });
    const { user, router } = openPage();
    await user.clear(await screen.findByRole("textbox", { name: "学号" }));
    await user.type(screen.getByRole("textbox", { name: "学号" }), "NEW");
    if (editing) await user.click(screen.getByRole("button", { name: "修改" }));
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    try {
      await waitFor(() => expect(removal).toHaveBeenCalled());
      expect(router.state.location.pathname).toContain("/OLD");
      expect(screen.getByRole("textbox", { name: "学号" })).toBeDisabled();
      expect(screen.queryByRole("button", { name: "修改" })).not.toBeInTheDocument();
      if (editing) {
        expect(document.querySelector("textarea")).toBeDisabled();
        expect(document.querySelector("textarea")).toHaveValue("Original answer");
      } else expect(screen.getByText("Original answer")).toBeInTheDocument();
    } finally { await act(async () => finishCleanup()); }
    await waitFor(() => expect(router.state.location.pathname).toContain("/NEW"));
    expect(await screen.findByRole("button", { name: "修改" })).toBeEnabled();
  });

  it("does not lose an unsaved response when changing the identity would remount the detail", async () => {
    task.student_data.OLD.stu_name = "Lin";
    const { user, router } = openPage();
    await user.click(screen.getByRole("button", { name: "修改" }));
    await user.type(document.querySelector("textarea")!, " edited");
    await user.type(screen.getByRole("textbox", { name: "学号" }), "NEW");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    expect(api.identity).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("请先保存正在修改的作答，再保存身份");
    expect(document.querySelector("textarea")).toHaveValue("Original answer edited");
    expect(router.state.location.pathname).toContain("/OLD");
  });

  it.each([
    ["student_identity_conflict", 422, "另一名学生"],
    ["workflow_busy", 409, "正在处理"],
    ["stale_revision", 409, "内容已更新"],
    ["student_not_found", 404, "找不到原学生记录"],
    ["", 0, "未收到保存结果"],
  ])("gives actionable %s feedback without losing input", async (code, status, message) => {
    task.student_data.OLD.stu_name = "Lin";
    api.identity.mockRejectedValueOnce(new APIError(status, "unsafe internal detail", { detail: { code } }));
    const { user, router } = openPage();
    await user.type(await screen.findByRole("textbox", { name: "学号" }), "NEW");
    await user.click(screen.getByRole("button", { name: "保存并确认身份" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("alert")).not.toHaveTextContent("unsafe internal detail");
    expect(screen.getByRole("textbox", { name: "学号" })).toHaveValue("OLDNEW");
    expect(router.state.location.pathname).toContain("/OLD");
    expect(api.identity).toHaveBeenCalledTimes(1);
  });

  it("opens and retains error feedback when confirming an unchanged identity outside the editor", async () => {
    task.student_data.OLD.stu_name = "Lin";
    api.identity.mockRejectedValueOnce(new APIError(0, "Network Error"));
    const { user } = openPage("?question=Q1");
    await user.click(await screen.findByRole("button", { name: "确认身份 · 待确认" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未收到保存结果");
    expect(screen.getByRole("textbox", { name: "姓名" })).toHaveValue("Lin");
    expect(screen.getByRole("button", { name: "确认身份" })).toBeEnabled();
  });

  it("does not pull a different page back when an identity request finishes late", async () => {
    task.student_data.OLD.stu_name = "Lin";
    let finish!: (value: unknown) => void;
    api.identity.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { user, router } = openPage();
    await user.click(await screen.findByRole("button", { name: "确认身份" }));
    await act(async () => { await clearPageDrafts(); await router.navigate("/elsewhere"); });
    await act(async () => { finish({ previous_student_id: "OLD", student: { ...task.student_data.OLD, stu_id: "LATE" }, workflow_revision: 4 }); });
    expect(router.state.location.pathname).toBe("/elsewhere");
    expect(screen.getByText("Another page")).toBeInTheDocument();
  });
});
