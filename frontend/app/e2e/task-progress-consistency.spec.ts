import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const scenario of [
  { name: "initial-and-final-work", completed: 4, percent: 44, eta: 900, phase: "grading" },
  { name: "last-response-pending", completed: 216, percent: 99, eta: null, phase: "grading" },
  { name: "saving-final-results", completed: 217, percent: 99, eta: null, phase: "done" },
]) {
  test(`same grading percentage on three surfaces: ${scenario.name}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-teacher");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    let finished = false;
    const task = () => ({ task_id: "consistent-progress", name: "进度一致性测试", owner_id: "teacher",
      workflow_revision: 1, status: finished ? "graded" : "grading", problem_count: 7, student_count: 31,
      created_at: 1, updated_at: 2, tag_ids: [], kb_docs: {}, kb_doc_count: 0, grading_job_id: "run",
      progress_percent: finished ? 100 : scenario.percent, eta_seconds: finished ? 0 : scenario.eta });
    const model = (id: string, succeeded: number, failed = 0) => ({ id, label: id, total: 217,
      succeeded, failed, queued: 217 - succeeded - failed, responding: 0, retrying: 0, processing: 0,
      cancelled: 0, completion_times: [] });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204,
        headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "teacher", username: "示例教师", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher" });
      if (path === "/knowledge/activity") return json({ items: [], total: 0, active_count: 0 });
      if (path === "/tasks/") return json(url.searchParams.has("page")
        ? { items: [task()], total: 1, available_facets: { semesters: [], statuses: {}, courses: [], tags: [] } }
        : { [task().task_id]: task() });
      if (path === `/tasks/${task().task_id}`) return json(task());
      if (path.endsWith("/state")) return json({ ...task(), active_job_id: finished ? null : "run",
        active_operation: finished ? null : "grading", active_operation_status: finished ? null : "running",
        progress: { phase: scenario.phase, workflow: "grading", total_students: 31, total_questions: 7,
          completed_units: scenario.completed, saved_units: 217, fully_saved_units: 217,
          pending_finalization_units: 217 - scenario.completed, messages: [], active: [],
          model_eta_seconds: 1, grading_eta_seconds: scenario.eta,
          model_progress: scenario.completed === 4 ? [model("Gemini", 80), model("GPT", 199, 1)]
            : [model("Gemini", 217), model("GPT", 217)] } });
      if (path.endsWith("/grading/activity")) return json({ run_id: "run", events: [], cursor: 0, has_more: false });
      return json([]);
    });

    await page.goto(`/tasks/${task().task_id}/grading/progress`);
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(scenario.percent));
    if (scenario.eta === 900) await expect(page.getByText(/预计剩余.*15.*分钟/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("progress.png") });
    for (const surface of ["dashboard", "history"]) {
      await page.goto(`/${surface}`);
      const table = page.getByRole("table", { name: surface === "dashboard" ? "需要关注的任务" : "历史任务列表" });
      const row = table.getByRole("row").filter({ hasText: task().name });
      await expect(row.getByRole("cell").nth(3)).toHaveText(`${scenario.percent}%`);
      if (scenario.eta === 900) await expect(row.getByRole("cell").nth(4)).toContainText("15");
      await page.screenshot({ path: testInfo.outputPath(`${surface}.png`) });
    }
    if (scenario.completed === 4) {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto("/dashboard");
      await expect(page.getByRole("region", { name: "需要关注的任务" }).getByText(/44%/).filter({ visible: true })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("dashboard-mobile.png") });
      await page.goto(`/tasks/${task().task_id}/grading/progress`);
      await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "44");
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(false);
      await page.screenshot({ path: testInfo.outputPath("progress-mobile.png") });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    // The task's confirmed terminal state changes both lists to 100%.
    finished = true;
    for (const surface of ["dashboard", "history"]) {
      await page.goto(`/${surface}`);
      const table = page.getByRole("table", { name: surface === "dashboard" ? "需要关注的任务" : "历史任务列表" });
      await expect(table.getByRole("row").filter({ hasText: task().name }).getByRole("cell").nth(3)).toHaveText("100%");
    }
    expect(errors).toEqual([]);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  });
}
