import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const stage of ["questions", "submissions", "grading"] as const) {
  test(`${stage}: blocked confirmation locates the issue repeatedly`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-review-token");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const taskId = "issue-navigation";
    const problem_data = Object.fromEntries([1, 2, 3].map(n => [`q${n}`, {
      q_id: `q${n}`, number: String(n), type: "Reasoning", stem: `Synthetic question ${n}.\n\n${"A line of question context.\n\n".repeat(12)}`,
      criterion: stage === "questions" && n === 2 ? "" : "Award 10 points for valid reasoning.",
      reference_answer: "Complete reasoning.", max_score: 10, review_status: "pending", max_score_review_status: "pending",
    }]));
    const student_data = { S1: { stu_id: "S1", stu_name: "Synthetic student", identity_status: "matched", identity_review_status: "confirmed",
      stu_ans: [1, 2, 3].map(n => ({ q_id: `q${n}`, content: "Synthetic answer.\n\n".repeat(20), review_status: "pending", flag: stage === "submissions" && n === 2 ? ["recognition_failed"] : [] })) } };
    const corrections = [1, 2, 3].map(n => ({ q_id: `q${n}`, score: n === 2 ? null : 8, provisional_score: n === 2 ? null : 8,
      max_score: 10, confidence: .95, comment: "Synthetic explanation.", steps: [], review_status: "pending", teacher_score: null,
      result_status: n === 2 ? "failed" : "completed", review_reasons: n === 2 ? ["grading_failed"] : [] }));
    const task = { task_id: taskId, name: "Issue navigation", status: stage === "questions" ? "problems_ready" : stage === "submissions" ? "submissions_ready" : "graded",
      workflow_revision: 1, problem_data, student_data, problem_count: 3, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {} };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Synthetic teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-review-token" });
      expect(request.method()).toBe("GET");
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results: [{ student_id: "S1", student_name: "Synthetic student", corrections }], timestamp: 1 });
      if (path.endsWith("/source-files")) return json({ task_id: taskId, workflow_revision: 1, problem_source: null, submission_sources: {} });
      if (path.endsWith("/teacher_comments")) return json({ comments: {} });
      if (path.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, remaining_reviews: [], remaining_review_count: 1, ready_for_confirmation: false });
      return json([]);
    });
    const initial = `/tasks/${taskId}/${stage === "questions" ? "questions/q1/content" : stage === "submissions" ? "students/S1?question=q1" : "review/S1/q1"}`;
    await page.goto(initial);
    const target = page.locator('[data-question-id="q2"]');
    for (const [iteration, control] of (["back", "escape", "link", "button", "button", "link"] as const).entries()) {
      const confirm = stage === "submissions"
        ? page.getByRole("button", { name: "进入批改设置", exact: true })
        : stage === "questions" && iteration === 0
        ? target.getByRole("button", { name: /待确认/ })
        : page.getByRole("button", { name: "全部确认", exact: true });
      await confirm.scrollIntoViewIfNeeded();
      // Measure at the actual click, after Playwright's automatic scrolling.
      await confirm.evaluate(element => element.addEventListener("click", () => {
        element.setAttribute("data-top-before-dialog", String(element.getBoundingClientRect().top));
      }, { once: true }));
      await confirm.click();
      const dialog = page.getByRole("alertdialog");
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("button", { name: "返回", exact: true })).toBeFocused();
      const triggerDisplacement = () => confirm.evaluate(element =>
        Math.abs(element.getBoundingClientRect().top - Number(element.getAttribute("data-top-before-dialog"))));
      await expect.poll(triggerDisplacement).toBeLessThan(2);
      const href = await dialog.getByRole("link").first().getAttribute("href");
      await page.screenshot({ path: testInfo.outputPath(`${iteration}-${control}-before.png`) });
      if (control === "back" || control === "escape") {
        if (control === "back") await dialog.getByRole("button", { name: "返回", exact: true }).click();
        else await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
        await expect.poll(triggerDisplacement).toBeLessThan(2);
        continue;
      }
      if (control === "button") await dialog.getByRole("button", { name: "前往问题位置" }).click();
      else await dialog.getByRole("link").first().click();
      await expect(dialog).toHaveCount(0);
      await expect(page).toHaveURL(new URL(href!, page.url()).href);
      await expect.poll(async () => target.evaluate(element => element.getBoundingClientRect().top)).toBeGreaterThanOrEqual(0);
      await expect.poll(async () => target.evaluate(element => element.getBoundingClientRect().top)).toBeLessThan(150);
      await page.screenshot({ path: testInfo.outputPath(`${iteration}-${control}-after.png`) });
    }
    expect(errors).toEqual([]);
  });
}
