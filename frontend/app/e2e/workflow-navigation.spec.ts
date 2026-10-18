import { test, expect, type Page } from "@playwright/test";
import type { ProblemInfo, SubmissionSourceOutcome, SubmissionSourceSummary } from "../src/types";

type Stage = "problems" | "submissions" | "grading";
const paths = { problems: "upload/problems", submissions: "submissions/upload", grading: "grading/preflight" };
const next = { problems: "questions", submissions: "submissions", grading: "review" };
const submissionSource = (status: SubmissionSourceOutcome["status"]): SubmissionSourceOutcome => ({
  source_id: "source-1", file_id: "file-1", file_name: "synthetic.txt", content_type: "text/plain", size_bytes: 20,
  status, internal_status: status === "processing" ? "pending" : "parsed", retryable: false,
  matched_answer_count: status === "processing" ? 0 : 1, unknown_question_ids: [], job_id: "new-job", attempt: 1, created_at: 1,
  reason_code: status === "identity_needs_review" ? "identity_needs_review" : null,
});

// Real production router/query/draft lifecycles, synthetic API only: no model calls.
async function fixture(page: Page, stage: Stage) {
  const calls: string[] = [], errors: string[] = [];
  let detailDelay = 0;
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const provider = { provider_id: "test-provider", provider_type: "openai", model: "synthetic", enabled: true,
    is_default: true, is_shared: false, scope: "owner", editable: true, max_concurrent: 1, rpm: 10 };
  const activity = { sequence: 1, ts: Date.now() / 1000, code: "responding", model_id: "model-1" };
  const problem: ProblemInfo = { q_id: "q1", number: "1", type: "short_answer", stem: "What is 6 × 7?", max_score: 1,
    reference_answer: "42", criterion: "One point for 42.", review_status: "confirmed" };
  const student = { stu_id: "s1", stu_name: "Synthetic student", identity_status: "matched", identity_review_status: "confirmed",
    stu_ans: [{ q_id: "q1", content: "42", review_status: "confirmed", flag: [] as string[] }] };
  const task = { task_id: "navigation-task", name: "自动跳转验证", status: stage === "problems" ? "draft" : stage === "submissions" ? "problems_ready" : "submissions_ready",
    workflow_revision: 1, extract_job_id: stage === "problems" ? null : "old-extract", parse_job_id: stage === "grading" ? "old-parse" : null,
    grading_job_id: null as string | null, active_job_id: null as string | null, active_operation_status: "pending",
    question_recognition_provider_id: provider.provider_id, submission_recognition_provider_id: provider.provider_id,
    grading_setup_configured: stage === "grading", error: null, last_failed_job_id: null,
    problem_count: stage === "problems" ? 0 : 1, student_count: stage === "grading" ? 1 : 0,
    problem_data: stage === "problems" ? {} : { q1: problem }, student_data: stage === "grading" ? { s1: student } : {},
    submission_sources: [] as SubmissionSourceOutcome[], submission_source_summary: null as SubmissionSourceSummary | null, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  const setup = { schema_version: 1, selected_provider_ids: [provider.provider_id], primary_provider_id: provider.provider_id,
    aggregation_method: "single", multi_sample_n: 1, knowledge_scope: "none", strictness: 75, allow_partial_credit: true,
    feedback_tone: "neutral", feedback_length: "medium", feedback_language: "zh", suggest_corrections: true,
    low_confidence_threshold: 0.6, teacher_notes: "" };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-navigation-token"); localStorage.setItem("smartai_locale", "zh-CN"); });
  await page.addInitScript(() => {
    const paths: string[] = [];
    Object.assign(window, { navigationPaths: paths });
    const replace = history.replaceState.bind(history);
    history.replaceState = (data, unused, url) => { paths.push(String(url)); replace(data, unused, url); };
  });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-navigation-token" });
    if (path.endsWith("/content-language/default")) return json({ language: "zh" });
    if (path.startsWith("/experts")) return json([provider]);
    if (path === "/tags/") return json([]);
    if (path === "/tasks/") return json({ items: [task], total: 1, page: 1, page_size: 20,
      facets: { semesters: [], courses: [], tags: [], statuses: [] } });
    if (request.method() === "PUT") {
      calls.push(path);
      const body = request.postDataJSON();
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      task.workflow_revision += 1;
      if (path.endsWith("/identity")) {
        student.identity_status = "matched";
        student.stu_name = body.student_name;
        return json({ status: "ok", previous_student_id: student.stu_id, student, workflow_revision: task.workflow_revision });
      }
      if (path.endsWith("/answers/q1")) {
        student.stu_ans[0].review_status = body.review_status;
        return json({ status: "ok", answer: student.stu_ans[0], workflow_revision: task.workflow_revision });
      }
      throw new Error(`Unexpected mutation: ${path}`);
    }
    if (request.method() === "POST") {
      calls.push(path);
      if (path.endsWith("/sources/preflight")) return json({ source_token: "prepared-source", source: { stored_file_id: "stored" } });
      task.status = stage === "problems" ? "extracting_problems" : stage === "submissions" ? "parsing_submissions" : "grading";
      task.active_operation_status = "running";
      task.active_job_id = "new-job"; task.workflow_revision += 1;
      if (stage === "problems") task.extract_job_id = "new-job";
      if (stage === "submissions") {
        task.parse_job_id = "new-job";
        task.submission_sources = [submissionSource("processing")];
        task.submission_source_summary = { uploaded: 1, parsed: 0, failed: 0, identity_needs_review: 0, pending: 1 };
      }
      if (stage === "grading") task.grading_job_id = "new-job";
      return json({ status: "started", task_id: task.task_id, job_id: "new-job", workflow_revision: task.workflow_revision });
    }
    if (path.endsWith("/input")) {
      // Previously this request unmounted the submitting form before mutateAsync resumed.
      if (task.active_job_id) await new Promise(resolve => setTimeout(resolve, 400));
      return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, input: null });
    }
    if (path.endsWith("/question-preparation/capabilities")) return json({
      source_roles: Object.fromEntries(["problem", "reference_answer", "rubric", "programming_tests"].map(role => [role, { accepted_extensions: [".pdf", ".txt"] }])),
      reader: { ocr: true }, limits: { max_file_bytes: 5242880 }, score_policy: { maximum_max_score: 10000, per_question_text_max_characters: 12000 } });
    if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, task_status: task.status, workflow_revision: task.workflow_revision,
      configured: true, grading_setup: setup, available_experts: [provider], knowledge: { scope_options: ["none"], task_doc_count: 0, task_docs: [] },
      readiness: { ready: true, blocking_issues: [], warnings: [] } });
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    if (path.endsWith("/grading/activity")) return json({ run_id: task.grading_job_id, events: [activity], cursor: activity.sequence, has_more: false });
    if (path.endsWith("/state")) return json({ ...task, progress: { job_id: task.active_job_id, phase: task.active_job_id ? "processing" : "done",
      current_step: "preparing_files", total_steps: 4, completed_steps: task.active_job_id ? 0 : 4, total_students: task.student_count,
      total_questions: task.problem_count, completed_units: 0, active: [],
      messages: [{ ts: activity.ts, level: "info", message: "activity_responding" }],
      ...(stage === "grading" ? { activity_latest: activity, activity_cursor: 1,
        model_progress: [{ id: "model-1", label: "synthetic", total: 1, succeeded: 0, failed: 0,
          cancelled: 0, processing: 0, queued: 0, responding: 1, retrying: 0, completion_times: [] }] }
        : { recognition_requests: [{ state: "responding", count: 1 }] }) } });
    if (path === `/tasks/${task.task_id}`) {
      if (detailDelay) await new Promise(resolve => setTimeout(resolve, detailDelay));
      return json(task);
    }
    if (path.endsWith("/result")) return json({ task_id: task.task_id, result: {}, review_items: [] });
    if (path.endsWith("/finalization")) return json({ task_id: task.task_id, remaining_review_count: 0, ready_for_confirmation: true });
    return json({ items: [], total: 0 });
  });
  return { calls, errors, task, student, complete() {
    detailDelay = 700;
    task.status = stage === "problems" ? "problems_ready" : stage === "submissions" ? "submissions_ready" : "graded";
    task.active_job_id = null; task.problem_count = 1; task.problem_data = { q1: problem };
    if (stage !== "problems") { task.student_count = 1; task.student_data = { s1: student }; }
    if (stage === "submissions") {
      student.identity_status = "needs_review";
      student.stu_ans[0].review_status = "pending";
      student.stu_ans[0].flag = ["recognition_needs_review"];
      task.submission_sources = [submissionSource("identity_needs_review")];
      task.submission_source_summary = { uploaded: 1, parsed: 0, failed: 0, identity_needs_review: 1, pending: 0 };
    }
    task.workflow_revision += 1;
  } };
}

