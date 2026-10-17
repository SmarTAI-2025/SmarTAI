import { expect, test } from "@playwright/test";
import type { GradingSetup, TaskStatus } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1440, 390]) {
  test(`${locale} ${width}px: dashboard and history share stages and workspace destinations`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN";
    const errors: string[] = [];
    await page.setViewportSize({ width, height: 1000 });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(locale => {
      localStorage.setItem("smartai_locale", locale);
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
    }, locale);
    const problem = { q_id: "q1", number: "1", type: "short", stem: "Six times seven?", reference_answer: "42",
      criterion: "Correct answer", max_score: 10, review_status: "confirmed" };
    const student = { stu_id: "S1", stu_name: "Example", identity_status: "matched",
      stu_ans: [{ q_id: "q1", content: "42", review_status: "pending", flag: [] }] };
    const statuses: Record<string, TaskStatus> = { questions: "problems_ready", answers: "submissions_ready", configured: "grading_setup", graded: "graded", finalized: "finalized" };
    const tasks = Object.entries(statuses).map(([id, status]) => ({
      task_id: id, owner_id: "stage-teacher", name: `Stage ${id}`, status, workflow_revision: 1,
      created_at: 1, updated_at: 1, problem_count: 1, student_count: id === "questions" ? 0 : 1,
      // An earlier saved setup must not send question review to upload.
      grading_setup_configured: id !== "answers", tag_ids: [], kb_docs: {},
      problem_data: { q1: problem }, student_data: id === "questions" ? {} : { S1: student },
    }));
    const setup: GradingSetup = {
      schema_version: 1, selected_provider_ids: ["p1"], primary_provider_id: "p1", aggregation_method: "single",
      expert_weights: {}, multi_sample_n: 1, knowledge_scope: "none", strictness: 50, allow_partial_credit: true,
      feedback_tone: "neutral", feedback_length: "medium", feedback_language: "zh", suggest_corrections: true,
      low_confidence_threshold: .6, teacher_notes: "",
    };
    const expert = { provider_id: "p1", provider_type: "openai", model: "test", display_name: "Test grader", enabled: true, scope: "owner", editable: true };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*",
      } });
      if (path === "/auth/me") return json({ id: "stage-teacher", username: "Test teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path.startsWith("/experts")) return json([expert]);
      if (path === "/tags/") return json([]);
      if (path === "/course-materials/") return json({ items: [] });
      if (path === "/knowledge/activity") return json({ items: [], active_count: 0, total: 0, page: 1, page_size: 5 });
      if (path === "/tasks/") return json(url.searchParams.has("page")
        ? { items: tasks, total: tasks.length, page: 1, page_size: 25, available_facets: { semesters: [], courses: [], tags: [], statuses: {} } }
        : Object.fromEntries(tasks.map(task => [task.task_id, task])));
      const task = tasks.find(item => path === `/tasks/${item.task_id}`);
      if (task) return json(task);
      if (path === "/tasks/configured/grading-setup") return json({
        task_id: "configured", task_status: "grading_setup", workflow_revision: 1, configured: true,
        grading_setup: setup, suggested_setup: setup, grading_setup_fingerprint: "saved",
        available_experts: [expert], knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: 0, task_docs: [] },
        readiness: { ready: true, blocking_issues: [], warnings: [] },
      });
      if (path.endsWith("/kb")) return json({ docs: [] });
      throw new Error(`Unexpected request ${request.method()} ${path}`);
    });

    const cases = [
      { id: "questions", suffix: "questions", label: zh ? "校对题目" : "Review questions", heading: zh ? "题目资料总览" : "Question Material Overview" },
      { id: "answers", suffix: "submissions", label: zh ? "校对作答" : "Review submissions", heading: zh ? "作答校对总览" : "Submission Review Overview" },
      { id: "configured", suffix: "grading-setup", label: zh ? "批改配置" : "Grading setup", heading: zh ? "批改设置" : "Grading Setup" },
    ];
    const colors = new Map<string, string>();
    for (const surface of ["dashboard", "history"]) {
      await page.goto(`/${surface}`);
      await expect(page).toHaveTitle("SmarTAI");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(surface === "dashboard" ? (zh ? "工作台" : "Workspace") : (zh ? "历史任务" : "History"));
      if (surface === "dashboard") {
        const modelCard = page.getByRole("link", { name: /Models \/ BYOK|模型 \/ BYOK/ });
        await expect(modelCard).toBeVisible();
        expect(await modelCard.locator("strong").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      }
      await expect(page.getByText(/题目待校对|作答待校对|结果已保存|分析生成中|Results saved|Generating analysis/)).toHaveCount(0);
      for (const item of cases) {
        const pill = page.locator('span.rounded-full[class*="bg-"]:visible, a.rounded-full[class*="bg-"]:visible')
          .filter({ hasText: new RegExp(`^${item.label}$`) }).first();
        await expect(pill).toBeVisible();
        const color = await pill.evaluate(element => getComputedStyle(element).backgroundColor);
        if (surface === "dashboard") colors.set(item.id, color);
        else expect(color).toBe(colors.get(item.id));
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${surface}.png`), fullPage: true });
      for (const item of cases) {
        const href = `/tasks/${item.id}/${item.suffix}`;
        await page.locator(`a[href="${href}"]:visible`).filter({ hasText: item.label }).first().click();
        await expect(page).toHaveURL(new RegExp(`${href}$`));
        await expect(page.getByRole("heading", { level: 1 })).toHaveText(item.heading);
        if (item.id === "questions") await expect(page.getByRole("table")).toBeVisible();
        if (item.id === "answers") await expect(page.getByRole("region", { name: zh ? "学生与题目作答状态矩阵" : "Student-by-question submission status matrix" }).last()).toBeVisible();
        if (item.id === "configured") await expect(page.getByRole("button", { name: zh ? "保存并查看批改摘要" : "Save & Review Grading" })).toBeEnabled();
        await page.goto(`/${surface}`);
      }
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    }
    expect(errors).toEqual([]);
  });
}
