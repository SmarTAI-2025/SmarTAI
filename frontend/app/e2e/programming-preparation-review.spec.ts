import { expect, test } from "@playwright/test";

// Saved-response browser regression, not a live model acceptance test.
for (const required of [false, true]) for (const width of [1440, 390]) test(`missing ${required ? "required" : "optional"} tests agree across Demo, matrix, queue and detail (${width}px)`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 1000 });
  const errors: string[] = [], writes: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.addInitScript(() => {
    localStorage.setItem("smartai_locale", "zh-CN");
    sessionStorage.setItem("smartai_frontier_token", "synthetic-guest-token");
    sessionStorage.setItem("smartai_frontier_owner", "guest-owner");
    sessionStorage.setItem("smartai_frontier_generation", "1");
    sessionStorage.setItem("smartai_frontier_tasks", '["asg_browser_demo"]');
  });
  const task = { task_id: "asg_browser_demo", name: "Preparation review regression", status: "problems_ready",
    workflow_revision: 2, problem_count: 4, student_count: 0, error: null, active_job_id: null,
    created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0, student_data: {},
    problem_data: Object.fromEntries([1, 2, 3, 4].map(n => [`q${n}`, {
      q_id: `q${n}`, number: `Q${n}`, type: n === 4 ? "编程题" : "概念题", stem: `Question ${n}`,
      criterion: "Correct answer earns full credit.", reference_answer: "Supplied answer.",
      max_score: 10, max_score_review_status: "confirmed", review_status: "needs_review",
      test_cases: [], preparation_issues: n === 4 && required ? [{ issue_id: "required-tests:q4", q_id: "q4",
        field: "programming_tests", code: "generation_failed", severity: "blocking", status: "open",
        details: { reason: "required_programming_tests_missing" } }] : [],
    }])),
  };
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (data: unknown) => route.fulfill({ json: data });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (request.method() !== "GET") writes.push(path);
    if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
    if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json({ ...task, progress: { phase: "done", messages: [] } });
    if (path.endsWith("/source-files")) return json({ problem_source: null, submission_sources: {} });
    return json([]);
  });
  await page.goto(`/frontier/live?taskId=${task.task_id}`);
  if (required) {
    await expect(page.getByRole("button", { name: "继续识别作答", exact: true })).toHaveCount(0);
    await expect(page.getByRole("alert").getByText("请审核并修正题目中的问题，再继续识别作答。", { exact: true })).toBeVisible();
  } else await expect(page.getByRole("button", { name: "继续识别作答", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "人工继续处理", exact: true })).toHaveCount(0);
  await page.goto(`/tasks/${task.task_id}/questions`);
  const queue = page.getByRole("region", { name: "待复核队列" });
  await expect(queue.getByRole("tab", { name: `需要处理 (${required ? 1 : 0})` })).toBeVisible();
  await expect(queue.getByRole("tab", { name: `建议处理 (${required ? 0 : 1})` })).toBeVisible();
  await queue.getByRole("tab", { name: required ? "需要处理 (1)" : "建议处理 (1)" }).click();
  const reason = required ? "评分标准包含测试得分，但测试样例尚未准备完成。请重试或补齐后再批改。" : "测试样例尚未提供，请核对或补充。";
  await expect(queue).toContainText(reason);
  const row = page.getByRole("row").filter({ has: page.getByText("Q4", { exact: true }) });
  await expect(row).toContainText(required ? "需处理" : "待复核");
  await expect(row).toContainText("1 项待复核");
  await page.screenshot({ path: testInfo.outputPath("consistent-review-queue.png"), fullPage: true });
  await queue.getByRole("link").first().click();
  const card = page.locator('[data-question-id="q4"]');
  await expect(card).toContainText("1 项待确认");
  await expect(card.locator("[data-review-issue-notes]")).toContainText(reason);
  const confirm = card.getByRole("button", { name: "第 Q4 题 · 待确认", exact: true });
  await expect(confirm).toBeEnabled();
  if (required) {
    await confirm.click();
    const dialog = page.getByRole("alertdialog", { name: "请先处理关键问题" });
    await expect(dialog).toContainText(reason);
    await dialog.getByRole("button", { name: "返回", exact: true }).click();
  }
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("consistent-review-detail.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.goto(`/frontier/live?taskId=${task.task_id}`);
  if (required) await expect(page.getByRole("button", { name: "继续识别作答", exact: true })).toHaveCount(0);
  else await expect(page.getByRole("button", { name: "继续识别作答", exact: true })).toBeEnabled();
  expect(writes).toEqual([]); // Merely viewing the details must not change readiness.
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  expect(errors).toEqual([]);
});
