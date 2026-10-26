import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const stage of ["submissions", "grading"] as const) for (const locale of ["zh-CN", "en-US"] as const) {
  test(`${stage} ${locale}: every dialog lists current errors and shrinks after saved repairs`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN", taskId = "blocker-repairs";
    const errors: string[] = [], writes: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width: zh ? 1280 : 390, height: 900 });
    await page.addInitScript(locale => {
      localStorage.setItem("smartai_locale", locale);
      localStorage.setItem("smartai_token", "synthetic-review-token");
    }, locale);
    const problem_data = Object.fromEntries([1, 2].map(n => [`q${n}`, {
      q_id: `q${n}`, number: String(n), type: "reasoning", stem: "Explain the result.", criterion: "Valid reasoning earns ten points.",
      reference_answer: "Reference reasoning.", max_score: 10, review_status: "confirmed", max_score_review_status: "confirmed",
    }]));
    const answers = [1, 2].map(n => ({ q_id: `q${n}`, number: String(n), type: "reasoning", content: "Transcription to repair.",
      review_status: "pending", recognition_confidence: .2, flag: stage === "submissions" ? ["parse_failed", "authorship_uncertain"] : [] }));
    const student_data = { S1: { stu_id: "S1", stu_name: "Example", identity_status: "matched", identity_review_status: "confirmed", stu_ans: answers } };
    const corrections = [1, 2].map(n => ({ q_id: `q${n}`, score: null as number | null, provisional_score: null as number | null,
      teacher_score: null as number | null, max_score: 10, confidence: .2, comment: "Original evidence.", steps: [],
      result_status: "failed", review_status: "pending", review_reasons: ["parse_failed", "low_confidence"] }));
    const task = { task_id: taskId, name: "Blocker repairs", status: stage === "submissions" ? "submissions_ready" : "graded", workflow_revision: 1,
      problem_data, student_data, problem_count: 2, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {} };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-review-token" });
      if (request.method() !== "GET") {
        const input = request.postDataJSON();
        expect(request.method()).toBe("PUT");
        expect(input.expected_workflow_revision).toBe(task.workflow_revision);
        writes.push(path);
        task.workflow_revision++;
        if (stage === "submissions") {
          expect(path).toMatch(/\/students\/S1\/answers\/q[12]$/);
          const answer = answers.find(a => path.endsWith(`/${a.q_id}`))!;
          expect(input.content).toBe("Corrected transcription.");
          Object.assign(answer, { content: input.content, flag: [], review_status: input.review_status });
          return json({ status: "ok", stu_id: "S1", q_id: answer.q_id, answer, workflow_revision: task.workflow_revision });
        }
        expect(path).toMatch(/\/reviews\/S1\/q[12]$/);
        const correction = corrections.find(c => path.endsWith(`/${c.q_id}`))!;
        expect(input.teacher_score).toBe(8);
        Object.assign(correction, { teacher_score: 8, score: 8, result_status: "completed", review_status: "confirmed", review_reasons: [] });
        return json({ status: "ok", student_id: "S1", q_id: correction.q_id, correction, workflow_revision: task.workflow_revision });
      }
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results: [{ student_id: "S1", student_name: "Example", corrections }], timestamp: 1 });
      if (path.endsWith("/source-files")) return json({ task_id: taskId, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
      if (path.endsWith("/teacher_comments")) return json({ comments: {} });
      if (path.endsWith("/finalization")) {
        const remaining = corrections.filter(c => c.score === null).length;
        return json({ task_id: taskId, workflow_revision: task.workflow_revision, remaining_review_count: remaining, remaining_reviews: [], ready_for_confirmation: remaining === 0 });
      }
      return json([]);
    });
    const overview = `/tasks/${taskId}/${stage === "submissions" ? "submissions" : "review"}`;
    const detail = `/tasks/${taskId}/${stage === "submissions" ? "students/S1?question=q1" : "review/S1/q1"}`;
    const next = stage === "submissions" ? (zh ? "进入批改设置" : "Open Grading Setup") : (zh ? "查看结果分析" : "View Results & Analysis");
    const dialog = page.getByRole("alertdialog");
    async function inspectDialog(count: number, target: string) {
      await expect(dialog.getByRole("listitem")).toHaveCount(count);
      await expect(dialog).not.toContainText(/置信度|confidence|笔迹|authorship|学生实际留白|Genuine blank/);
      for (const row of await dialog.getByRole("listitem").all()) {
        await expect(row).toContainText(stage === "submissions" ? (zh ? "解析失败" : "parsing failed") : (zh ? /分数|最终分|解析失败/ : /score|parse result/));
      }
      await dialog.getByRole("button", { name: zh ? /前往问题位置|直接前往填写分数/ : /Go to issue|Go to the missing score/ }).click();
      await expect(dialog).toHaveCount(0);
      await expect(page.locator(`[data-question-id="${target}"]`)).toBeInViewport();
    }
    await page.goto(overview);
    await page.getByRole("button", { name: next, exact: true }).click();
    await inspectDialog(stage === "submissions" ? 2 : 4, "q1");
    await page.goto(detail);
    if (stage === "submissions") {
      await page.locator('[data-question-id="q1"]').getByRole("button", { name: zh ? /待确认/ : /Confirm review/ }).click();
      await inspectDialog(1, "q1");
    } else {
      await page.getByRole("button", { name: zh ? "全部确认" : "Confirm all", exact: true }).click();
      await inspectDialog(4, "q1");
    }
    expect(writes).toEqual([]);
    await page.getByRole("button", { name: next, exact: true }).click();
    await inspectDialog(stage === "submissions" ? 2 : 4, "q1");
    for (const n of [1, 2]) {
      const card = page.locator(`[data-question-id="q${n}"]`);
      if (stage === "submissions") {
        await card.getByRole("button", { name: zh ? "修改" : "Edit", exact: true }).click();
        await card.locator("textarea").fill("Corrected transcription.");
        await card.getByRole("button", { name: zh ? /保存并确认/ : /Save and confirm/ }).click();
      } else {
        await card.getByRole("textbox", { name: zh ? /最终得分$/ : /final score$/ }).fill("8");
        await card.getByRole("button", { name: zh ? /待确认/ : /Confirm review/ }).click();
      }
      await expect.poll(() => writes.length).toBe(n);
      if (n === 1) {
        await page.getByRole("button", { name: next, exact: true }).click();
        await expect(dialog.getByRole("listitem")).toHaveCount(stage === "submissions" ? 1 : 2);
        await page.screenshot({ path: testInfo.outputPath("one-remaining-issue.png"), fullPage: true });
        await inspectDialog(stage === "submissions" ? 1 : 2, "q2");
      }
    }
    await page.getByRole("button", { name: next, exact: true }).click();
    await expect(page).toHaveURL(stage === "submissions" ? /grading-setup/ : /results/);
    await expect(dialog).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
