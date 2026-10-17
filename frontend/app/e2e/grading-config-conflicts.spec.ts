import { expect, test } from "@playwright/test";
import type { GradingSetup } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
for (const width of [1440, 390]) {
  test(`${width}px: library changes can be drafted and saved without false conflicts`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    const doc = { doc_id: "doc-1", filename: "Calculus notes.pdf", library_material_id: "material-1", chunk_count: 4, uploaded_at: 1 };
    let docs: typeof doc[] = [], revision = 1, saves = 0;
    let saved: GradingSetup | null = null;
    let taskRefresh: Promise<void> | undefined;
    const defaults: GradingSetup = {
      schema_version: 1, selected_provider_ids: ["provider-1"], primary_provider_id: "provider-1",
      aggregation_method: "single", expert_weights: {}, multi_sample_n: 1, knowledge_scope: "none",
      strictness: 50, allow_partial_credit: true, feedback_tone: "neutral", feedback_length: "medium",
      feedback_language: "zh", suggest_corrections: true, low_confidence_threshold: .6, teacher_notes: "",
    };
    const payload = () => ({
      task_id: "config-task", task_status: saved ? "grading_setup" : "submissions_ready", workflow_revision: revision, configured: saved !== null,
      grading_setup: saved, suggested_setup: { ...defaults, knowledge_scope: docs.length ? "all_task_docs" : "none" },
      grading_setup_fingerprint: saved ? JSON.stringify(saved) : null,
      available_experts: [{ provider_id: "provider-1", provider_type: "openai", model: "example-model", display_name: "Grader", enabled: true,
        scope: "owner", is_shared: false, editable: true, max_concurrent: 1, rpm: 10 },
        // Every refresh changes this unselected model; it cannot invalidate edits.
        { provider_id: "unused", provider_type: "openai", model: `unused-${revision}`, display_name: "Other model", enabled: true,
          scope: "owner", is_shared: false, editable: true, max_concurrent: 1, rpm: 10 }],
      knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: docs.length, task_docs: docs },
      readiness: { ready: true, blocking_issues: [], warnings: [] },
    });
    await page.setViewportSize({ width, height: 1000 });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_locale", "zh-CN");
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
    });
    await page.route(`${backend}/**`, async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "config-teacher", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path.startsWith("/experts") || path === "/tags/") return json([]);
      if (path === "/tasks/") return json({ "config-task": { task_id: "config-task", name: "微积分周测", status: saved ? "grading_setup" : "submissions_ready" } });
      if (path === "/tasks/config-task/grading-setup") {
        if (req.method() === "PUT") {
          const body = req.postDataJSON(); expect(body.expected_workflow_revision).toBe(revision);
          saved = body.grading_setup; revision++; saves++;
        }
        return json(payload());
      }
      if (path === "/tasks/config-task/kb") {
        if (req.method() === "POST") { docs = [doc]; revision++; return json({ ...doc, workflow_revision: revision }); }
        return json({ docs });
      }
      if (path === "/tasks/config-task/kb/doc-1" && req.method() === "DELETE") { docs = []; revision++; return json({ status: "deleted", workflow_revision: revision }); }
      if (path === "/course-materials/") return json({ items: [{ material_id: "material-1", filename: doc.filename, category: "lecture" }] });
      if (path === "/tasks/config-task") {
        await taskRefresh;
        return json({ task_id: "config-task", owner_id: "config-teacher", name: "Grading configuration", status: saved ? "grading_setup" : "submissions_ready", workflow_revision: revision,
          problem_data: {}, student_data: {}, problem_count: 1, student_count: 1 });
      }
      if (path.endsWith("/finalization")) return json({ task_id: "config-task", workflow_revision: revision, remaining_review_count: 0 });
      throw new Error(`Unexpected request ${req.method()} ${path}`);
    });
    const conflicts = page.getByText(/任务或模型配置已更新|服务器业务版本已变化|请先核对服务器变化/);
    const draft = page.getByRole("button", { name: "暂存", exact: true });
    const submit = page.getByRole("button", { name: "保存并查看批改摘要" });
    const attach = async () => {
      await page.getByRole("searchbox", { name: "搜索课程资料库" }).click();
      await page.getByRole("button", { name: "选择", exact: true }).click();
      await expect(page.getByText(/已将.*加入本任务/)).toBeVisible();
    };
    await page.goto("/tasks/config-task/grading-setup");
    await expect(draft).toBeEnabled();
    const slider = page.getByRole("slider");
    for (let step = 0; step < 4; step++) await slider.press("ArrowRight");
    await attach();
    await expect(conflicts).toHaveCount(0);
    await draft.click();
    await expect(page.getByText(/已暂存 ·/)).toBeVisible();
    await page.getByRole("button", { name: /移除/ }).click();
    await expect(page.getByText("已选择 0/20 份")).toBeVisible();
    await draft.click();
    await expect(conflicts).toHaveCount(0);
    await expect(slider).toHaveValue("70");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("materials-removed-drafted.png"), fullPage: true });
    await page.goto("/history?view=drafts");
    const openDraft = page.getByRole("link", { name: "打开草稿 · 微积分周测 · 批改设置" });
    await expect(openDraft).toBeVisible();
    await expect(page.getByText(/暂存于/)).toBeVisible();
    await expect(page.getByText(/config-task/)).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("readable-draft-list.png"), fullPage: true });
    await openDraft.click();
    await expect(slider).toHaveValue("70");
    await expect(conflicts).toHaveCount(0);
    let release!: () => void;
    taskRefresh = new Promise(resolve => { release = resolve; });
    try {
      await submit.click();
      await expect.poll(() => saves).toBe(1);
      await expect(page.getByRole("button", { name: "正在保存…" })).toBeVisible();
      await expect(conflicts).toHaveCount(0);
    } finally { release(); taskRefresh = undefined; }
    await expect(page).toHaveURL(/grading\/preflight/);
    expect(saved).toMatchObject({ strictness: 70, knowledge_scope: "none" });
    // Simulate an answer-review revision before returning to the saved config.
    revision++;
    await page.goto("/tasks/config-task/grading-setup");
    await expect(slider).toHaveValue("70");
    await expect(draft).toBeEnabled();
    await attach();
    await draft.click();
    await expect(conflicts).toHaveCount(0);
    await submit.click();
    await expect(page).toHaveURL(/grading\/preflight/);
    expect(saved).toMatchObject({ strictness: 70, knowledge_scope: "all_task_docs" });
    expect(saves).toBe(2);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("grading-summary.png"), fullPage: true });
    expect(errors).toEqual([]);
  });
}
