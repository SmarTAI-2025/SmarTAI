import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskExecutionControls } from "./TaskExecutionControls";
import { stopTaskRun } from "@/api/tasks";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import type { JobProgress, TaskStateSnapshot } from "@/types";

vi.mock("@/api/tasks", () => ({ stopTaskRun: vi.fn() }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));
const state = { active_job_id: "job-1", workflow_revision: 7, active_operation_status: "pending" } as TaskStateSnapshot;
const refresh = vi.fn().mockResolvedValue(undefined);

describe("Task execution controls", () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => vi.useRealTimers());
  it.each([ ["user_busy", "你已有任务正在处理"], ["server_busy", "服务器繁忙"] ] as const)("explains %s and logout survival", (reason, message) => {
    render(<TaskExecutionControls taskId="task" state={{ ...state, queue_reason: reason }} onChanged={refresh} />);
    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.getByRole("status")).toHaveTextContent("退出登录");
  });
  it("counts down explicit rate-limit retries", () => {
    vi.useFakeTimers(); vi.setSystemTime(100000);
    const progress = { model_waits: [{ model: "Model", reason: "provider_rate_limited", attempt: 2, max_attempts: 3, retry_at: 165 }] } as JobProgress;
    render(<TaskExecutionControls taskId="task" state={{ ...state, active_operation_status: "running" }} progress={progress} onChanged={refresh} />);
    expect(screen.getByRole("status")).toHaveTextContent("已尝试 1/3 次，65 秒后重试");
    act(() => vi.advanceTimersByTime(65000));
    expect(screen.getByRole("status")).toHaveTextContent("正在进行第 2/3 次尝试");
  });
  it("requires an explicit stop and sends the exact displayed run only once", async () => {
    let finish!: () => void;
    vi.mocked(stopTaskRun).mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ status: "stopped", job_id: "job-1" }); }));
    render(<TaskExecutionControls taskId="task" state={state} onChanged={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "停止运行" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("保留配置");
    expect(stopTaskRun).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "停止运行" }));
    expect(stopTaskRun).toHaveBeenCalledExactlyOnceWith("task", "job-1", 7);
    await act(async () => finish());
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
  it("lets the user continue with saved settings and a fresh queue position", () => {
    const info = classifyRecoverableError("operation_cancelled", { locale: "zh-CN" });
    expect(info.actionLabel).toBe("继续处理");
    expect(info.description).toContain("原配置");
    expect(info.description).toContain("新的提交时间");
  });
});