test("empty question text blocks both countdown and manual grading without a mutation", async ({ page }) => {
  const run = await fixture(page, "grading");
  run.task.problem_data.q1!.stem = "";
  await page.goto("/tasks/navigation-task/grading/preflight");
  await expect(page.getByText("题目存在关键缺项或冲突。请修正并确认后再开始批改。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "立即开始批改", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await expect(page).toHaveURL(/\/grading\/preflight$/);
  expect(run.calls).toEqual([]);
  expect(run.errors).toEqual([]);
});

for (const mode of ["manual", "all"] as const) test(`${mode} identity and answer confirmations update the matrix and all-confirmed state immediately`, async ({ page }) => {
  const run = await fixture(page, "grading");
  run.student.identity_status = "needs_review";
  run.student.stu_ans[0].review_status = "pending";
  await page.goto("/tasks/navigation-task/submissions");
  await expect(page.getByText("待确认：1 位学生身份、1 个作答题次")).toBeVisible();
  await page.getByRole("link", { name: "查看", exact: true }).click();
  await expect(page.locator('[data-question-id="q1"]').getByRole("button", { name: /待确认/ })).toHaveCount(1);
  if (mode === "manual") {
    await page.getByRole("button", { name: "确认身份 · 待确认", exact: true }).click();
    await expect(page.getByRole("button", { name: "确认身份 · 已确认", exact: true })).toBeDisabled();
    await page.locator('[data-question-id="q1"]').getByRole("button", { name: /待确认/ }).click();
  } else {
    await page.getByRole("button", { name: "全部确认", exact: true }).click();
    await expect(page.getByRole("button", { name: "确认身份 · 已确认", exact: true })).toBeDisabled();
  }
  await expect(page.locator('[data-question-id="q1"]').getByRole("button", { name: /已确认/ })).toHaveCount(1);
  await page.getByRole("link", { name: "返回作答总览", exact: true }).click();
  await expect(page.getByText("待确认：0 位学生身份、0 个作答题次")).toBeVisible();
  await expect(page.getByRole("region", { name: "待复核队列" })).toContainText("当前没有待处理项");
  await expect(page.getByRole("button", { name: "全部确认", exact: true })).toHaveCount(0);
  await expect(page.locator('[data-review-stage-actions]')).toContainText("进入批改设置");
  await page.screenshot({ path: `output/playwright/navigation-review-${mode}-confirmed.png`, fullPage: true });
  expect(run.calls).toHaveLength(2);
  expect(run.errors).toEqual([]);
});

for (const stage of ["problems", "submissions", "grading"] as const) {
  test(`${stage}: history opens completed work while the detail cache still says running`, async ({ page }) => {
    const run = await fixture(page, stage);
    run.task.status = stage === "problems" ? "extracting_problems" : stage === "submissions" ? "parsing_submissions" : "grading";
    run.task.active_job_id = "history-job";
    await page.goto(`/tasks/navigation-task/${stage}/progress`);
    await expect(page.locator("main h1")).toBeVisible();
    await page.getByRole("link", { name: "历史任务", exact: true }).first().click();
    run.complete();
    const open = page.getByRole("link", { name: "自动跳转验证", exact: true });
    await expect(open).toHaveAttribute("href", `/tasks/navigation-task/${next[stage]}`);
    await open.click();
    await expect(page).toHaveURL(new RegExp(`/${next[stage]}$`));
    await expect(page.locator("main h1")).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { navigationPaths: string[] }).navigationPaths.length)).toBeLessThan(8);
    expect(run.calls).toEqual([]);
    expect(run.errors).toEqual([]);
  });

  test(`${stage}: one start click → progress → next page; history reopens without reload`, async ({ page }) => {
    const run = await fixture(page, stage);
    await page.goto(`/tasks/navigation-task/${paths[stage]}`);
    if (stage !== "grading") {
      await page.locator('input[type="file"]').first().setInputFiles({ name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("1. What is 6 × 7?") });
      await expect(page.getByLabel(stage === "problems" ? "题目识别模型" : "作答识别模型", { exact: true })).toHaveValue("test-provider");
    }
    await page.getByRole("button", { name: stage === "problems" ? "识别并准备题目资料" : stage === "submissions" ? "开始识别作答" : "立即开始批改", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/${stage}/progress$`));
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    // All three first-start paths must expose activity before stopping, resuming or reloading.
    const activityToggle = page.getByRole("button", { name: /最近动态/ });
    if (stage === "grading") {
      await expect(activityToggle).toBeVisible();
      await expect(activityToggle).toHaveAttribute("aria-expanded", "false");
      await expect(activityToggle).toContainText("请求已发出，等待模型响应");
      await expect(page.getByLabel("各模型进度")).toContainText("synthetic：已处理 0 / 1，有效结果 0 · 等待模型响应 1");
      await activityToggle.click();
    } else {
      await expect(page.getByRole("heading", { name: "最近动态" })).toBeVisible();
      await expect(activityToggle).toHaveCount(0);
      await expect(page.getByText("1 个请求等待模型响应", { exact: true })).toBeVisible();
    }
    const activityHistory = page.getByRole("region", { name: stage === "grading" ? "批改动态历史" : "识别动态历史" });
    await expect(activityHistory).toBeVisible();
    await expect(activityHistory.getByRole("listitem")).toHaveCount(1);
    await expect(activityHistory).toContainText("请求已发出，等待模型响应");
    if (stage === "grading") {
      await activityToggle.click();
      await expect(activityHistory).toHaveCount(0);
    }
    await page.screenshot({ path: `output/playwright/navigation-${stage}-progress.png`, fullPage: true });
    run.complete();
    await expect(page).toHaveURL(new RegExp(`/${next[stage]}$`), { timeout: 10_000 });
    await expect(page.locator("main h1")).toBeVisible();
    if (stage === "submissions") {
      await expect(page.getByText("待确认：1 位学生身份、1 个作答题次")).toBeVisible();
      await expect(page.getByRole("button", { name: "重试失败项", exact: true })).toHaveCount(0);
      await page.screenshot({ path: `output/playwright/navigation-submissions-completed-needs-review.png`, fullPage: true });
    }
    expect(await page.evaluate(() => (window as unknown as { navigationPaths: string[] }).navigationPaths.length)).toBeLessThan(8);
    await page.getByRole("link", { name: "历史任务", exact: true }).first().click();
    await page.getByRole("link", { name: "自动跳转验证", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/${next[stage]}$`));
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.getByText("页面暂时无法显示", { exact: true })).toHaveCount(0);
    expect(run.calls).toHaveLength(stage === "problems" ? 2 : 1);
    expect(run.errors).toEqual([]);
  });
}
