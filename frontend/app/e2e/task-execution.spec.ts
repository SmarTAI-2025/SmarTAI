import { test, expect, type Page } from "@playwright/test";

async function fixture(page: Page, reason: "user_busy" | "server_busy" | "rate_limited") {
  const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const task = { task_id: "execution-task", name: "排队与停止验证", status: "parsing_submissions", workflow_revision: 7,
    active_job_id: "run-1" as string | null, active_operation: "submission_recognition", active_operation_status: reason === "rate_limited" ? "running" : "pending",
    queue_reason: reason, error: null as string | null, last_failed_job_id: null as string | null,
    parse_job_id: "run-1", extract_job_id: "prepared", problem_count: 1, student_count: 0,
    submission_recognition_provider_id: "saved-provider", question_recognition_provider_id: "saved-provider",
    problem_data: {}, student_data: {}, submission_sources: [], submission_source_summary: null,
    created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-execution-token"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-execution-token" });
    if (path.startsWith("/experts")) return json([{ provider_id: "saved-provider", provider_type: "openai", model: "synthetic-model", enabled: true, is_default: true, is_shared: false, scope: "owner", editable: true, max_concurrent: 1, rpm: 10 }]);
    if (request.method() === "POST") {
      calls.push({ path, body: request.postDataJSON() });
      if (path.endsWith("/stop")) {
        task.status = "error"; task.error = "operation_cancelled";
        task.last_failed_job_id = task.active_job_id; task.active_job_id = null;
        task.workflow_revision += 1;
        return json({ status: "stopped", job_id: "run-1" });
      }
      if (path.endsWith("/retry")) {
        task.status = "parsing_submissions"; task.error = null; task.active_job_id = "run-2";
        task.active_operation_status = "pending"; task.queue_reason = "server_busy"; task.workflow_revision += 1;
        return json({ status: "started", task_id: task.task_id, job_id: "run-2" });
      }
    }
    if (path === `/tasks/${task.task_id}/state`) return json({ ...task, progress: { job_id: task.active_job_id,
      phase: task.error ? "error" : "parsing", current_step: "preparing_files", total_steps: 4, completed_steps: 0,
      total_students: 0, total_questions: 1, completed_units: 0, active: [], messages: [],
      model_waits: reason === "rate_limited" && task.active_operation_status === "running"
        ? [{ model: "synthetic-model", reason: "provider_rate_limited", attempt: 2, max_attempts: 3, retry_at: Date.now() / 1000 + 65 }] : [] } });
    if (path === `/tasks/${task.task_id}`) return json(task);
    return json({ items: [], total: 0 });
  });
  return { calls, errors };
}

for (const reason of ["user_busy", "server_busy", "rate_limited"] as const) {
  test(`visible ${reason} status and stop/continue with saved settings`, async ({ page }) => {
    await page.setViewportSize(reason === "user_busy" ? { width: 390, height: 844 } : { width: 1280, height: 900 });
    const { calls, errors } = await fixture(page, reason);
    await page.goto("/tasks/execution-task/submissions/progress");
    await expect(page.getByText(reason === "user_busy" ? /你已有任务正在处理/ : reason === "server_busy" ? /服务器繁忙/ : "服务商限流，等待重试", { exact: reason === "rate_limited" })).toBeVisible();
    if (reason === "rate_limited") await expect(page.getByText(/已尝试 1\/3 次/)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `output/playwright/execution-${reason}.png`, fullPage: true });
    await page.getByRole("button", { name: "停止运行", exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "停止运行", exact: true }).click();
    await expect(page.getByText("本次运行已停止", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "继续处理", exact: true }).click();
    await expect(page.getByText(/服务器繁忙，本任务已自动排队/)).toBeVisible();
    expect(calls).toHaveLength(2);
    expect(calls[0].body).toEqual({ job_id: "run-1", expected_workflow_revision: 7 });
    expect(calls[1].body).toMatchObject({ recognition_provider_id: "saved-provider", expected_workflow_revision: 8, acknowledge_possible_duplicate_call: true });
    expect(errors).toEqual([]);
  });
}
