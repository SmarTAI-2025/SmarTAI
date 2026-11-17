import { expect, test, type Page } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "completion-partial";
const progressPath = `/tasks/${taskId}/questions/ai-complete/progress/job`;
const targets = [
  { target_id: "q1:reference_answer", q_id: "q1", question_number: "1", question_type: "推理题", target: "reference_answer", label: "标答" },
  { target_id: "q1:criterion", q_id: "q1", question_number: "1", question_type: "推理题", target: "criterion", label: "评分标准" },
  { target_id: "q2:criterion", q_id: "q2", question_number: "2", question_type: "推理题", target: "criterion", label: "评分标准" },
];

async function mockCompletion(page: Page, locale: string, options: { full?: boolean; filledElsewhere?: boolean; legacy?: boolean } = {}) {
  await page.addInitScript(locale => {
    localStorage.setItem("smartai_token", "synthetic-completion-token");
    localStorage.setItem("smartai_locale", locale);
  }, locale);
  const requests: unknown[] = [];
  const task = { task_id: taskId, name: "Synthetic completion", status: "problems_ready", workflow_revision: 5,
    active_job_id: null, active_operation: null, problem_data: { q1: { q_id: "q1", number: "1", type: "推理题", stem: "Prove the claim.", max_score: 10, criterion: "", reference_answer: "Valid reference" } }, student_data: {}, kb_docs: {} };
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ json: value });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-completion-token" });
    if (path.startsWith("/experts")) return json([{ provider_id: "synthetic", enabled: true, is_default: true, model: "offline", provider_type: "gemini" }]);
    if (path.endsWith("/ai-completions/preflight")) {
      const missing = options.filledElsewhere ? targets.slice(2) : targets.slice(1);
      return json({ status: "ready", task_id: taskId, missing_targets: missing, workflow_revision: 5,
        overwrite_policy: "missing_only", summary: { question_count: missing.length, missing_count: missing.length,
          by_target: { criterion: missing.length, reference_answer: 0, solution_code: 0, test_cases: 0 } } });
    }
    if (path.endsWith("/ai-completions/confirm")) {
      requests.push(request.postDataJSON());
      return json({ status: "started", job_id: "retry", task_id: taskId, workflow_revision: 6 });
    }
    if (path.endsWith("/ai-completions/job")) return json({ job_id: "job", task_id: taskId, status: "done",
      target_ids: targets.slice(0, 2).map(t => t.target_id), targets: options.legacy ? undefined : targets.slice(0, 2),
      applied_target_ids: options.full ? targets.slice(0, 2).map(t => t.target_id) : [targets[0].target_id],
      skipped_target_ids: options.full ? [] : [targets[1].target_id], workflow_revision: 5 });
    if (path.endsWith("/ai-completions/retry")) return json({ job_id: "retry", task_id: taskId, status: "done",
      target_ids: [targets[1].target_id], applied_target_ids: [targets[1].target_id], skipped_target_ids: [], workflow_revision: 6 });
    if (path.endsWith("/state") || path === `/tasks/${taskId}`) return json(task);
    if (path.endsWith("/input")) return json({ task_id: taskId, input: null, workflow_revision: 5 });
    return json([]);
  });
  return requests;
}

for (const locale of ["zh-CN", "en-US"]) for (const width of [1440, 390]) {
  test(`partial completion and missing-only retry ${locale} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 950 });
    const errors: string[] = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    const requests = await mockCompletion(page, locale);
    const zh = locale === "zh-CN";
    await page.goto(progressPath);
    await expect(page).toHaveURL(new RegExp(`${progressPath}$`));
    await expect(page).toHaveTitle(/SmarTAI/i);
    await expect(page.getByRole("heading", { name: zh ? "部分资料已保存，仍有项目未补全" : "Some materials were saved; others were not completed" })).toBeVisible();
    await expect(page.getByText(zh ? "第 1 题 · 标答" : "Question 1 · Reference answer", { exact: true })).toBeVisible();
    await expect(page.getByText(zh ? "第 1 题 · 评分标准" : "Question 1 · Grading rubric", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    await page.screenshot({ path: info.outputPath("partial.png"), fullPage: true });
    await page.reload();
    await page.getByRole("link", { name: zh ? "重试本次未补全项" : "Retry unfinished items", exact: true }).click();
    await expect(page.getByRole("checkbox")).toHaveCount(2);
    await expect(page.getByRole("checkbox").nth(0)).toBeChecked();
    await expect(page.getByRole("checkbox").nth(1)).not.toBeChecked();
    await page.getByRole("button", { name: zh ? "确认并开始生成" : "Confirm & Generate", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}/questions$`));
    expect(requests).toEqual([{ target_ids: ["q1:criterion"], expected_workflow_revision: 5 }]);
    expect(errors).toEqual([]);
  });
}

test("retry rechecks current missing items and does not select unrelated ones", async ({ page }) => {
  const requests = await mockCompletion(page, "zh-CN", { filledElsewhere: true });
  await page.goto(progressPath);
  await page.getByRole("link", { name: "重试本次未补全项", exact: true }).click();
  await expect(page.getByRole("checkbox")).toHaveCount(1);
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(page.getByRole("button", { name: "确认并开始生成", exact: true })).toBeDisabled();
  expect(requests).toEqual([]);
});

test("fully successful completion still redirects to questions", async ({ page }) => {
  await mockCompletion(page, "zh-CN", { full: true });
  await page.goto(progressPath);
  await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}/questions$`));
});

test("older jobs without display metadata still expose unfinished targets", async ({ page }) => {
  await mockCompletion(page, "zh-CN", { legacy: true });
  await page.goto(progressPath);
  await expect(page.getByRole("heading", { name: "部分资料已保存，仍有项目未补全" })).toBeVisible();
  await expect(page.getByText("第 q1 题 · 评分标准", { exact: true })).toBeVisible();
});
