import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1280, 390]) {
  for (const fraction of [false, true]) {
    test(`alternative editor preserves full rules (${locale}, ${width}px, fraction=${fraction})`, async ({ page }, testInfo) => {
      const zh = locale === "zh-CN";
      const full = fraction
        ? (zh ? "其他解法最高得本问满分的 1/3，最低0分。每个独立错误扣0.5分。" : "Alternative methods: maximum 1/3 of the part maximum, minimum 0 points. Deduct 0.5 points per independent error.")
        : (zh ? "其他解法最高得5分，最低0分。每个独立错误扣1分。累计最多扣5分。" : "Alternative methods: maximum 5 points, minimum 0 points. Deduct 1 points per independent error. Maximum deduction 5 points.");
      const summary = fraction
        ? (zh ? "其他解法最高得 3.33333 分，每个独立错误扣 0.5 分。" : "Alternative methods earn up to 3.33333 points; deduct 0.5 points per independent error.")
        : (zh ? "其他解法最高得 5 分，每个独立错误扣 1 分。" : "Alternative methods earn up to 5 points; deduct 1 points per independent error.");
      const edited = fraction ? full.replace("0.5", "0.25") : full.replace("5", "8");
      const question = { q_id: "q1", number: "1", type: "计算题", max_score: 10,
        stem: "Solve x²−3x+2=0.", criterion: "Correct solution: 10 points.", reference_answer: "x=1 or x=2.",
        rubric_version: 1, review_status: "confirmed", preparation_issues: [],
        alternative_rules: [{ id: "p", label: null, text: full, display_text: summary, show_in_review: true }] };
      const task = { task_id: "alternative-edit", name: "Alternative edit", status: "problems_ready", workflow_revision: 1,
        problem_data: { q1: question }, student_data: {}, problem_count: 1, student_count: 0,
        created_at: 1, updated_at: 1, kb_docs: {} };
      const errors: string[] = [];
      let saves = 0;
      await page.setViewportSize({ width, height: 900 });
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
      await page.addInitScript(lang => {
        localStorage.setItem("smartai_token", "synthetic-alternative-token");
        localStorage.setItem("smartai_locale", lang);
      }, locale);
      await page.route(`${backend}/**`, async route => {
        const request = route.request(), path = new URL(request.url()).pathname;
        if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
          "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
        const json = (value: unknown) => route.fulfill({ json: value });
        if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
        if (path === "/auth/activity") return json({ token: "synthetic-alternative-token" });
        if (path === "/tasks/alternative-edit/problems/q1" && request.method() === "PUT") {
          expect(request.postDataJSON().alternative_rule_edits).toEqual({ p: edited });
          saves++;
          question.alternative_rules[0].text = edited;
          task.workflow_revision++;
          return json({ status: "ok", problem: question, workflow_revision: task.workflow_revision });
        }
        if (path === "/tasks/alternative-edit") return json(task);
        if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1,
          problem_source: null, submission_sources: {} });
        return json([]);
      });
      await page.goto("/tasks/alternative-edit/questions/q1/content");
      await expect(page).toHaveURL(/questions\/q1\/content$/);
      expect(await page.title()).not.toBe("");
      const card = page.locator('[data-question-id="q1"]');
      await expect(card.getByText(summary, { exact: true })).toBeVisible();
      await card.getByRole("button", { name: zh ? "修改第 1 题满分与评分标准" : "Edit score and rubric for question 1" }).click();
      const input = card.getByRole("textbox", { name: zh ? "其他解法评分标准" : "Alternative methods" });
      await expect(input).toHaveValue(full);
      await input.fill(edited);
      await input.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath("alternative-editor.png"), fullPage: true });
      await card.getByRole("button", { name: zh ? "保存" : "Save", exact: true }).click();
      await expect(input).toHaveCount(0);
      expect(saves).toBe(1);
      // Reopen after the accepted response: editing must still use exact text.
      await card.getByRole("button", { name: zh ? "修改第 1 题满分与评分标准" : "Edit score and rubric for question 1" }).click();
      await expect(input).toHaveValue(edited);
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
      expect(errors).toEqual([]);
    });
  }
}
