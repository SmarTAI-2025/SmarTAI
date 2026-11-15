import { expect, test } from "@playwright/test";
const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1280, 390]) {
  test(`feedback preferences save and reload (${locale}, ${width}px)`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN";
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(language => {
      localStorage.setItem("smartai_token", "synthetic-token");
      localStorage.setItem("smartai_locale", language);
    }, locale);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    let revision = 1, saves = 0;
    let config = { schema_version: 1, selected_provider_ids: ["model-a"], primary_provider_id: "model-a", aggregation_method: "single",
      multi_sample_n: 1, knowledge_scope: "none", feedback_tone: "neutral", feedback_length: "medium", feedback_language: "zh",
      suggest_corrections: true, low_confidence_threshold: .6 };
    await page.route(`${backend}/**`, async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-token" });
      if (path === "/tasks/feedback/grading-setup") {
        if (req.method() === "PUT") { config = req.postDataJSON().grading_setup; revision++; saves++; }
        return json({ task_id: "feedback", task_status: "submissions_ready", workflow_revision: revision, configured: true,
          grading_setup: config, suggested_setup: config, grading_setup_fingerprint: JSON.stringify(config),
          available_experts: [{ provider_id: "model-a", provider_type: "openai", model: "gpt-test", display_name: "Grading model", enabled: true,
            scope: "owner", is_shared: false, editable: true, max_concurrent: 1, rpm: 10 }],
          knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: 0, task_docs: [] },
          readiness: { ready: true, blocking_issues: [], warnings: [] } });
      }
      if (path === "/tasks/feedback") return json({ task_id: "feedback", name: zh ? "数学周测" : "Math quiz", status: "submissions_ready",
        workflow_revision: revision, problem_data: {}, student_data: {}, problem_count: 1, student_count: 1, kb_docs: {} });
      if (path.endsWith("/kb")) return json({ docs: [] });
      if (path === "/course-materials/") return json({ items: [] });
      if (path === "/tasks/") return json({});
      return json([]);
    });
    const url = "/tasks/feedback/grading-setup?teacher_notes=Q2&strictness=100&allow_partial_credit=false";
    await page.goto(url);
    const open = () => page.getByRole("button", { name: zh ? "展开高级设置" : "Show advanced settings" }).click();
    await open();
    const tone = page.getByRole("combobox", { name: zh ? "评语语气" : "Feedback tone" });
    const length = page.getByRole("combobox", { name: zh ? "评语长度" : "Feedback length" });
    const language = page.getByRole("combobox", { name: zh ? "评语语言" : "Feedback language" });
    await tone.selectOption("encouraging");
    await length.selectOption("long");
    await language.selectOption(zh ? "zh" : "en");
    await expect(page.getByRole("textbox", { name: /批改注意事项|Teacher notes/ })).toHaveCount(0);
    await expect(page.getByRole("slider")).toHaveCount(0);
    await page.getByRole("button", { name: zh ? "保存并查看批改摘要" : "Save & Review Grading" }).click();
    await expect(page).toHaveURL(/grading\/preflight/);
    expect(saves).toBe(1);
    expect(config).toMatchObject({ feedback_tone: "encouraging", feedback_length: "long", feedback_language: zh ? "zh" : "en" });
    expect(config).not.toHaveProperty("teacher_notes");
    expect(config).not.toHaveProperty("strictness");
    expect(config).not.toHaveProperty("allow_partial_credit");
    await page.goto("/tasks/feedback/grading-setup");
    await open();
    await expect(tone).toHaveValue("encouraging");
    await expect(length).toHaveValue("long");
    await expect(language).toHaveValue(zh ? "zh" : "en");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("feedback-settings-after.png"), fullPage: true });
    for (const retired of ["setup", "upload/submissions", "upload/answers"]) {
      await page.goto(`/tasks/feedback/${retired}`);
      await expect(page.getByText(zh ? "页面不存在" : "Page not found", { exact: true })).toBeVisible();
    }
    expect(saves).toBe(1);
    expect(errors).toEqual([]);
  });
}
