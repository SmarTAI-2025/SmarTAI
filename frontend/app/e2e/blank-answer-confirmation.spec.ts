import { test, expect } from "@playwright/test";

for (const edit of [false, true]) test(`confirm a required blank answer through the existing ${edit ? "save" : "confirm"} button`, async ({ page }) => {
  const errors: string[] = [], mutations: unknown[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const answer = { q_id: "q1", number: "1", type: "short", content: "", review_status: "pending",
    flag: [edit ? "recognition_failed" : "recognition_manual_pending"] };
  const task = { task_id: "blank-answer", name: "Blank answer review", status: "submissions_ready", workflow_revision: 3,
    problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0,
    problem_data: { q1: { q_id: "q1", number: "1", type: "short", stem: "What is 2 + 2?", criterion: "Award 10 points for 4.", max_score: 10, review_status: "confirmed" } },
    student_data: { S001: { stu_id: "S001", stu_name: "Lin", identity_status: "matched", stu_ans: [answer] } } };
  await page.addInitScript(() => { localStorage.setItem("smartai_token", "synthetic-blank"); localStorage.setItem("smartai_locale", "zh-CN"); });
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    if (path === "/auth/me") return json({ id: "teacher", username: "test", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-blank" });
    if (request.method() === "PUT") {
      expect(path).toBe("/tasks/blank-answer/students/S001/answers/q1");
      const body = request.postDataJSON(); mutations.push(body);
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      expect(body.content).toBe("");
      expect(body.review_status).toBe("confirmed");
      expect(body.flag).toContain("manual_blank_confirmed");
      answer.flag = ["manual_blank_confirmed"]; answer.review_status = "confirmed";
      task.workflow_revision++;
      return json({ status: "ok", answer, workflow_revision: task.workflow_revision });
    }
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    if (path === "/tasks/blank-answer" || path.endsWith("/state")) return json(task);
    return json([]);
  });
  await page.goto("/tasks/blank-answer/submissions");
  await expect(page.getByRole("tab", { name: "需要处理 (1)" })).toBeVisible();
  await expect(page.getByRole("button", { name: "全部确认" })).toHaveCount(0);
  await page.getByRole("region", { name: "待复核队列" }).getByRole("link").click();
  await expect(page).toHaveURL(/students\/S001\?question=q1/);
  const card = page.locator('[data-question-id="q1"]');
  await expect(page.getByRole("button", { name: "全部确认" })).toHaveCount(0);
  if (edit) {
    await card.getByRole("button", { name: "修改", exact: true }).click();
    await card.locator("textarea").fill("   \n ");
  }
  await card.getByRole("button", { name: edit ? "第 1 题 · 保存并确认" : "第 1 题 · 待确认", exact: true }).click();
  await expect(card.getByRole("button", { name: "第 1 题 · 已确认", exact: true })).toBeVisible();
  await page.reload();
  await expect(card.getByRole("button", { name: "第 1 题 · 已确认", exact: true })).toBeVisible();
  await page.goto("/tasks/blank-answer/submissions");
  await expect(page.getByRole("tab", { name: "需要处理 (0)" })).toBeVisible();
  await page.getByRole("button", { name: "进入批改设置", exact: true }).click();
  await expect(page).toHaveURL(/grading-setup$/);
  expect(mutations).toHaveLength(1);
  expect(errors).toEqual([]);
});
