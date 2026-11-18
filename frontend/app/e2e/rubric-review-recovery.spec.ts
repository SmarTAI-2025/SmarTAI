import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1280, 390]) for (const recovered of [false, true]) {
  test(`rubric review preserves edits and explains model recovery (${locale}, ${width}px, recovered=${recovered})`, async ({ page }, testInfo) => {
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
      criterion: recovered ? "" : criterion, reference_answer: "10+x=17, x=7.", review_status: "needs_review", max_score_review_status: "needs_review",
      preparation_issues: [{ issue_id: "review", field: "rubric", code: "generation_failed", severity: "blocking", status: "open",
        details: { reason: "invalid_structured_rubric", rubric_review_draft: { status: "needs_review", fields: [
          { path: "part_rubric.parts[0].steps[0].criterion", value: zh ? "建立等式" : "Establish the equation" },
          { path: "part_rubric.parts[0].steps[0].total_pts", value: 3 },
        ], invalid_fields: ["part_rubric.parts[0].steps[1]"] } } }],
    };
    const task = { task_id: "review-recovery", name: zh ? "评分标准复核" : "Rubric review", status: "problems_ready",
      question_recognition_provider_id: "old-model", workflow_revision: 1, problem_data: { q1: question }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {} };
    let code = "provider_auth_failed", saves = 0, confirmations = 0;
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-review-token" });
      if (path.endsWith("/question-preparation/provider") && request.method() === "PUT") {
        expect(request.postDataJSON().expected_workflow_revision).toBe(task.workflow_revision);
        task.question_recognition_provider_id = request.postDataJSON().provider_id;
        task.workflow_revision++;
        return json({ question_recognition_provider_id: task.question_recognition_provider_id, workflow_revision: task.workflow_revision });
      }
      if (path.endsWith("/problems/q1") && request.method() === "PUT") {
        if (request.postDataJSON().review_status === "confirmed") {
          confirmations++;
          question.review_status = "confirmed";
          question.max_score_review_status = "confirmed";
          question.preparation_issues = [];
          task.workflow_revision++;
          return json({ problem: question, workflow_revision: task.workflow_revision });
        }
        saves++;
        expect(request.postDataJSON()).not.toHaveProperty("rubric_provider_id");
        if (task.question_recognition_provider_id === "retry-model") {
          expect(request.postDataJSON().expected_workflow_revision).toBe(task.workflow_revision);
          question.criterion = request.postDataJSON().criterion;
          question.preparation_issues = [];
          task.workflow_revision++;
          return json({ problem: question, workflow_revision: task.workflow_revision });
        }
        return route.fulfill({ status: 422, json: { error: { code, message: "private diagnostic" } } });
      }
      if (path === "/experts/quota") return json({ resets_at: "2026-11-19T00:00:00Z", shared_pool_enabled: false,
        shared: { requests: 0, request_limit: 100, estimated_input_tokens: 0, estimated_token_limit: 100000 },
        history: { requests: 0, request_limit: 100 } });
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
    await expect(card.getByRole("complementary", { name: zh ? "待核对的评分项" : "Scoring details to review" })).toHaveCount(0);
    const scoring = card.locator("section").filter({ has: page.getByRole("heading", { name: zh ? "本题满分与评分标准" : "Maximum Score and Rubric", exact: true }) });
    await expect(scoring).toContainText(recovered ? (zh ? "建立等式" : "Establish the equation") : criterion);
    if (recovered) await expect(scoring).toContainText(zh ? "步骤 2：待核对" : "Step 2：Review needed");
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
    await expect(input).toHaveValue(recovered ? /Establish the equation|建立等式/ : criterion);
    const corrected = zh ? "列式3分，计算结果7分。" : "Equation: 3 points. Result: 7 points.";
    await input.fill(corrected);
    // Leaving for model management uses the same durable draft protection.
    await page.getByRole("link", { name: zh ? "管理模型" : "Manage models" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: zh ? "暂存并离开" : "Save draft and leave", exact: true }).click();
    await expect(page).toHaveURL(/settings\/byok/);
    await page.goBack();
    await expect(input).toHaveValue(corrected);
    const save = card.getByRole("button", { name: zh ? "保存" : "Save", exact: true });
    await save.click();
    await expect(page.getByRole("alertdialog")).toContainText(zh ? "请在 API 设置中更新模型密钥或权限" : "Update the model's API key or permissions");
    await expect(input).toHaveValue(corrected);
    await page.keyboard.press("Escape");
    code = "provider_quota_exceeded";
    await save.click();
    await expect(page.getByRole("alertdialog")).toContainText(zh ? "请更换可用模型，或等模型额度恢复" : "Choose another available model");
    await expect(input).toHaveValue(corrected);
    expect(saves).toBe(2);
    await expect(page.getByText("private diagnostic")).toHaveCount(0);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("rubric-save-guidance-after.png"), fullPage: true });
    await page.getByRole("alertdialog").getByRole("button", { name: zh ? "更换模型" : "Change model" }).click();
    const picker = page.getByRole("combobox", { name: zh ? "题目准备模型" : "Question preparation model" });
    await expect(picker).toBeFocused();
    await expect(picker).toHaveValue("old-model");
    await picker.selectOption("retry-model");
    await expect(picker).toHaveValue("retry-model");
    await save.click();
    await expect(input).toHaveCount(0);
    await expect(card.getByRole("complementary")).toHaveCount(0);
    expect(saves).toBe(3);
    expect(question.criterion).toBe(corrected);
    await card.getByRole("button", { name: zh ? "第 1 题 · 待确认" : "Question 1 · Confirm review" }).click();
    await expect(card.getByRole("button", { name: zh ? "第 1 题 · 已确认" : "Question 1 · Confirmed" })).toBeVisible();
    expect(confirmations).toBe(1);
    expect(saves).toBe(3);
    await page.screenshot({ path: testInfo.outputPath("rubric-saved-confirmed.png"), fullPage: true });
    expect(errors).toEqual([]);
  });
}

