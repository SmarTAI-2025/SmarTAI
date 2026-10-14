import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
for (const locale of ["zh-CN", "en-US"]) {
  test(`${locale}: custom model weights save, reload, and reset to confidence`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN", errors: string[] = [];
    let weights: Record<string, number> = {}, revision = 1, saved = 0;
    const setup = () => ({ schema_version: 1, selected_provider_ids: ["saved-a", "saved-b"], primary_provider_id: "saved-a", aggregation_method: "weighted_average",
      expert_weights: weights, multi_sample_n: 1, knowledge_scope: "none", strictness: 50, allow_partial_credit: true, feedback_tone: "neutral", feedback_length: "medium",
      feedback_language: "zh", suggest_corrections: true, low_confidence_threshold: .6, teacher_notes: "" });
    const payload = () => ({ task_id: "weight-task", task_status: "submissions_ready", workflow_revision: revision, configured: true, grading_setup: setup(),
      suggested_setup: setup(), grading_setup_fingerprint: `weights-${revision}`, available_experts: ["a", "b"].map(id => ({ provider_id: `saved-${id}`, provider_type: "openai", model: "example-model",
        display_name: `Model ${id.toUpperCase()}`, enabled: true, scope: "owner", is_shared: false, editable: true, max_concurrent: 1, rpm: 10 })),
      knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: 0, task_docs: [] }, readiness: { ready: true, blocking_issues: [], warnings: [] } });
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(locale => { localStorage.setItem("smartai_locale", locale); localStorage.setItem("smartai_token", "synthetic-teacher-token"); }, locale);
    await page.route(`${backend}/**`, async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "weight-teacher", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path.startsWith("/experts") || path === "/tags/") return json([]);
      if (path === "/tasks/weight-task/grading-setup") {
        if (req.method() === "PUT") { const body = req.postDataJSON(); expect(body.expected_workflow_revision).toBe(revision); weights = body.grading_setup.expert_weights; revision++; saved++; }
        return json(payload());
      }
      if (path === "/tasks/weight-task/kb") return json({ docs: [] });
      if (path === "/course-materials/") return json({ items: [] });
      if (path === "/tasks/weight-task") return json({ task_id: "weight-task", owner_id: "weight-teacher", name: "Weight configuration", status: "submissions_ready", workflow_revision: revision, problem_data: {}, student_data: {}, problem_count: 1, student_count: 1 });
      if (path.endsWith("/finalization")) return json({ task_id: "weight-task", workflow_revision: revision, remaining_review_count: 0 });
      throw new Error(`Unexpected request ${req.method()} ${path}`);
    });
    await page.goto("/tasks/weight-task/grading-setup");
    const custom = page.getByRole("checkbox", { name: zh ? "自定义模型权重" : "Custom model weights" });
    await expect(custom).not.toBeChecked();
    await expect(page.getByText(zh ? "默认：按置信度加权" : "Default: confidence weights")).toBeVisible();
    await custom.check();
    const second = page.getByRole("spinbutton", { name: `Model B ${zh ? "权重" : "Weight"}` });
    await second.fill("3");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("custom-weights.png"), fullPage: true });
    await page.getByRole("button", { name: zh ? "保存并查看批改摘要" : "Save & Review Grading" }).click();
    await expect(page).toHaveURL(/grading\/preflight/);
    expect(weights).toEqual({ "saved-a": 1, "saved-b": 3 });
    await page.goto("/tasks/weight-task/grading-setup");
    await expect(custom).toBeChecked(); await expect(second).toHaveValue("3");
    await custom.uncheck();
    await page.getByRole("button", { name: zh ? "保存并查看批改摘要" : "Save & Review Grading" }).click();
    await expect(page).toHaveURL(/grading\/preflight/);
    expect(weights).toEqual({}); expect(saved).toBe(2);
    expect(errors).toEqual([]);
  });
}
