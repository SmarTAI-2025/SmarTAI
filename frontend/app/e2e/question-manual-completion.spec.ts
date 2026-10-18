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
      return json({ task_id: task.task_id, workflow_revision: 3, first_question_id: "q2" });
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

test("early pause requires question, rubric and source acknowledgement; reference answer is optional", async ({ page }) => {
  const errors: string[] = [], mutations: unknown[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const task = { task_id: "early-manual", name: "人工来源复核", status: "problems_paused", workflow_revision: 2,
    extract_job_id: "early-job", last_failed_job_id: "early-job" as string | null, error: "operation_cancelled" as string | null,
    problem_count: 0, problem_data: {}, student_data: {}, student_count: 0, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  const sources = ["first.pdf", "second.pdf"].map((filename, i) => ({ source_token: `source-${i}`, filename, role: "problem", source_kind: "upload", available: true }));
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-manual"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-manual" });
    if (path.startsWith("/experts")) return json([]);
    if (path.endsWith("/question-preparation/input")) return json({ task_id: task.task_id, workflow_revision: 2, input: { job_id: "early-job", sources } });
    if (request.method() === "POST") {
      expect(path).toBe("/tasks/early-manual/question-preparation/early-job/manual-review");
      const body = request.postDataJSON(); mutations.push(body);
      if (!body.manual_questions) return json({ status: "sources_pending", first_question_id: null, workflow_revision: 2 });
      expect(body.completed_source_tokens).toEqual(["source-0", "source-1"]);
      expect(body.manual_questions[0].q_id).toBe("teacher-q");
      expect(body.manual_questions[0].reference_answer).toBe("");
      task.status = "problems_ready"; task.workflow_revision++; task.last_failed_job_id = null; task.error = null;
      task.problem_data = { "teacher-q": { ...body.manual_questions[0], review_status: "needs_review", preparation_issues: [], max_score_review_status: "confirmed" } };
      task.problem_count = 1;
      return json({ status: "ready_for_review", first_question_id: "teacher-q", workflow_revision: 3 });
    }
    if (path.endsWith("/state")) return json({ ...task, progress: { messages: [], active: [] } });
    if (path === "/tasks/early-manual") return json(task);
    return json([]);
  });
  await page.goto("/tasks/early-manual/problems/progress");
  await page.getByRole("button", { name: "人工继续处理", exact: true }).click();
  await expect(page.getByRole("heading", { name: "人工补齐题目" })).toBeVisible();
  await expect(page.getByRole("list", { name: "对照原资料" })).toContainText("second.pdf");
  const save = page.getByRole("button", { name: "保存题目并继续复核" });
  await expect(save).toBeDisabled();
  await page.getByLabel("题号", { exact: true }).fill("teacher-q");
  await page.getByLabel("题干", { exact: true }).fill("What is 2 + 2?");
  await page.getByLabel("评分标准（写明分值）", { exact: true }).fill("Award 10 points for 4.");
  await page.getByRole("checkbox").nth(0).check();
  await expect(save).toBeDisabled();
  await page.getByRole("checkbox").nth(1).check();
  await expect(save).toBeEnabled();
  expect(mutations).toHaveLength(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "output/playwright/manual-source-queue.png", fullPage: true });
  await save.click();
  await expect(page.getByRole("heading", { name: "题目资料总览", exact: true })).toBeVisible();
  expect(mutations).toHaveLength(2);
  expect(errors).toEqual([]);
});
