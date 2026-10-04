import { test, expect, type Page } from "@playwright/test";

// Synthetic API responses only. Never use a user's task, key or paid endpoint.
async function fixture(page: Page, stage: "problems" | "submissions" | "grading", code = "provider_request_rejected", startSucceeds = false) {
  const calls: Array<{ path: string; body: string }> = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const providers = ["old", "new"].map((id) => ({ provider_id: id, provider_type: "openai", model: `synthetic-${id}`, enabled: true, is_default: id === "old", is_shared: false, scope: "owner", editable: true, max_concurrent: 1, rpm: 10, image_capability_status: "unverified" }));
  const task = { task_id: "recovery-task", name: "Synthetic recovery task", status: "error", workflow_revision: 7,
    last_failed_job_id: `${stage}-job`, extract_job_id: "problems-job", parse_job_id: stage !== "problems" ? "submissions-job" : null, grading_job_id: stage === "grading" ? "grading-job" : null,
    error: code, problem_count: stage === "problems" ? 0 : 1, student_count: stage === "grading" ? 1 : 0,
    problem_file_name: "saved.pdf", pending_submission_file_name: stage !== "problems" ? "saved.zip" : null,
    question_recognition_provider_id: "old", submission_recognition_provider_id: "old", grading_setup_configured: stage === "grading", problem_data: {}, submission_source_summary: null, submission_sources: [] };
  const setup = { schema_version: 1, selected_provider_ids: ["old"], primary_provider_id: "old", aggregation_method: "single", multi_sample_n: 1, knowledge_scope: "none", strictness: 75, allow_partial_credit: true, feedback_tone: "neutral", feedback_length: "medium", feedback_language: "zh", suggest_corrections: true, low_confidence_threshold: 0.6, teacher_notes: "原批改说明" };
  const setupResponse = { task_id: task.task_id, task_status: task.status, workflow_revision: 7, configured: true, grading_setup: setup, suggested_setup: null, grading_setup_fingerprint: "synthetic", grading_setup_updated_at: 1,
    available_experts: providers, knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: 0, task_docs: [] }, readiness: { ready: true, blocking_issues: [], warnings: [] } };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-recovery-token"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend.replace(/\/$/, "")}/**`, async (route) => {
    const req = route.request(), path = new URL(req.url()).pathname;
    const method = req.method();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (method === "POST" || method === "PUT") {
      calls.push({ path, body: req.postData() ?? "" });
      if (path.endsWith("/question-preparation/sources/preflight")) return json({ source_token: "new-source", source: { stored_file_id: "stored-pdf" } });
      if (startSucceeds) {
        task.status = stage === "problems" ? "extracting_problems" : stage === "submissions" ? "parsing_submissions" : "grading";
        task.error = ""; task.workflow_revision += 1;
        return json({ status: "started", task_id: task.task_id, job_id: `${stage}-restarted`, workflow_revision: task.workflow_revision });
      }
      return json({ detail: { code } }, 422);
    }
    if (path === "/auth/me") return json({ id: "synthetic-teacher", username: "test", email: "synthetic@example.test", role: "teacher", is_active: true, created_at: 0 });
    if (path.startsWith("/experts/")) return json(providers);
    if (path.endsWith("/question-preparation/input")) return json({ task_id: task.task_id, workflow_revision: 7, input: { job_id: "problems-job", recognition_provider_id: "old", score_policy: { mode: "uniform", uniform_max_score: 15 }, sources: [
      { source_token: "old-source", role: "problem", source_kind: "upload", filename: "saved.pdf", stored_file_id: "stored-pdf", library_material_id: null, inline_text: "", structure_mode: "extract_from_source", extraction_hint: "保留图表\n页码: 3-5\n题号: 1.1.5", recognition_options: { pages: [3, 4, 5], targets: ["1.1.5"] }, enable_material_ocr: false, save_to_library: false, available: true },
    ] } });
    if (path.endsWith("/submission-recognition/input")) return json({ task_id: task.task_id, workflow_revision: 7, input: { job_id: "submissions-job", stored_file_id: "stored-zip", filename: "saved.zip", available: true, identity_mode: "roster", roster_name: "saved-roster.csv", roster_count: 2, recognition_provider_id: "old" } });
    if (path.endsWith("/draft-reference")) return json({ available: true, prepared: false, filename: "saved.pdf" });
    if (path.endsWith("/question-preparation/capabilities")) return json({ source_roles: Object.fromEntries(["problem", "reference_answer", "rubric", "programming_tests"].map((role) => [role, { accepted_extensions: [".pdf", ".txt", ".png"] }])), reader: { ocr: true }, limits: { max_file_bytes: 5242880 }, score_policy: { maximum_max_score: 10000, per_question_text_max_characters: 12000 } });
    if (path.endsWith("/grading-setup")) return json(setupResponse);
    if (path === `/tasks/${task.task_id}/state`) return json({ ...task, progress: { phase: task.status === "error" ? "error" : "processing", current_step: stage, error_detail: task.error, messages: [] } });
    if (path === `/tasks/${task.task_id}`) return json(task);
    if (path.endsWith("/kb")) return json({ docs: [] });
    return json({ items: [], total: 0 });
  });
  page.on("dialog", (dialog) => void dialog.accept());
  return { calls, errors };
}

