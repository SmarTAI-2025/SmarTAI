import { test, expect, type Page } from "@playwright/test";

const stages = {
  problems: { active: "extracting_problems", paused: "problems_paused", operation: "question_preparation", label: "题目识别暂停", action: "继续识别" },
  submissions: { active: "parsing_submissions", paused: "submissions_paused", operation: "submission_recognition", label: "作答识别暂停", action: "继续识别" },
  grading: { active: "grading", paused: "grading_paused", operation: "grading", label: "批改暂停", action: "继续批改" },
} as const;

async function fixture(page: Page, reason: "user_busy" | "server_busy" | "rate_limited", stage: keyof typeof stages) {
  const spec = stages[stage];
  const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const task = { task_id: "execution-task", owner_id: "teacher", name: "排队与停止验证", status: spec.active as string, workflow_revision: 7,
    active_job_id: "run-1" as string | null, active_operation: spec.operation as string | null, active_operation_status: (reason === "rate_limited" ? "running" : "pending") as string | null,
    queue_reason: reason, error: null as string | null, last_failed_job_id: null as string | null,
    parse_job_id: stage === "submissions" ? "run-1" : null, extract_job_id: stage === "problems" ? "run-1" : "prepared",
    grading_job_id: stage === "grading" ? "run-1" : null, problem_count: 2, student_count: stage === "grading" ? 2 : 0,
    submission_recognition_provider_id: "saved-provider", question_recognition_provider_id: "saved-provider",
    problem_data: {}, student_data: {}, submission_sources: [], submission_source_summary: null,
    created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-execution-token"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-execution-token" });
    if (path.startsWith("/experts")) return json([{ provider_id: "saved-provider", provider_type: "openai", model: "synthetic-model", enabled: true, is_default: true, is_shared: false, scope: "owner", editable: true, max_concurrent: 1, rpm: 10 }]);
    if (path === "/tags/") return json([]);
    if (path === "/course-materials/") return json({ items: [] });
    if (path === "/knowledge/activity") return json({ items: [], active_count: 0, total: 0, page: 1, page_size: 5 });
    if (path === "/tasks/") return json(url.searchParams.has("page")
      ? { items: [task], total: 1, page: 1, page_size: 25, available_facets: { semesters: [], courses: [], tags: [], statuses: {} } }
      : { [task.task_id]: task });
    if (request.method() === "POST") {
      calls.push({ path, body: request.postDataJSON() });
      if (path.endsWith("/stop")) {
        task.status = spec.paused; task.error = null;
        task.last_failed_job_id = task.active_job_id; task.active_job_id = null;
        task.active_operation = null; task.active_operation_status = null;
        task.workflow_revision += 1;
        return json({ status: "stopped", job_id: "run-1" });
      }
      if (path.endsWith("/retry") || path.endsWith("/grade")) {
        task.status = spec.active; task.error = null; task.active_job_id = "run-2"; task.active_operation = spec.operation;
        if (stage === "grading") task.grading_job_id = "run-2";
        task.active_operation_status = "pending"; task.queue_reason = "server_busy"; task.workflow_revision += 1;
        return json({ status: "started", task_id: task.task_id, job_id: "run-2" });
      }
    }
    if (path === `/tasks/${task.task_id}/state`) return json({ ...task, progress: { job_id: task.active_job_id ?? task.last_failed_job_id,
      phase: task.status === spec.paused ? "paused" : stage === "grading" ? "grading" : "parsing", current_step: "preparing_files", total_steps: 4, completed_steps: 1,
      total_students: task.student_count, total_questions: 2, completed_units: 1, active: [], messages: [], error_detail: null,
      model_waits: reason === "rate_limited" && task.active_operation_status === "running"
        ? [{ model: "synthetic-model", reason: "provider_rate_limited", attempt: 2, max_attempts: 3, retry_at: Date.now() / 1000 + 65 }] : [] } });
    if (path === `/tasks/${task.task_id}`) return json(task);
    return json({ items: [], total: 0 });
  });
  return { calls, errors };
}

for (const stage of ["problems", "submissions", "grading"] as const) for (const reason of ["user_busy", "server_busy", "rate_limited"] as const) {
  test(`${stage}: visible ${reason} status and stop/continue with saved settings`, async ({ page }, testInfo) => {
    const spec = stages[stage];
    await page.setViewportSize(reason === "user_busy" ? { width: 390, height: 844 } : { width: 1280, height: 900 });
    const { calls, errors } = await fixture(page, reason, stage);
    const progressHref = `/tasks/execution-task/${stage}/progress`;
    await page.goto(progressHref);
    await expect(page.getByText(reason === "user_busy" ? /你已有任务正在处理/ : reason === "server_busy" ? /服务器繁忙/ : stage === "grading" ? "服务商限流，等待重试" : /1 个请求等待重试 · 请求受限 · 第 2\/3 次尝试/, { exact: reason === "rate_limited" && stage === "grading" })).toBeVisible();
    if (reason === "rate_limited" && stage === "grading") await expect(page.getByText(/已尝试 1\/3 次/)).toBeVisible();
    if (stage !== "grading") await expect(page.getByRole("heading", { name: "最近动态" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("running.png"), fullPage: true });
    await page.getByRole("button", { name: "停止运行", exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "停止运行", exact: true }).click();
    await expect(page.getByRole("heading", { name: spec.label, exact: true })).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.getByText(/需要处理|异常|识别未完成|本次运行已停止/)).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("paused.png"), fullPage: true });
    for (const surface of ["dashboard", "history"]) {
      await page.goto(`/${surface}`);
      await expect(page.getByText(spec.label, { exact: true }).first()).toBeVisible();
      await expect(page.getByText("需要处理", { exact: true })).toHaveCount(0);
      await expect(page.locator(`a[href="${progressHref}"]:visible`).filter({ hasText: spec.action }).first()).toBeVisible();
      await page.locator(`a[href="${progressHref}"]:visible`).filter({ hasText: spec.label }).first().click();
      await expect(page.getByRole("heading", { name: spec.label, exact: true })).toBeVisible();
    }
    await page.getByRole("button", { name: spec.action, exact: true }).click();
    await expect(page.getByText(/服务器繁忙，本任务已自动排队/)).toBeVisible();
    expect(calls).toHaveLength(2);
    expect(calls[0].body).toEqual({ job_id: "run-1", expected_workflow_revision: 7 });
    expect(calls[1].body).toMatchObject(stage === "grading"
      ? { expected_workflow_revision: 8, retry_scope: "failed_only", request_id: expect.any(String) }
      : { recognition_provider_id: "saved-provider", expected_workflow_revision: 8, acknowledge_possible_duplicate_call: true });
    expect(errors).toEqual([]);
  });
}
