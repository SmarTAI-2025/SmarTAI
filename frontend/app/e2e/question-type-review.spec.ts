import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const width of [1280, 390]) {
test(`missing type and materials stay visible without duplicate failure notes (${width}px)`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
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
      const patch = request.postDataJSON();
      expect(patch.reference_answer).toBeUndefined();
      if (patch.type) {
        expect(patch.type).toBe("reasoning");
        problem.type = "推理题";
      } else if (patch.criterion) {
        problem.criterion = patch.criterion;
        problem.preparation_issues.filter(issue => issue.field === "rubric").forEach(issue => { issue.status = "resolved"; });
      } else {
        expect(patch.review_status).toBe("confirmed");
        expect(problem.type && problem.criterion).toBeTruthy();
        problem.review_status = "confirmed";
        problem.preparation_issues.filter(issue => issue.status === "open").forEach(issue => { issue.status = "acknowledged"; });
      }
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
  await expect(card.getByText("请补齐缺失的题型、评分标准；完成前不能批改。")).toBeVisible();
  await expect(card.locator('[data-review-issue-notes] .text-amber-700')).toHaveText('缺少标答（可选），可按需补充。');
  await page.screenshot({ path: testInfo.outputPath("specific-missing-fields.png"), fullPage: true });
  await page.getByRole("button", { name: "进入上传作答", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("listitem")).toHaveCount(2);
  await expect(dialog.getByRole("listitem").nth(0)).toHaveText("第 1 题 · 请补齐缺失的题型；完成前不能批改。");
  await expect(dialog.getByRole("listitem").nth(1)).toHaveText("第 1 题 · 请补齐缺失的评分标准；完成前不能批改。");
  await expect(dialog).not.toContainText("标答");
  await expect(dialog).not.toContainText("请确认");
  await page.screenshot({ path: testInfo.outputPath("only-required-issues.png"), fullPage: true });
  await dialog.getByRole("button", { name: "返回", exact: true }).click();
  await card.getByRole("button", { name: "题型 · 修改" }).click();
  const select = card.getByRole("combobox", { name: "题型" });
  await expect(select).toHaveValue("");
  await expect(select.locator('option:checked')).toHaveText("请选择");
  await expect(select.locator("option")).toHaveText(["请选择", "计算题", "概念题", "推理题", "编程题", "其他"]);
  expect(mutations).toEqual([]);
  await select.selectOption("reasoning");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.getByText("请补齐缺失的评分标准；完成前不能批改。")).toBeVisible();
  expect(mutations).toEqual(["/tasks/type-review/problems/q1"]);
  await card.getByRole("button", { name: "修改第 1 题满分与评分标准" }).click();
  await card.getByRole("textbox", { name: "第 1 题评分标准" }).fill("Full credit for a correct explanation.");
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect(card.locator('[data-review-issue-notes] .text-red-700')).toHaveCount(0);
  await expect(card.getByText("缺少标答（可选），可按需补充。", { exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: "第 1 题 · 待确认", exact: true })).toBeEnabled();
  expect(problem.review_status).toBe("needs_review");
  await page.getByRole("button", { name: "进入上传作答", exact: true }).click();
  await expect(page).toHaveURL(/submissions\/upload/);
  expect(mutations).toHaveLength(2);
  expect(problem.reference_answer).toBe("");
  await page.goto("/tasks/type-review/questions");
  await expect(page.getByRole("tab", { name: "需要处理 (0)" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "建议处理 (1)" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("saved-without-confirmation.png"), fullPage: true });
  expect(errors).toEqual([]);
});

}

test("retired editors reject direct URLs and are absent from current navigation", async ({ page }) => {
  const requests: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem("smartai_token", "synthetic-retired-type-token");
    localStorage.setItem("smartai_locale", "en-US");
  });
  await page.route(`${backend}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path === "/auth/me") return route.fulfill({ json: { id: "teacher", username: "Teacher", role: "teacher", is_active: true } });
    if (path === "/auth/activity") return route.fulfill({ json: { token: "synthetic-retired-type-token" } });
    return route.fulfill({ json: [] });
  });
  for (const path of ["/teacher/assignments/new", "/teacher/assignments/old", "/teacher/assignments/old/edit", "/prob_preview", "/hw_preview", "/ai_grading"]) {
    await page.goto(path);
    await expect(page.getByText("Page not found", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(page.locator('a[href^="/teacher/assignments"]')).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: /type|题型/i })).toHaveCount(0);
  }
  expect(requests.some(path => path.startsWith("/assignments/") && !path.includes("/knowledge"))).toBe(false);
});
