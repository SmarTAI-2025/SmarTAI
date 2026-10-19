import { test, expect } from "@playwright/test";

for (const paused of [false, true]) test(`${paused ? "paused" : "failed"} question → manual queue → editor → confirm; successful questions are preserved`, async ({ page }) => {
  const errors: string[] = [], mutations: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const provider = { provider_id: "test-provider", provider_type: "gemini", model: "synthetic", enabled: true, is_default: true };
  const good = { q_id: "q1", number: "1", type: "short", stem: "What is 6 × 7?", max_score: 1,
    reference_answer: "42", criterion: "One point for 42.", review_status: "needs_review", max_score_review_status: "confirmed", preparation_issues: [] };
  const failed = { ...good, q_id: "q2", number: "2", stem: "What is 2 + 2?", reference_answer: "", criterion: "",
    preparation_issues: [
      { issue_id: "answer", field: "answer", code: "generation_failed", status: "open", severity: "blocking", details: { target: "reference_answer" } },
      { issue_id: "rubric", field: "rubric", code: "generation_failed", status: "open", severity: "blocking", details: { target: "criterion" } },
      { issue_id: "manual", field: "source", code: "manual_completion_required", status: "open", severity: "warning", details: { required_fields: ["reference_answer", "criterion"] } },
    ] };
  const task = { task_id: "manual-test", name: "手动补全验证", status: paused ? "problems_paused" : "error", workflow_revision: 2,
    extract_job_id: "failed-job", last_failed_job_id: "failed-job" as string | null, error: "provider_response_invalid" as string | null,
    question_recognition_provider_id: provider.provider_id, problem_count: 0, problem_data: {}, student_data: {},
    student_count: 0, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-manual"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-manual" });
    if (path.startsWith("/experts")) return json([provider]);
    if (request.method() === "POST") {
      mutations.push(path);
      expect(path).toBe("/tasks/manual-test/question-preparation/failed-job/manual-review");
      expect(request.postDataJSON()).toEqual({ expected_workflow_revision: 2 });
      task.status = "problems_ready"; task.error = null; task.last_failed_job_id = null;
      task.workflow_revision = 3; task.problem_count = 2; task.problem_data = { q1: good, q2: failed };
      return json({ status: "ready_for_review", task_id: task.task_id, workflow_revision: 3, first_question_id: "q2" });
    }
    if (request.method() === "PUT") {
      mutations.push(path);
      expect(path).toBe("/tasks/manual-test/problems/q2");
      const body = request.postDataJSON();
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      task.workflow_revision++;
      Object.assign(failed, body);
      for (const issue of failed.preparation_issues) {
        if (body.review_status === "confirmed") issue.status = "acknowledged";
        else if (issue.details.target && body[issue.details.target]) issue.status = "resolved";
      }
      return json({ status: "ok", problem: failed, workflow_revision: task.workflow_revision });
    }
    if (path.endsWith("/state")) return json({ ...task, progress: { job_id: "failed-job", phase: "error", active: [], messages: [],
      error_detail: "provider_response_invalid", failed_question_ids: ["q2"], completed_question_ids: ["q1"], total_questions: 2 } });
    if (path === "/tasks/manual-test") return json(task);
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    return json([]);
  });
  await page.goto("/tasks/manual-test/problems/progress");
  if (!paused) await expect(page.getByRole("button", { name: "重试失败项", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "人工继续处理", exact: true }).click();
  await expect(page).toHaveURL(/tasks\/manual-test\/questions$/);
  const queue = page.getByRole("region", { name: "待复核队列", exact: true });
  await expect(queue.getByRole("tab", { name: "需要处理 (1)", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(queue.getByRole("link")).toHaveCount(1);
  await queue.getByRole("link").click();
  await expect(page).toHaveURL(/questions\/q2\/content#question-q2$/);
  const card = page.locator('[data-question-id="q2"]');
  await expect(card.locator("textarea")).toHaveCount(0);
  await card.getByRole("button", { name: "标答 / 解题步骤 · 修改", exact: true }).click();
  await card.getByRole("button", { name: "修改第 2 题满分与评分标准", exact: true }).click();
  await expect(card.locator("textarea")).toHaveCount(2);
  await expect(page.locator('[data-question-id="q1"]')).toContainText("One point for 42.");
  await expect(page.locator('[data-question-id="q1"] textarea')).toHaveCount(0);
  await card.getByRole("button", { name: /· 待确认$/ }).click();
  expect(mutations).toHaveLength(1);
  await page.getByRole("button", { name: "返回", exact: true }).click();
  const answer = card.locator("section").filter({ has: page.getByRole("heading", { name: "标答 / 解题步骤", exact: true }) }).first();
  await answer.locator("textarea").fill("4");
  await answer.getByRole("button", { name: "保存", exact: true }).click();
  await expect(answer.locator("textarea")).toHaveCount(0);
  const rubric = card.locator("textarea");
  await rubric.fill("One point for 4.");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.locator("textarea")).toHaveCount(0);
  await card.getByRole("button", { name: /· 待确认$/ }).click();
  await expect(card.getByRole("button", { name: /· 已确认$/ })).toBeVisible();
  await page.screenshot({ path: "output/playwright/manual-question-completed.png", fullPage: true });
  expect(mutations).toHaveLength(4);
  expect(errors).toEqual([]);
});

for (const paused of [false, true]) test(`early ${paused ? "pause" : "failure"} enters the original matrix, creates rows, and edits existing details`, async ({ page }, testInfo) => {
  const errors: string[] = [], mutations: unknown[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const problem = { q_id: "manual-1", number: "1", type: "short", stem: "", reference_answer: "", criterion: "", max_score: 10,
    review_status: "needs_review", max_score_review_status: "needs_review", preparation_issues: [
      { issue_id: "manual", field: "source", code: "manual_completion_required", status: "open", severity: "warning", details: { required_fields: ["stem", "criterion"] } },
    ] };
  const task = { task_id: "early-manual", name: "原矩阵人工复核", status: paused ? "problems_paused" : "error", workflow_revision: 2,
    extract_job_id: "early-job", last_failed_job_id: "early-job" as string | null, error: paused ? "operation_cancelled" : "provider_response_invalid" as string | null,
    manual_question_count_required: false, problem_count: 0, problem_data: {}, student_data: {}, student_count: 0, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-manual"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-manual" });
    if (path.startsWith("/experts")) return json([]);
    if (request.method() === "POST") {
      expect(path).toBe("/tasks/early-manual/question-preparation/early-job/manual-review");
      const body = request.postDataJSON(); mutations.push(body);
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      expect(body.manual_questions).toBeUndefined();
      task.status = "problems_ready"; task.workflow_revision++; task.last_failed_job_id = null; task.error = null;
      task.manual_question_count_required = !body.question_count;
      if (body.question_count) {
        expect(body.question_count).toBe(1);
        task.problem_data = { "manual-1": problem }; task.problem_count = 1;
      }
      return json({ status: "ready_for_review", first_question_id: body.question_count ? "manual-1" : null, workflow_revision: task.workflow_revision });
    }
    if (request.method() === "PUT") {
      expect(path).toBe("/tasks/early-manual/problems/manual-1");
      const body = request.postDataJSON(); mutations.push(body);
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      Object.assign(problem, body); task.workflow_revision++;
      if (body.max_score) problem.max_score_review_status = "confirmed";
      if (body.review_status === "confirmed") problem.preparation_issues[0].status = "acknowledged";
      return json({ status: "ok", problem, workflow_revision: task.workflow_revision });
    }
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    if (path.endsWith("/state")) return json({ ...task, progress: { messages: [], active: [] } });
    if (path === "/tasks/early-manual") return json(task);
    return json([]);
  });
  await page.goto("/tasks/early-manual/problems/progress");
  await page.getByRole("button", { name: "人工继续处理", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/early-manual\/questions$/);
  await expect(page.getByRole("heading", { name: "题目资料总览" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("spinbutton", { name: "题数" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "人工补齐题目" })).toHaveCount(0);
  await page.getByRole("spinbutton", { name: "题数" }).fill("1");
  await page.getByRole("button", { name: "建立题目行" }).click();
  const queue = page.getByRole("region", { name: "待复核队列" });
  await queue.getByRole("tab", { name: /需要处理/ }).click();
  await queue.getByRole("link").click();
  const card = page.locator('[data-question-id="manual-1"]');
  await card.getByRole("button", { name: "题号 · 修改", exact: true }).click();
  await card.getByRole("textbox", { name: "题号", exact: true }).fill("1.1.5");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.getByRole("heading", { name: "第 1.1.5 题" })).toBeVisible();
  await card.getByRole("button", { name: "题型 · 修改", exact: true }).click();
  await card.getByRole("combobox", { name: "题型" }).selectOption("other");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.getByText("其他", { exact: true })).toHaveCount(2);
  await page.reload();
  await expect(card.getByText("其他", { exact: true })).toHaveCount(2);
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("question-number-and-type.png") });
  await card.getByRole("button", { name: "题目 · 修改", exact: true }).click();
  await card.getByRole("textbox", { name: "题目", exact: true }).fill("What is 2 + 2?");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await card.getByRole("button", { name: "修改第 1.1.5 题满分与评分标准", exact: true }).click();
  await card.getByRole("textbox", { name: "第 1.1.5 题评分标准" }).fill("Award 10 points for 4.");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await card.getByRole("button", { name: /· 待确认$/ }).click();
  await expect(card.getByRole("button", { name: /· 已确认$/ })).toBeVisible();
  await page.goto("/tasks/early-manual/questions?manualJob=early-job");
  await expect(page.getByRole("heading", { name: "题目资料总览" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "人工补齐题目" })).toHaveCount(0);
  expect(mutations).toHaveLength(7);
  expect(errors).toEqual([]);
});
