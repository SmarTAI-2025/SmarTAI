import { expect, test, type Page } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

async function expectPageTop(page: Page) {
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
}

async function checkStudentSwitch(page: Page, nextButton: string) {
  for (const key of ["ArrowRight", "ArrowLeft"]) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
    await page.locator("body").press(key);
    await expect(page).toHaveURL(key === "ArrowRight" ? /\/S002\b/ : /\/S001\b/);
    await expectPageTop(page);
  }
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.getByRole("button", { name: nextButton, exact: true }).click();
  await expect(page).toHaveURL(/\/S002\b/);
  await expectPageTop(page);
}

for (const stage of ["submissions", "grading"]) for (const demo of [false, true]) for (const locale of ["zh-CN", "en-US"]) for (const width of [390, 1440]) {
  test(`${stage} ${demo ? "Demo" : "Teacher"} ${locale} ${width}px: matrix View opens details with a normal click`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width, height: 844 });
    const taskId = "matrix-navigation", owner = "matrix-owner", zh = locale === "zh-CN";
    await page.addInitScript(({ demo, locale, taskId, owner }) => {
      localStorage.setItem("smartai_locale", locale);
      if (demo) {
        sessionStorage.setItem("smartai_frontier_token", "synthetic-matrix-token");
        sessionStorage.setItem("smartai_frontier_owner", owner);
        sessionStorage.setItem("smartai_frontier_generation", "1");
        sessionStorage.setItem("smartai_frontier_tasks", JSON.stringify([taskId]));
      } else localStorage.setItem("smartai_token", "synthetic-matrix-token");
    }, { demo, locale, taskId, owner });
    const problem_data = Object.fromEntries(Array.from({ length: 3 }, (_, i) => {
      const q_id = `q${i + 1}`;
      return [q_id, { q_id, number: String(i + 1), type: "Reasoning", stem: `Question ${i + 1}.`, criterion: "Award 10 points for valid reasoning.", reference_answer: "A complete proof.", max_score: 10, review_status: "confirmed", max_score_review_status: "confirmed" }];
    }));
    const students = Array.from({ length: 12 }, (_, i) => ({ stu_id: `S${String(i + 1).padStart(3, "0")}`, stu_name: `Student S${String(i + 1).padStart(3, "0")}`, identity_status: "matched",
      stu_ans: Object.keys(problem_data).map(q_id => ({ q_id, content: "Synthetic answer.", review_status: "confirmed", flag: [] })) }));
    const student_data = Object.fromEntries(students.map(student => [student.stu_id, student]));
    const results = students.map(student => ({ student_id: student.stu_id, student_name: student.stu_name,
      corrections: Object.keys(problem_data).map(q_id => ({ q_id, score: 8, provisional_score: 8, teacher_score: 8, max_score: 10, confidence: .95, comment: "Valid reasoning.", steps: [], review_status: "confirmed", review_reasons: [] })) }));
    const task = { task_id: taskId, owner_id: owner, name: "Matrix navigation", status: stage === "grading" ? "graded" : "submissions_ready", workflow_revision: 1,
      problem_data, student_data, problem_count: 3, student_count: students.length, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: owner, username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: demo ? "frontier_demo" : "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-matrix-token" });
      expect(request.method()).toBe("GET");
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results, timestamp: 1 });
      if (path.endsWith("/source-files")) return json({ task_id: taskId, workflow_revision: 1, problem_source: null, submission_sources: {} });
      if (path.endsWith("/teacher_comments")) return json({ comments: {} });
      if (path.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, remaining_reviews: [], remaining_review_count: 0, ready_for_confirmation: true });
      return json([]);
    });
    const overview = `/tasks/${taskId}/${stage === "grading" ? "review" : "submissions"}`;
    for (const position of ["first", "last"] as const) {
      await page.goto(overview);
      const rows = page.locator("table tbody tr");
      await expect(rows).toHaveCount(students.length);
      const link = rows[position]().getByRole("link", { name: zh ? "查看" : "View", exact: true });
      const href = await link.getAttribute("href");
      await link.click({ timeout: 5_000 });
      await expect(page).toHaveURL(new URL(href!, page.url()).href);
      await expect(page.locator('[data-question-id="q1"]')).toBeVisible();
      await expectPageTop(page);
      if (position === "first" && zh && !demo && width === 1440) await checkStudentSwitch(page, "Student S002");
      await expect(page.getByRole("main")).toHaveCount(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    }
    await page.screenshot({ path: testInfo.outputPath("matrix-details.png"), fullPage: false });
    if (zh && !demo && width === 1440) {
      for (const [index, flag] of [[1, "recognition_failed"], [2, "authorship_uncertain"]] as const) {
        Object.assign(students[index].stu_ans[1], { review_status: "pending", flag: [flag] });
        Object.assign(results[index].corrections[1], {
          review_status: "pending", teacher_score: null, score: index === 1 ? null : 8,
          provisional_score: index === 1 ? null : 8, confidence: index === 1 ? 0 : .2,
        });
      }
      await page.goto(overview + "?" + new URLSearchParams({ column_sort: JSON.stringify(["id", "desc"]) }));
      const rows = page.locator("table tbody tr");
      const firstIdCell = rows.first().locator("td, th").first();
      await expect(firstIdCell).toHaveText("S012");
      await page.getByRole("link", { name: stage === "grading" ? "查看批改详情" : "查看作答详情" }).click();
      await expect(page).toHaveURL(/\/S012(?:\/all)?(?:\?|$)/);
      await expect(page.locator('[data-question-id="q1"]')).toBeVisible();
      await expectPageTop(page);
      for (const [id, queueTab] of [["S001", null], ["S002", "需要处理"], ["S003", "建议处理"]] as const) {
        for (const source of (queueTab ? ["cell", "queue"] : ["cell"])) {
          await page.goto(overview);
          const row = rows.filter({ hasText: id });
          let link = row.locator("td, th").nth(3).getByRole("link");
          if (source === "queue") {
            const queue = page.getByRole("region", { name: "待复核队列" });
            await queue.getByRole("tab", { name: new RegExp(queueTab!) }).click();
            link = queue.getByRole("link", { name: new RegExp(id) });
          }
          const href = await link.getAttribute("href");
          await link.click();
          await expect(page).toHaveURL(new URL(href!, page.url()).href);
          expect(new URL(page.url()).pathname.split("/")).toContain(id);
          const target = page.locator('[data-question-id="q2"]');
          await expect.poll(() => target.evaluate(node => Math.abs(node.getBoundingClientRect().top - 86))).toBeLessThan(3);
        }
      }
    }
    if (stage === "grading" && zh && !demo && width === 1440) {
      await page.goto(`/tasks/${taskId}/results/students/S001`);
      await expect(page.getByRole("heading", { name: "Student S001", exact: true })).toBeVisible();
      await checkStudentSwitch(page, "下一位");
    }
    expect(errors).toEqual([]);
  });
}
