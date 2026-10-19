import { expect, test } from "@playwright/test";

for (const stage of ["questions", "submissions"] as const) {
  for (const responseStatus of ["started", "already_running"] as const) {
    for (const fails of [false, true]) {
      test(`${stage} saved-text handoff ${responseStatus}: refresh while slow, then ${fails ? "failure" : "review"}`, async ({ page }, testInfo) => {
        const errors: string[] = [], visits: string[] = [];
        page.on("pageerror", error => errors.push(error.message));
        page.on("framenavigated", frame => { if (frame === page.mainFrame()) visits.push(frame.url()); });
        const questions = stage === "questions";
        const progressPath = `/tasks/saved-text/${questions ? "problems" : "submissions"}/progress`;
        const matrixPath = `/tasks/saved-text/${stage}`;
        const problem = { q_id: "q1", number: "1", type: "short", stem: "Saved original question", criterion: "One point.",
          max_score: 1, reference_answer: "Original reference", preparation_issues: [], review_status: "needs_review" };
        const task = { task_id: "saved-text", name: "Saved recovery", status: questions ? "problems_paused" : "submissions_paused",
          workflow_revision: 2, extract_job_id: questions ? "failed-job" : "old-question-job", parse_job_id: questions ? null : "failed-job",
          last_failed_job_id: "failed-job" as string | null, error: "operation_cancelled" as string | null,
          question_recognition_provider_id: "provider", submission_recognition_provider_id: "provider",
          problem_count: questions ? 0 : 1, problem_data: questions ? {} : { q1: problem }, student_count: 0, student_data: {},
          recovering_saved_text: false, active_job_id: null as string | null, active_operation_status: null as string | null,
          created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
        let handoffs = 0;
        await page.addInitScript(() => {
          localStorage.setItem("smartai_token", "synthetic"); localStorage.setItem("smartai_locale", "zh-CN");
        });
        const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
        await page.route(`${backend}/**`, async route => {
          const request = route.request(), path = new URL(request.url()).pathname;
          const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
          if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
            "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
          if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
          if (path === "/auth/activity") return json({ token: "synthetic" });
          if (path.startsWith("/experts")) return json([{ provider_id: "provider", provider_type: "gemini", model: "synthetic", enabled: true }]);
          if (request.method() === "POST") {
            expect(path).toBe(`/tasks/saved-text/${questions ? "question-preparation" : "submission-recognition"}/failed-job/manual-review`);
            handoffs++;
            task.status = questions ? "extracting_problems" : "parsing_submissions";
            task.recovering_saved_text = true; task.active_job_id = "recovery"; task.active_operation_status = "running";
            task.error = null; task.last_failed_job_id = null; task.workflow_revision++;
            return json({ status: responseStatus, job_id: "recovery", workflow_revision: task.workflow_revision });
          }
          if (path === "/tasks/saved-text/state") return json({ ...task, progress: {
            job_id: "recovery", phase: task.active_job_id ? "extracting" : "done", messages: [], active: [], model_waits: [],
            stage_metrics: {}, current_step: "extracting_questions", total_steps: 2, completed_steps: 0 } });
          if (path === "/tasks/saved-text") return json(task);
          if (path.endsWith("/source-files")) return json({ problem_source: null, submission_sources: {} });
          return json([]);
        });
        await page.goto(progressPath);
        await page.getByRole("button", { name: "人工继续处理", exact: true }).click();
        await expect(page.getByText("正在整理已保存的识别文字，完成后自动进入复核。")).toBeVisible();
        await expect(page).toHaveURL(new RegExp(`${progressPath}$`));
        await page.reload();
        await expect(page.getByText("正在整理已保存的识别文字，完成后自动进入复核。")).toBeVisible();
        expect(visits.some(url => url.endsWith(matrixPath))).toBe(false);
        expect(handoffs).toBe(1);
        await page.screenshot({ path: testInfo.outputPath("organizing-saved-text.png") });
        task.active_job_id = null; task.active_operation_status = null; task.recovering_saved_text = false;
        if (fails) {
          task.status = "error"; task.error = "provider_response_invalid"; task.last_failed_job_id = "recovery";
          if (questions) task.extract_job_id = "recovery"; else task.parse_job_id = "recovery";
          await expect(page.getByRole("button", { name: "重试失败项", exact: true })).toBeVisible();
          await expect(page).toHaveURL(new RegExp(`${progressPath}$`));
          expect(visits.some(url => url.endsWith(matrixPath))).toBe(false);
        } else {
          task.status = questions ? "problems_ready" : "submissions_ready";
          task.problem_count = 1; task.problem_data = { q1: problem };
          if (!questions) {
            task.student_count = 1; task.student_data = { S1: { stu_id: "S1", stu_name: "Saved Student", stu_ans: [
              { q_id: "q1", number: "1", type: "short", content: "Saved original answer", flag: [] }] } };
          }
          await expect(page).toHaveURL(new RegExp(`${matrixPath}$`));
          if (questions) await expect(page.getByRole("row").filter({ hasText: "1" }).first()).toBeVisible();
          else await expect(page.getByText("Saved Student", { exact: true })).toBeVisible();
          expect(handoffs).toBe(1);
        }
        expect(errors).toEqual([]);
      });
    }
  }
}
