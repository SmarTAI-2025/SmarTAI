import { expect, test } from "@playwright/test";

for (const stage of ["submissions", "grading"] as const) test(`${stage}: failure page identifies the affected response`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: stage === "grading" ? 1440 : 390, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const correction = { student_id: "S1", q_id: "q1", type: "concept", score: null, max_score: 10, confidence: 0,
    comment: "", steps: [], requires_human_review: true, review_status: "pending", review_reasons: ["all_failed", "parse_failed"],
    expert_results: [{ provider: "Gemini", score: 0, max_score: 10, confidence: 0, error_kind: "parse_failed", comment: "", steps: [] }] };
  const sources = [{ source_id: "src1", file_id: "file1", file_name: "样例作答.pdf", content_type: "application/pdf", size_bytes: 1000,
    status: "failed", internal_status: "parse_failed", retryable: true, reason_code: "parse_failed", failure_phase: "parse", matched_answer_count: 1,
    unknown_question_ids: [], student_candidate: "样例学生", answer_diagnostic_count: 1, answer_diagnostics: [{ q_id: "q1", number: "1.10", has_content: true,
      recognition_confidence: .4, flag: ["recognition_failed", "recognition_manual_pending", "recognition_invalid_fields:content"] }], job_id: "failed", attempt: 1, created_at: 1 }];
  const task = { task_id: "failure-details", name: "失败明细", status: "error", workflow_revision: 2, error: "provider_response_invalid",
    last_failed_job_id: "failed", extract_job_id: "extract", parse_job_id: stage === "submissions" ? "failed" : "parse",
    grading_job_id: stage === "grading" ? "failed" : null, active_job_id: null, grading_setup_configured: stage === "grading",
    problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {},
    problem_data: { q1: { q_id: "q1", number: "1.10", type: "concept", stem: "样例题干", criterion: "正确得10分。", max_score: 10, reference_answer: "参考答案" } },
    student_data: { S1: { stu_id: "S1", stu_name: "样例学生", stu_ans: [{ q_id: "q1", number: "1.10", type: "concept", content: "识别片段", flag: [] }] } },
    submission_sources: stage === "submissions" ? sources : [],
    submission_source_summary: { total: 1, parsed: 0, failed: 1, pending: 0, identity_needs_review: 0 },
  };
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic" });
    if (path.startsWith("/experts")) return json([]);
    if (path.endsWith("/state")) return json({ ...task, progress: { phase: "error", total_questions: 1, total_students: 1,
      completed_units: 0, messages: [], error_detail: task.error,
      grading_review: [correction], grading_review_summary: { affected: 1, missing_scores: 1 } } });
    if (path === "/tasks/failure-details") return json(task);
    return json({});
  });
  await page.goto(`/tasks/failure-details/${stage}/progress`);
  await expect(page).toHaveTitle("SmarTAI");
  await expect(page.getByRole("button", { name: "人工继续处理", exact: true })).toBeVisible();
  if (stage === "submissions") {
    await expect(page.getByText("第 1.10 题", { exact: true })).toBeVisible();
    await expect(page.getByText(/需处理：待核对：作答内容/)).toHaveClass(/text-red-700/);
    await expect(page.getByText(/建议复核：置信度 0.4/)).toHaveClass(/text-amber-700/);
    await expect(page.getByText(/已识别身份：样例学生/)).toBeVisible();
  } else {
    const issues = page.getByRole("region", { name: "批改问题明细" });
    await expect(issues).toContainText("样例学生 · 第 1.10 题");
    await expect(issues).toContainText("Gemini：模型返回的评分格式无法读取");
    await expect(issues).toContainText("1 份缺少有效分数");
  }
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`${stage}-failure-details.png`), fullPage: true });
  expect(errors).toEqual([]);
});
