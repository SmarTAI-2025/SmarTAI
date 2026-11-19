import { expect, test } from "@playwright/test";

// Production bundle + saved-response fixtures. Provider behavior is covered
// separately; this proves the teacher can see and navigate the same issues.
for (const width of [1440, 390]) test(`preparation diagnostics and concise rubric (${width}px)`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.addInitScript(() => {
    localStorage.setItem("smartai_token", "synthetic-diagnostics");
    localStorage.setItem("smartai_locale", "zh-CN");
  });
  const issues = [
    { issue_id: "formula", q_id: "q1", field: "stem", code: "recognition_needs_review", status: "open", severity: "warning", details: { reason: "conflicting_printed_formula", confidence_scope: "question" } },
    { issue_id: "rubric", q_id: "q1", field: "rubric", code: "generation_failed", status: "open", severity: "blocking",
      details: { provider_error_code: "provider_timeout" } },
    { issue_id: "reference", q_id: "q1", field: "answer", code: "generation_failed", status: "open", severity: "warning" },
  ];
  const q1 = { q_id: "q1", number: "1.10", type: "concept", stem: "按增长率由小至大排列各函数：$n^2,2^n,n!$。",
    criterion: "", reference_answer: "", max_score: 10, max_score_review_status: "confirmed", review_status: "needs_review", preparation_issues: issues };
  const q2 = { q_id: "q2", number: "1.12", type: "concept", stem: "判断下列五个独立断言正确与否。",
    criterion: "本题满分10分。配分单位1分。", effective_criterion: "共 5 小问，每问 2 分。回答正确得 2 分，错误或未答得 0 分。",
    reference_answer: "(1)正确；(2)错误；(3)正确；(4)错误；(5)正确。", max_score: 10, rubric_version: 1,
    max_score_review_status: "confirmed", review_status: "confirmed", preparation_issues: [],
    alternative_rules: [1,2,3,4,5].map(i => ({ id: `p${i}`, label: `(${i})`, text: "最高得本问满分的1/1，每个独立错误扣2分。", show_in_review: false })),
  };
  const task = { task_id: "diagnostics", name: "题目准备复核", status: "error", workflow_revision: 2,
    error: "provider_response_invalid" as string | null, last_failed_job_id: "failed-job" as string | null,
    extract_job_id: "failed-job", active_job_id: null, problem_count: 2, student_count: 0,
    problem_data: { q1, q2 }, student_data: {}, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ json: value });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-diagnostics" });
    if (path.startsWith("/experts")) return json([]);
    if (request.method() === "POST") {
      expect(path).toBe("/tasks/diagnostics/question-preparation/failed-job/manual-review");
      task.status = "problems_ready"; task.workflow_revision++; task.error = null; task.last_failed_job_id = null;
      return json({ status: "ready_for_review", workflow_revision: task.workflow_revision, job_id: "failed-job" });
    }
    if (path === "/tasks/diagnostics/state") return json({ ...task, progress: { phase: "error", error_detail: task.error, messages: [],
      preparation_counts: { questions: 2, stems: 2, reference_answers: 1, rubrics: 1 },
      preparation_review: [{ q_id: "q1", number: "1.10", issues }] } });
    if (path === "/tasks/diagnostics") return json(task);
    if (path.endsWith("/source-files")) return json({ problem_source: null, submission_sources: {} });
    return json({});
  });
  await page.goto("/tasks/diagnostics/problems/progress");
  await expect(page).toHaveTitle("SmarTAI");
  const diagnostics = page.getByRole("region", { name: "题目待处理详情" });
  await expect(diagnostics.getByText("第 1.10 题")).toBeVisible();
  await expect(diagnostics).toContainText("已保存 2 个题干、1 份标答、1 份评分标准");
  await expect(diagnostics).toContainText("需处理：评分标准未生成或不完整");
  await expect(diagnostics).toContainText("模型响应超时");
  await expect(diagnostics).not.toContainText("建议复核：");
  await expect(page.getByText("未找齐指定题目", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("preparation-failure.png"), fullPage: true });
  await page.getByRole("button", { name: "人工继续处理", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/diagnostics\/questions$/);
  const queue = page.getByRole("region", { name: "待复核队列" });
  await expect(queue.getByRole("tab", { name: "需要处理 (1)" })).toBeVisible();
  await queue.getByRole("tab", { name: "建议处理 (1)" }).click();
  await expect(queue).toContainText("标答未生成，可重试或人工补充（可选）");
  await expect(queue).toContainText("本题公式待核对");
  await page.screenshot({ path: testInfo.outputPath("review-yellow-queue.png"), fullPage: true });
  await queue.getByRole("link").first().click();
  const card = page.locator('[data-question-id="q1"]');
  await expect(card).toContainText("3 项待确认");
  await expect(card.locator("[data-review-issue-notes]")).toContainText("评分标准未生成或不完整");
  await expect(card.locator("[data-review-issue-notes]")).toContainText("模型响应超时");
  await expect(card.locator("[data-review-issue-notes]")).toContainText("本题公式待核对");
  await page.goto("/tasks/diagnostics/questions/q2/content");
  const rubric = page.locator('[data-question-id="q2"]');
  await expect(rubric).toContainText(q2.effective_criterion);
  await expect(rubric).not.toContainText(/配分单位|1\/1|独立错误|其他解法|沿用错值/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("concise-rubric.png"), fullPage: true });
  expect(errors).toEqual([]);
});
