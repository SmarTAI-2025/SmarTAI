import { test, expect } from "@playwright/test";

const pages = [
  ["problems/progress", "extracting_problems"],
  ["submissions/progress", "parsing_submissions"],
  ["grading/progress", "grading"],
  ["questions/import/progress/job", "problems_ready"],
  ["questions/ai-complete/progress/job", "problems_ready"],
  ["submissions/upload", "error"],
] as const;

for (const width of [1440, 390]) for (const [path, status] of pages) {
  test(`${path}: same-page cards align at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 950 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const provider = { provider_id: "provider", provider_type: "gemini", model: "synthetic", enabled: true, is_default: true };
    const task = { task_id: "width-task", name: "尺寸检查", status, workflow_revision: 1, active_job_id: status === "error" ? null : "job",
      active_operation_status: "pending", queue_reason: "server_busy", extract_job_id: "job", parse_job_id: "job", grading_job_id: "job",
      question_recognition_provider_id: "provider", submission_recognition_provider_id: "provider", problem_data: {}, student_data: {},
      problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0,
      submission_sources: status === "error" ? [{ source_id: "source", file_name: "synthetic.txt", status: "failed",
        mime_type: "text/plain", file_size: 10, unknown_question_ids: [], matched_question_ids: [], recognized_question_count: 0,
        error_code: "provider_response_invalid", failure_stage: "recognition", retryable: true }] : [],
    };
    const progress = { job_id: "job", phase: "parsing", total_students: 1, total_questions: 1, completed_units: 0,
      total_steps: 4, completed_steps: 0, current_step: "preparing_files", active: [], messages: [] };
    await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-width-token"); localStorage.setItem("smartai_locale", "zh-CN"); });
    const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), url = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (url === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
      if (url === "/auth/activity") return json({ token: "synthetic-width-token" });
      if (url.startsWith("/experts")) return json([provider]);
      if (url.endsWith("/state")) return json({ ...task, progress });
      if (url.endsWith("/input")) return json({ task_id: task.task_id, workflow_revision: 1, input: null });
      if (url.endsWith("/job")) return json({ job_id: "job", status: "running", progress });
      if (url === "/tasks/width-task") return json(task);
      return json([]);
    });
    await page.goto(`/tasks/width-task/${path}`);
    const controls = path.endsWith("upload")
      ? page.getByRole("heading", { name: "本批文件识别结果", exact: true }).locator("xpath=ancestor::section[1]")
      : page.getByRole("button", { name: "停止运行", exact: true }).locator("..");
    const content = path.endsWith("upload") ? page.locator('[data-draft-width="900"]')
      : page.locator("main h2").first().locator("xpath=ancestor::section[1]");
    await expect(controls).toBeVisible();
    await expect(content).toBeVisible();
    const a = await controls.boundingBox(), b = await content.boundingBox();
    expect(a).not.toBeNull(); expect(b).not.toBeNull();
    expect(Math.abs(a!.x - b!.x)).toBeLessThan(1);
    expect(Math.abs(a!.width - b!.width)).toBeLessThan(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `output/playwright/width-${path.replaceAll("/", "-")}-${width}.png`, fullPage: true });
    expect(errors).toEqual([]);
  });
}
