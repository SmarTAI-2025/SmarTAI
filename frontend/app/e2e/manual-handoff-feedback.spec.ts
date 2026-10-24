import { expect, test } from "@playwright/test";

// Synthetic HTTP responses only: manual handoff must never submit model work.
for (const stage of ["questions", "submissions", "grading"] as const) {
  for (const locale of ["zh-CN", "en-US"] as const) {
    test(`${stage} ${locale}: slow manual handoff is visible, failure retries, then opens the original matrix`, async ({ page }, testInfo) => {
      const zh = locale === "zh-CN";
      await page.setViewportSize({ width: zh ? 1440 : 390, height: 900 });
      const errors: string[] = [], consoleErrors: string[] = [], mutations: string[] = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error" && !message.text().includes("422")) consoleErrors.push(message.text()); });
      const pathStage = stage === "questions" ? "problems" : stage;
      const progressPath = `/tasks/manual-feedback/${pathStage}/progress`;
      const matrixPath = `/tasks/manual-feedback/${stage === "grading" ? "review" : stage}`;
      const handoffPath = stage === "grading" ? "/tasks/manual-feedback/continue"
        : `/tasks/manual-feedback/${stage === "questions" ? "question-preparation" : "submission-recognition"}/failed-job/manual-review`;
      const problem = { q_id: "q1", number: "1", type: "concept", stem: "Saved original question", criterion: "One point.", max_score: 1,
        reference_answer: "Saved reference", preparation_issues: [], review_status: "needs_review" };
      const task = { task_id: "manual-feedback", name: "Manual recovery", status: zh ? `${pathStage}_paused` : "error",
        workflow_revision: 2, last_failed_job_id: "failed-job" as string | null, error: zh ? "operation_cancelled" as string | null : "provider_response_invalid",
        extract_job_id: stage === "questions" ? "failed-job" : "question-job", parse_job_id: stage === "submissions" ? "failed-job" : "answer-job",
        grading_job_id: stage === "grading" ? "failed-job" : null, grading_setup_configured: stage === "grading",
        question_recognition_provider_id: "provider", submission_recognition_provider_id: "provider",
        problem_count: 1, problem_data: { q1: problem }, student_count: stage === "questions" ? 0 : 1,
        student_data: stage === "questions" ? {} : { S1: { stu_id: "S1", stu_name: "Saved Student", stu_ans: [{ q_id: "q1", number: "1", type: "concept", content: "Saved original answer", flag: [] }] } },
        submission_sources: [], active_job_id: null, active_operation_status: null, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
      let release!: () => void;
      const slowRequest = new Promise<void>(resolve => { release = resolve; });
      await page.addInitScript(language => { localStorage.setItem("smartai_token", "synthetic"); localStorage.setItem("smartai_locale", language); }, locale);
      const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
      await page.route(`${backend}/**`, async route => {
        const request = route.request(), path = new URL(request.url()).pathname;
        const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
        if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
        if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
        if (path === "/auth/activity") return json({ token: "synthetic" });
        if (path.startsWith("/experts")) return json([{ provider_id: "provider", provider_type: "gemini", model: "synthetic", enabled: true }]);
        if (request.method() === "POST") {
          mutations.push(path);
          expect(path).toBe(handoffPath);
          expect(request.postDataJSON()).toMatchObject({ expected_workflow_revision: 2 });
          if (stage === "grading") expect(request.postDataJSON().manual).toBe(true);
          if (mutations.length === 1) {
            await slowRequest;
            return json({ detail: { code: stage === "questions" ? "question_preparation_artifact_invalid" : "provider_response_invalid" } }, 422);
          }
          task.status = stage === "questions" ? "problems_ready" : stage === "submissions" ? "submissions_ready" : "graded";
          task.error = null; task.last_failed_job_id = null; task.workflow_revision++;
          return json({ status: stage === "grading" ? "manual_review" : "ready_for_review", workflow_revision: 3, job_id: "failed-job" });
        }
        if (path === "/tasks/manual-feedback/state") return json({ ...task, progress: { phase: "paused", messages: [], active: [],
          error_detail: task.error, preparation_counts: { stems: 1, reference_answers: 1, rubrics: 1, questions: 1 } } });
        if (path === "/tasks/manual-feedback") return json(task);
        if (path.endsWith("/source-files")) return json({ problem_source: null, submission_sources: {} });
        return json({});
      });
      await page.goto(progressPath);
      await expect(page).toHaveTitle("SmarTAI");
      await expect(page).toHaveURL(new RegExp(`${progressPath}$`));
      const manual = page.getByRole("button", { name: zh ? "人工继续处理" : "Continue manually", exact: true });
      await manual.click();
      const opening = page.getByRole("button", { name: zh ? "正在打开…" : "Opening…", exact: true });
      await expect(opening).toBeDisabled();
      if (zh) await expect(opening).toHaveAttribute("aria-busy", "true");
      await expect(page.getByText(zh ? "正在打开人工复核" : "Opening manual review", { exact: true })).toHaveCount(0);
      await expect.poll(async () => {
        const box = await opening.boundingBox();
        return box && box.y >= 70 && box.y + box.height <= 900;
      }).toBe(true);
      await expect.poll(() => mutations).toEqual([handoffPath]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await opening.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath("pending-manual-handoff.png"), fullPage: false });
      release();
      await expect(manual).toBeEnabled();
      if (stage === "questions") await expect(page.getByRole("heading", { name: zh ? "题目结果保存或恢复未完成" : "Question results could not be saved or restored" })).toBeVisible();
      await manual.click();
      await expect(page).toHaveURL(new RegExp(`${matrixPath}$`));
      await expect(page.locator("h1")).not.toBeEmpty();
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
      expect(mutations).toEqual([handoffPath, handoffPath]);
      expect(errors).toEqual([]);
      expect(consoleErrors).toEqual([]);
    });
  }
}
