import { expect, test } from "@playwright/test";

for (const stage of ["problems", "submissions", "grading"] as const) {
  for (const locale of ["zh-CN", "en-US"] as const) {
    test(`${stage} ${locale}: failure diagnostics only count required items and scroll in a compact panel`, async ({ page }, info) => {
      const zh = locale === "zh-CN";
      await page.setViewportSize({ width: zh ? 1440 : 390, height: 900 });
      const errors: string[] = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
      const provider = { provider_id: "test", model: "synthetic", enabled: true };
      const red = { issue_id: "red", field: "rubric", code: "generation_failed", status: "open", severity: "blocking" };
      const yellow = { issue_id: "yellow", field: "stem", code: "recognition_needs_review", status: "open", severity: "warning" };
      const correction = { student_id: "s1", q_id: "yellow", score: 8, max_score: 10, confidence: .2, review_reasons: ["low_confidence"], expert_results: [] };
      const source = { source_id: "source", file_id: "file", file_name: "Synthetic.txt", content_type: "text/plain", size_bytes: 100,
        status: "failed", internal_status: "parse_failed", reason_code: "recognition_response_invalid", retryable: true, unknown_question_ids: [],
        answer_diagnostic_count: 50, answer_required_diagnostic_count: 25,
        answer_diagnostics: [
          { q_id: "yellow", number: "yellow", has_content: true, recognition_confidence: .2, flag: [] },
          ...Array.from({ length: 25 }, (_, i) => ({ q_id: `q${i}`, number: `red-${i}`, has_content: true,
            recognition_confidence: .2, flag: ["recognition_invalid_fields:content"] })),
        ] };
      const task = { task_id: "required", name: "Required diagnostics", status: "error", workflow_revision: 2,
        last_failed_job_id: "failed", error: "provider_response_invalid", active_job_id: null,
        extract_job_id: stage === "problems" ? "failed" : "question-job", parse_job_id: stage === "submissions" ? "failed" : "answer-job",
        grading_job_id: stage === "grading" ? "failed" : null,
        question_recognition_provider_id: "test", submission_recognition_provider_id: "test", grading_setup_configured: true,
        problem_data: {}, student_data: {}, submission_sources: [source], problem_count: 25, student_count: 1, kb_docs: {}, kb_doc_count: 0 };
      const progress = { phase: "error", error_detail: "provider_response_invalid", active: [], messages: [],
        preparation_review_summary: { questions: 50, blocking: 25, advisory: 25 },
        preparation_review: [ { q_id: "yellow", number: "yellow", issues: [yellow] },
          ...Array.from({ length: 25 }, (_, i) => ({ q_id: `q${i}`, number: `red-${i}`, issues: [red, yellow] })) ],
        grading_review_summary: { affected: 25, missing_scores: 25 },
        grading_review: [correction, ...Array.from({ length: 25 }, (_, i) => ({ ...correction, q_id: `red-${i}`, score: null }))] };
      await page.addInitScript(language => { localStorage.setItem("smartai_token", "synthetic"); localStorage.setItem("smartai_locale", language); }, locale);
      const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
      await page.route(`${backend}/**`, route => {
        const path = new URL(route.request().url()).pathname;
        const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
        if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
        if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
        if (path === "/auth/activity") return json({ token: "synthetic" });
        if (path.startsWith("/experts")) return json([provider]);
        if (path.endsWith("/state")) return json({ ...task, progress });
        if (path === "/tasks/required") return json(task);
        return json({});
      });
      await page.goto(`/tasks/required/${stage}/progress`);
      await expect(page).toHaveTitle("SmarTAI");
      await expect(page).toHaveURL(new RegExp(`/${stage}/progress$`));
      const panel = stage === "grading" ? page.getByRole("region", { name: zh ? "批改问题明细" : "Grading issue details" })
        : stage === "problems" ? page.getByRole("region", { name: zh ? "题目待处理详情" : "Question preparation details" })
          : page.getByRole("heading", { name: zh ? "本批文件识别结果" : "Submission source results" }).locator("xpath=ancestor::section[1]");
      await expect(panel).toBeVisible();
      await expect(panel).not.toContainText(zh ? "建议复核：" : "Suggested:");
      await expect(panel).not.toContainText("yellow");
      await expect(panel).not.toContainText("red-20");
      if (stage === "problems") await expect(panel).toContainText(zh ? "需要处理 25 题" : "25 questions need action");
      else await expect(panel).toContainText(zh ? "25 份作答需要关注" : "25 responses need attention");
      const list = panel.locator(stage === "submissions" ? "ol" : "ul").first();
      const dimensions = await list.evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight }));
      expect(dimensions.height).toBeLessThanOrEqual(stage === "submissions" ? 260 : 192);
      expect(dimensions.scroll).toBeGreaterThan(dimensions.height);
      await list.evaluate(el => { el.scrollTop = el.scrollHeight; });
      expect(await list.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
      await expect(panel.getByText(zh ? "第 red-19 题" : "Question red-19", { exact: stage !== "grading" })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
      await panel.screenshot({ path: info.outputPath("required-diagnostics.png") });
      expect(errors).toEqual([]);
    });
  }
}
