import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const width of [1280, 390]) {
  test(`structured rubric reaches authoritative save without text-total veto (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-rubric-token");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const criterion = "第一问按1、2、1分分步；第二问2分；第三问4分。";
    const question = { q_id: "q1", number: "1", type: "计算题", max_score: 10,
      stem: "(1) 列式并计算。\n(2) 写出对称轴。\n(3) 说明结论。", criterion,
      reference_answer: "按题意解答。", rubric_version: 2, review_status: "confirmed", preparation_issues: [],
      rubric_point_summary: { contract_version: 1, has_explicit_subpart_points: true,
        total_points: "10", major_max_score: "10", is_valid: true, issue_code: null,
        items: [4, 2, 4].map((points, i) => ({ subpart_id: `sp${i + 1}`, label: `(${i + 1})`, points: String(points) })) },
    };
    const task = { task_id: "rubric-save", name: "Rubric save", status: "problems_ready", workflow_revision: 1,
      problem_data: { q1: question }, student_data: {}, problem_count: 1, student_count: 0,
      created_at: 1, updated_at: 1, kb_docs: {} };
    let saves = 0;
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-rubric-token" });
      if (path === "/tasks/rubric-save/problems/q1" && request.method() === "PUT") {
        const patch = request.postDataJSON();
        expect(patch.criterion).toBe(criterion + "。");
        expect(patch.max_score).toBe(10);
        expect(patch.expected_workflow_revision).toBe(1);
        saves++;
        question.criterion = patch.criterion;
        question.rubric_version++;
        task.workflow_revision++;
        return json({ status: "ok", problem: question, workflow_revision: task.workflow_revision });
      }
      if (path === "/tasks/rubric-save") return json(task);
      if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1,
        problem_source: null, submission_sources: {} });
      return json([]);
    });
    await page.goto("/tasks/rubric-save/questions/q1/content");
    await expect(page).toHaveURL(/questions\/q1\/content$/);
    expect(await page.title()).not.toBe("");
    const card = page.locator('[data-question-id="q1"]');
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "修改第 1 题满分与评分标准" }).click();
    const input = card.getByRole("textbox", { name: "第 1 题评分标准" });
    await input.fill(criterion + "。");
    await expect(card.getByRole("button", { name: "保存", exact: true })).toBeEnabled();
    await expect(card.getByRole("alert")).toHaveCount(0);
    await card.getByRole("button", { name: "保存", exact: true }).click();
    await expect(input).toHaveCount(0);
    expect(saves).toBe(1);
    await expect(card.getByText("分项合计 10/10", { exact: true })).toBeVisible();
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("structured-rubric-saved.png"), fullPage: true });
  });
}
