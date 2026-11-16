import { expect, test } from "@playwright/test";

for (const stage of ["problems", "submissions", "grading"] as const) {
  for (const locale of ["zh-CN", "en-US"] as const) {
    for (const requiredCount of [25, 1]) {
    test(`${stage} ${locale} ${requiredCount} required: failure diagnostics only count required items and scroll in a compact panel`, async ({ page }, info) => {
      const zh = locale === "zh-CN";
      await page.setViewportSize({ width: zh ? 1440 : 390, height: 900 });
      const errors: string[] = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
      const provider = { provider_id: "test", model: "synthetic", enabled: true };
      const red = { issue_id: "red", field: "rubric", code: "generation_failed", status: "open", severity: "blocking" };
      const yellow = { issue_id: "yellow", field: "stem", code: "recognition_needs_review", status: "open", severity: "warning" };
      const correction = { student_id: "S2026001", q_id: "advisory", score: 8, max_score: 10, confidence: .2, review_reasons: ["low_confidence"], expert_results: [] };
      const source = { source_id: "source", file_id: "file", file_name: "示例学生_S2026001.txt", content_type: "text/plain", size_bytes: 100,
        status: "failed", internal_status: "parse_failed", reason_code: "recognition_response_invalid", retryable: true, unknown_question_ids: [],
        answer_diagnostic_count: 147 + requiredCount, answer_required_diagnostic_count: requiredCount,
        answer_diagnostics: [
          ...Array.from({ length: 147 }, (_, i) => ({ q_id: `advisory-${i}`, number: `2.1.${i + 1}`, has_content: true, recognition_confidence: .2, flag: [] })),
          ...Array.from({ length: requiredCount }, (_, i) => ({ q_id: `q${i}`, number: `1.2.${i + 1}`, has_content: true,
            recognition_confidence: .2, flag: ["recognition_invalid_fields:content"] })),
        ] };
      const task = { task_id: "required", name: "Required diagnostics", status: "error", workflow_revision: 2,
        last_failed_job_id: "failed", error: "provider_response_invalid", active_job_id: null,
        extract_job_id: stage === "problems" ? "failed" : "question-job", parse_job_id: stage === "submissions" ? "failed" : "answer-job",
        grading_job_id: stage === "grading" ? "failed" : null,
        question_recognition_provider_id: "test", submission_recognition_provider_id: "test", grading_setup_configured: true,
        problem_data: Object.fromEntries(Array.from({ length: 25 }, (_, i) => [`q${i}`, { q_id: `q${i}`, number: `1.2.${i + 1}`, type: "concept", stem: "Synthetic question", criterion: "Synthetic rubric", max_score: 10 }])),
        student_data: { S2026001: { stu_id: "S2026001", stu_name: "张三（测试）", stu_ans: [] } }, submission_sources: [source], problem_count: 25, student_count: 1, kb_docs: {}, kb_doc_count: 0 };
      const progress = { phase: "error", error_detail: "provider_response_invalid", active: [], messages: [],
        preparation_review_summary: { questions: 147 + requiredCount, blocking: requiredCount, advisory: 147 },
        preparation_review: [ ...Array.from({ length: 147 }, (_, i) => ({ q_id: `advisory-${i}`, number: `2.1.${i + 1}`, issues: [yellow] })),
          ...Array.from({ length: requiredCount }, (_, i) => ({ q_id: `q${i}`, number: `1.2.${i + 1}`, issues: [red, yellow] })) ],
        grading_review_summary: { affected: requiredCount, missing_scores: requiredCount },
        grading_review: [...Array.from({ length: 147 }, (_, i) => ({ ...correction, q_id: `advisory-${i}` })), ...Array.from({ length: requiredCount }, (_, i) => ({ ...correction, q_id: `q${i}`, score: null }))] };
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
      await expect(panel).not.toContainText("2.1.1");
      await expect(panel).not.toContainText("1.2.21");
      await expect(panel).not.toContainText(/red-|yellow|红色|imported_/);
      if (stage === "grading") await expect(panel).toContainText(zh ? "张三（测试）（S2026001）" : "张三（测试） (S2026001)");
      if (stage === "problems") await expect(panel).toContainText(zh ? `需要处理 ${requiredCount} 题` : `${requiredCount} questions need action`);
      else await expect(panel).toContainText(zh ? `${requiredCount} 份作答需要关注` : `${requiredCount} responses need attention`);
      const list = panel.locator(stage === "submissions" ? "ol" : "ul").first();
      const dimensions = await list.evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight }));
      expect(dimensions.height).toBeLessThanOrEqual(stage === "submissions" ? 260 : 192);
      if (requiredCount > 20) {
        expect(dimensions.scroll).toBeGreaterThan(dimensions.height);
        await list.evaluate(el => { el.scrollTop = el.scrollHeight; });
        expect(await list.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
      }
      const lastNumber = `1.2.${Math.min(20, requiredCount)}`;
      await expect(panel.getByText(zh ? `第 ${lastNumber} 题` : `Question ${lastNumber}`, { exact: stage !== "grading" })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
      await list.evaluate(el => { el.scrollTop = 0; });
      await panel.screenshot({ path: info.outputPath("required-diagnostics.png") });
      expect(errors).toEqual([]);
    });
  }
}
}
