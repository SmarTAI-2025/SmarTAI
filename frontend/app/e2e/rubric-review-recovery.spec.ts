import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1280, 390]) {
  test(`rubric review preserves edits and explains model recovery (${locale}, ${width}px)`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN";
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => {
      if (message.type() === "error" && !/Failed to load resource:.*422/.test(message.text())) errors.push(message.text());
    });
    await page.addInitScript(language => {
      localStorage.setItem("smartai_token", "synthetic-review-token");
      localStorage.setItem("smartai_locale", language);
    }, locale);
    const criterion = zh ? "列式3分，计算结果8分。" : "Equation: 3 points. Result: 8 points.";
    const question = { q_id: "q1", number: "1", type: "计算题", max_score: 10,
      stem: zh ? "笔和本共17元，本10元，求笔价。" : "A pen and notebook cost 17 dollars. The notebook costs 10 dollars. Find the pen's price.",
      criterion, reference_answer: "10+x=17, x=7.", review_status: "needs_review",
      preparation_issues: [{ issue_id: "review", field: "rubric", code: "generation_failed", severity: "blocking", status: "open",
        details: { reason: "invalid_structured_rubric", rubric_review_draft: { status: "needs_review", fields: [
          { path: "part_rubric.parts[0].steps[0].total_pts", value: 3 },
        ] } } }],
    };
    const task = { task_id: "review-recovery", name: zh ? "评分标准复核" : "Rubric review", status: "problems_ready",
      workflow_revision: 1, problem_data: { q1: question }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {} };
    let code = "provider_auth_failed", saves = 0;
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-review-token" });
      if (path.endsWith("/problems/q1") && request.method() === "PUT") {
        saves++;
        if (request.postDataJSON().rubric_provider_id === "retry-model") {
          question.criterion = request.postDataJSON().criterion;
          question.preparation_issues = [];
          task.workflow_revision++;
          return json({ problem: question, workflow_revision: task.workflow_revision });
        }
        return route.fulfill({ status: 422, json: { error: { code, message: "private diagnostic" } } });
      }
      if (path.startsWith("/experts")) return json([{ provider_id: "retry-model", provider_type: "openai", model: "New model", enabled: true, max_concurrent: 1, rpm: 10 }]);
      if (path === "/tasks/review-recovery") return json(task);
      if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1, problem_source: null, submission_sources: {} });
      return json([]);
    });
    await page.goto("/tasks/review-recovery/questions/q1/content");
    expect(await page.title()).not.toBe("");
    const card = page.locator('[data-question-id="q1"]');
    await expect(card).toBeVisible();
    await expect(card.getByText(zh ? "已保留已识别评分项，请核对分值和适用条件后保存。"
      : "Extracted scoring details are saved. Review the points and conditions, then save your corrections.")).toBeVisible();
    const confirm = card.getByRole("button", { name: zh ? "第 1 题 · 待确认" : "Question 1 · Confirm review" });
    await expect(confirm).toHaveClass(/border-red-200/);
    await confirm.click();
    await expect(page.getByRole("alertdialog")).toBeVisible();
    expect(saves).toBe(0);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("rubric-review-after.png"), fullPage: true });
    await card.getByRole("button", { name: zh ? "修改第 1 题满分与评分标准" : "Edit score and rubric for question 1" }).click();
    const input = card.getByRole("textbox", { name: zh ? "第 1 题评分标准" : "Rubric for question 1" });
    const corrected = zh ? "列式3分，计算结果7分。" : "Equation: 3 points. Result: 7 points.";
    await input.fill(corrected);
    const save = card.getByRole("button", { name: zh ? "保存" : "Save", exact: true });
    await save.click();
    await expect(card.getByRole("alert")).toContainText(zh ? "请在 API 设置中更新模型密钥或权限" : "Update the model's API key or permissions");
    await expect(input).toHaveValue(corrected);
    code = "provider_quota_exceeded";
    await save.click();
    await expect(card.getByRole("alert")).toContainText(zh ? "请更换可用模型，或等模型额度恢复" : "Choose another available model");
    await expect(input).toHaveValue(corrected);
    expect(saves).toBe(2);
    await expect(page.getByText("private diagnostic")).toHaveCount(0);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("rubric-save-guidance-after.png"), fullPage: true });
    const picker = card.getByRole("combobox", { name: zh ? "换模型重试" : "Retry with another model" });
    await expect(picker).toHaveValue("");
    await picker.selectOption("retry-model");
    await save.click();
    await expect(input).toHaveCount(0);
    expect(saves).toBe(3);
    expect(question.criterion).toBe(corrected);
    expect(errors).toEqual([]);
  });
}
