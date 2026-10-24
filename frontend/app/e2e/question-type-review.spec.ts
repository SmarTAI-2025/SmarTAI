import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

test("missing type and materials stay visible without duplicate failure notes", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("smartai_token", "synthetic-review-token");
    localStorage.setItem("smartai_locale", "zh-CN");
  });
  const problem = { q_id: "q1", number: "1", type: "", stem: "Compute 2 + 2.", criterion: "", reference_answer: "", max_score: 10,
    review_status: "needs_review", max_score_review_status: "confirmed", preparation_issues: [
      { issue_id: "rubric", code: "generation_failed", field: "rubric", severity: "blocking", status: "open" },
      { issue_id: "answer", code: "generation_failed", field: "answer", severity: "warning", status: "open" },
    ] };
  const task = { task_id: "type-review", name: "Type review", status: "problems_ready", workflow_revision: 1,
    problem_data: { q1: problem }, student_data: {}, problem_count: 1, student_count: 0, created_at: 1, updated_at: 1, kb_docs: {} };
  const mutations: string[] = [];
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-review-token" });
    if (request.method() !== "GET") {
      mutations.push(path);
      expect(path).toBe("/tasks/type-review/problems/q1");
      expect(request.postDataJSON().type).toBe("reasoning");
      problem.type = "推理题";
      task.workflow_revision++;
      return json({ status: "ok", q_id: "q1", problem, workflow_revision: task.workflow_revision });
    }
    if (path === "/tasks/type-review") return json(task);
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1, problem_source: null, submission_sources: {} });
    return json([]);
  });
  await page.goto("/tasks/type-review/questions");
  await expect(page.getByRole("tab", { name: "需要处理 (1)" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "建议处理 (0)" })).toBeVisible();
  await page.goto("/tasks/type-review/questions/q1/content");
  const card = page.locator('[data-question-id="q1"]');
  await expect(card.getByText("生成失败", { exact: true })).toHaveCount(1);
  await expect(card.getByText("请补齐缺失的题型、评分标准、标答并确认本题；完成前不能批改。")).toBeVisible();
  await expect(card.locator('[data-review-issue-notes] .text-amber-700')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("specific-missing-fields.png"), fullPage: true });
  await card.getByRole("button", { name: "题型 · 修改" }).click();
  const select = card.getByRole("combobox", { name: "题型" });
  await expect(select).toHaveValue("");
  await expect(select.locator('option:checked')).toHaveText("请选择");
  await expect(select.locator("option")).toHaveText(["请选择", "计算题", "概念题", "推理题", "编程题", "其他"]);
  expect(mutations).toEqual([]);
  await select.selectOption("reasoning");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.getByText("请补齐缺失的评分标准、标答并确认本题；完成前不能批改。")).toBeVisible();
  expect(mutations).toEqual(["/tasks/type-review/problems/q1"]);
  expect(errors).toEqual([]);
});