for (const width of [1280, 390]) {
  test(`uncertain complete rubric stays editable with a yellow review suggestion (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-rubric-token");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const question = { q_id: "q1", number: "1", type: "推理题", max_score: 10,
      stem: "证明两个奇数之和为偶数。", reference_answer: "写成2m+1和2n+1，相加得2(m+n+1)。",
      criterion: "分别将两个奇数写成2m+1和2n+1（4分）。求和化为2(m+n+1)，利用m+n+1为整数得出偶数结论（6分）。",
      review_status: "needs_review", max_score_review_status: "needs_review", preparation_issues: [{ issue_id: "uncertain", field: "rubric",
        code: "math_rubric_advisory", severity: "warning", status: "open",
        details: { reason: "teacher_rule_interpretation", message: "评分标准置信度较低，请核对评分依据与配分。" } }],
    };
    const task = { task_id: "uncertain-rubric", name: "评分标准复核", status: "problems_ready", workflow_revision: 1,
      problem_data: { q1: question }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {} };
    let confirms = 0;
    await page.route(`${backend}/**`, async route => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-rubric-token" });
      if (path.endsWith("/problems/q1") && route.request().method() === "PUT") {
        expect(route.request().postDataJSON().review_status).toBe("confirmed");
        confirms++;
        question.review_status = "confirmed";
        question.max_score_review_status = "confirmed";
        question.preparation_issues = [];
        task.workflow_revision++;
        return json({ problem: question, workflow_revision: task.workflow_revision });
      }
      if (path === "/tasks/uncertain-rubric") return json(task);
      if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1, problem_source: null, submission_sources: {} });
      return json([]);
    });
    await page.goto("/tasks/uncertain-rubric/questions/q1/content");
    const card = page.locator('[data-question-id="q1"]');
    await expect(card).toContainText(question.criterion);
    await expect(card.locator('[data-review-issue-notes] .text-amber-700')).toContainText("评分标准置信度较低");
    await expect(card.getByRole("complementary", { name: "待核对的评分项" })).toHaveCount(0);
    const confirm = card.getByRole("button", { name: "第 1 题 · 待确认" });
    await expect(confirm).not.toHaveClass(/border-red-200/);
    await page.screenshot({ path: testInfo.outputPath("rubric-yellow-review.png"), fullPage: true });
    await confirm.click();
    await expect(card).not.toContainText("评分标准置信度较低");
    await expect(card.getByRole("button", { name: "第 1 题 · 已确认" })).toBeVisible();
    expect(confirms).toBe(1);
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  });
}
