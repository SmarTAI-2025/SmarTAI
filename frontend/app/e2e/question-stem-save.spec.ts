import { expect, test } from "@playwright/test";
import type { QuestionStemSave } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const path = "/tasks/linked-stem";
const oldStem = "计算 6 × 7。";
const newStem = "计算 6 × 8。";

for (const width of [1280, 390]) {
  test(`linked question save resumes after reload at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => {
      if (message.type() === "error" && !message.text().includes("net::ERR_CONNECTION_RESET")) {
        errors.push(message.text());
      }
    });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-save-token");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    let operation: QuestionStemSave | undefined;
    let submissions = 0;
    const problem = {
      q_id: "q1", number: "1", type: "计算题", max_score: 10,
      stem: oldStem, criterion: "答出 42 得 10 分。", reference_answer: "答案是 42。",
    };
    const task = {
      task_id: "linked-stem", name: "Linked question save", status: "problems_ready", workflow_revision: 4,
      problem_data: { q1: problem }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {},
    };

    await page.route(`${backend}/**`, async route => {
      const request = route.request(), requestPath = new URL(request.url()).pathname;
      const json = (value: unknown, status = 200) => route.fulfill({ json: value, status });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*",
      } });
      if (requestPath === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (requestPath === "/auth/activity") return json({ token: "synthetic-save-token" });
      if (requestPath === `${path}/question-stem-saves`) return json(operation ? [operation] : []);
      if (requestPath === `${path}/problems/q1/stem-save` && request.method() === "POST") {
        expect(request.postDataJSON()).toEqual({
          stem: newStem, expected_workflow_revision: 4,
          ...(submissions ? { retry_of: "first-save" } : {}),
        });
        operation = { id: submissions++ ? "second-save" : "first-save", q_id: "q1", status: "running",
          workflow_revision: null, error_code: null, stem: newStem };
        // The durable operation already exists when the browser loses the response.
        if (width === 1280) return route.abort("connectionreset");
        return json(operation, 202);
      }
      if (requestPath === path) return json(task);
      if (requestPath.endsWith("/source-files")) return json({
        task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {},
      });
      return json([]);
    });

    await page.goto(`${path}/questions/q1/content`);
    await expect(page).toHaveURL(/questions\/q1\/content$/);
    expect(await page.title()).not.toBe("");
    const card = page.locator('[data-question-id="q1"]');
    const stem = card.getByRole("textbox", { name: "题目", exact: true });
    const save = card.getByRole("button", { name: "保存", exact: true });
    const updating = card.getByText("正在更新标答和评分标准…");
    await card.getByRole("button", { name: "题目 · 修改", exact: true }).click();
    await stem.fill(newStem);
    await save.click();
    await expect(updating).toBeVisible();
    await expect(card.getByText("请稍后重试保存。")).toHaveCount(0);
    await page.reload();
    await expect(updating).toBeVisible();
    await expect(card.getByText("答案是 42。")).toBeVisible();
    await expect(card.getByRole("button", { name: "题目 · 修改", exact: true })).toBeDisabled();
    expect(submissions).toBe(1);

    if (width === 390) {
      operation = { ...operation!, status: "error", error_code: "provider_timeout" };
      await expect(card.getByText(/更新标答和评分标准超时/)).toBeVisible();
      await page.reload();
      await card.getByRole("button", { name: "继续修改" }).click();
      await expect(stem).toHaveValue(newStem);
      await save.click();
      await expect(updating).toBeVisible();
      expect(submissions).toBe(2);
    }

    Object.assign(problem, {
      stem: newStem, criterion: "答案 48 得 10 分。", reference_answer: "答案是 48。",
      effective_criterion: "正确得出 48 可得 10 分。",
    });
    task.workflow_revision = 5;
    operation = { id: operation!.id, q_id: "q1", status: "done", workflow_revision: 5, error_code: null };
    await expect(card.getByText("答案是 48。")).toBeVisible();
    await expect(card.getByText("正确得出 48 可得 10 分。")).toBeVisible();
    await expect(updating).toHaveCount(0);
    await expect(card.getByText(newStem)).toBeVisible();
    await card.getByRole("button", { name: "修改第 1 题满分与评分标准" }).click();
    await expect(card.getByRole("textbox", { name: "第 1 题评分标准" })).toHaveValue("答案 48 得 10 分。");
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("linked-question-saved.png"), fullPage: true });
  });
}