test("mobile recovery actions stay visible and do not overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { calls, errors } = await fixture(page, "submissions", "provider_request_rejected");
  await page.goto("/tasks/recovery-task/submissions/progress");
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回修改配置" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "output/playwright/workflow-mobile-error.png", fullPage: true });
  expect(calls).toHaveLength(0); expect(errors).toEqual([]);
});

test("restored question inputs can start a new job only after manual continuation", async ({ page }) => {
  const { calls, errors } = await fixture(page, "problems", "provider_timeout", true);
  await page.goto("/tasks/recovery-task/problems/progress");
  await page.getByRole("link", { name: "返回修改配置" }).click();
  await expect(page.getByLabel("页码（选填）")).toHaveValue("3, 4, 5");
  expect(calls).toHaveLength(0);
  await page.getByRole("button", { name: "重新识别全部资料" }).click();
  await expect(page).toHaveURL(/\/problems\/progress$/);
  await expect(page.getByRole("heading", { name: "题目资料准备进度" })).toBeVisible();
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toHaveCount(0);
  expect(calls).toHaveLength(2);
  expect(calls[0].body).toContain("stored-pdf");
  expect(calls[0].body).not.toContain('filename="saved.pdf"');
  expect(errors).toEqual([]);
});

test("question failure → model change → restored original form → manual continue", async ({ page }) => {
  const { calls, errors } = await fixture(page, "problems");
  await page.goto("/tasks/recovery-task/problems/progress");
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "检查 BYOK 配置" })).toBeVisible();
  await page.getByRole("combobox", { name: "题目识别模型" }).selectOption("new");
  await page.screenshot({ path: "output/playwright/workflow-question-error.png", fullPage: true });
  await page.getByRole("link", { name: "返回修改配置" }).click();
  await expect(page.getByLabel("题目识别模型")).toHaveValue("new");
  await expect(page.getByLabel("页码（选填）")).toHaveValue("3, 4, 5");
  await expect(page.getByLabel("目标题号（选填）")).toHaveValue("1.1.5");
  await expect(page.getByLabel("补充说明（选填）")).toHaveValue("保留图表");
  expect(calls).toHaveLength(0);
  await page.screenshot({ path: "output/playwright/workflow-question-restored.png", fullPage: true });
  await page.getByRole("button", { name: "重新识别全部资料" }).click();
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toBeVisible();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls[0].body).toContain("stored-pdf");
  expect(calls[0].body).not.toContain('filename="saved.pdf"');
  expect(calls[1].body).toContain('"recognition_provider_id":"new"');
  expect(errors).toEqual([]);
});

test("manual previous-step navigation restores submission archive, roster and options", async ({ page }) => {
  const { calls, errors } = await fixture(page, "submissions", "provider_auth_failed");
  await page.goto("/tasks/recovery-task/submissions/progress");
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "作答识别模型" }).selectOption("new");
  await page.getByRole("link", { name: "上传作答", exact: true }).click();
  await expect(page.getByLabel("作答识别模型")).toHaveValue("new");
  await expect(page.getByText("saved.zip", { exact: true })).toBeVisible();
  await expect(page.getByText("saved-roster.csv (2)", { exact: true })).toBeVisible();
  await expect(page.getByRole("radio", { name: "导入名单" })).toHaveAttribute("aria-checked", "true");
  expect(calls).toHaveLength(0);
  await page.screenshot({ path: "output/playwright/workflow-submissions-restored.png", fullPage: true });
  await page.getByRole("button", { name: "按当前配置重试", exact: true }).click();
  await expect(page.getByRole("heading", { name: "模型密钥或授权无效" })).toBeVisible();
  expect(calls).toHaveLength(1);
  expect(calls[0].body).toContain("stored-zip");
  expect(calls[0].body).toContain("submissions-job");
  expect(errors).toEqual([]);
});

test("grading error keeps retry and configuration recovery with saved model and notes", async ({ page }) => {
  const { calls, errors } = await fixture(page, "grading", "provider_upstream_unavailable");
  await page.goto("/tasks/recovery-task/grading/progress");
  await expect(page.getByRole("button", { name: "按当前配置重试", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "返回修改配置" }).click();
  await expect(page.getByText("synthetic-old", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("slider").first()).toHaveValue("75");
  await page.getByRole("button", { name: /展开高级/ }).click();
  await expect(page.getByRole("textbox", { name: /批改注意事项/ })).toHaveValue("原批改说明");
  await page.screenshot({ path: "output/playwright/workflow-grading-restored.png", fullPage: true });
  expect(calls).toHaveLength(0);
  expect(errors).toEqual([]);
});

for (const code of ["provider_rate_limited", "provider_timeout", "source_empty", "unknown_failure"]) {
  test(`fallback controls remain usable for ${code}`, async ({ page }) => {
    const { calls, errors } = await fixture(page, "problems", code);
    await page.goto("/tasks/recovery-task/problems/progress");
    await expect(page.getByRole("link", { name: "返回修改配置" })).toBeVisible();
    await page.getByRole("button", { name: "按当前配置重试", exact: true }).dblclick();
    await expect.poll(() => calls.length).toBeGreaterThan(0);
    expect(calls[0].body).toContain('"use_current_configuration":true');
    expect(errors).toEqual([]);
  });
}
