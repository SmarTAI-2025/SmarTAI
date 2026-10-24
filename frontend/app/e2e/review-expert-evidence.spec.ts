import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "asg_review_evidence";
const problem_data = Object.fromEntries([1, 2].map(n => [`q${n}`, { q_id: `q${n}`, number: String(n), stem: `Explain synthetic result ${n}.`, criterion: "Credit valid reasoning.", max_score: 10, type: "reasoning" }]));
const student_data = { S1: { stu_id: "S1", stu_name: "Example teacher data", identity_status: "matched", stu_ans: [1, 2].map(n => ({ q_id: `q${n}`, content: "Synthetic reasoning.", review_status: "confirmed", flag: [] })) } };
const experts = [7, 10].map((score, index) => ({ provider: `model-${index}`, score, max_score: 10, confidence: index ? .99 : .95,
  comment: `Original expert explanation ${index}.`, steps: [{ step_no: 1, desc: `Expert ${index} reasoning`, score, is_correct: true }] }));
const results = [{ student_id: "S1", student_name: "Example teacher data", corrections: [1, 2].map(n => ({ q_id: `q${n}`, score: 8.53, provisional_score: 8.53, max_score: 10,
  confidence: n === 1 ? .99 : .4, requires_human_review: n === 2, review_reasons: n === 2 ? ["high_indecisiveness", "low_confidence"] : [], review_status: "pending",
  comment: `Complete AI explanation ${n}.`, steps: [{ step_no: 1, desc: `Combined reasoning ${n}`, score: 8.53, is_correct: true }], expert_results: [...experts,
    { provider: "failed-model", score: 0, max_score: 10, confidence: 0, error_kind: "parse_failed", comment: "Could not parse the score.", steps: [] }], synthesis_method: "weighted_average" })) }];
const task = { task_id: taskId, owner_id: "shared-owner", name: "Expert evidence", status: "graded", workflow_revision: 1, final_result_version: 0, problem_data, student_data,
  problem_count: 2, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };

for (const demo of [false, true]) for (const locale of ["zh-CN", "en-US"]) {
  test(`${demo ? "Demo" : "Teacher"} ${locale}: same evidence and score precision for both disagreement signals`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    if (demo) await page.setViewportSize({ width: 390, height: 844 });
    else await page.setViewportSize({ width: 1440, height: 1000 });
    await page.addInitScript(({ demo, locale, taskId }) => {
      localStorage.setItem("smartai_locale", locale);
      if (demo) {
        sessionStorage.setItem("smartai_frontier_token", "synthetic-guest-token");
        sessionStorage.setItem("smartai_frontier_owner", "shared-owner");
        sessionStorage.setItem("smartai_frontier_generation", "1");
        sessionStorage.setItem("smartai_frontier_tasks", JSON.stringify([taskId]));
      } else localStorage.setItem("smartai_token", "synthetic-teacher-token");
    }, { demo, locale, taskId });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "shared-owner", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: demo ? "frontier_demo" : "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path.startsWith("/experts") || path === "/tags/") return json([]);
      if (path === `/tasks/${taskId}`) return json(task);
      if (path.endsWith("/result")) return json({ task_id: taskId, problem_data, student_data, results, timestamp: 1 });
      if (path.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, final_result_version: 0, final_result_dirty: false, analysis_status: "not_started", remaining_review_count: 0, ready_for_confirmation: true });
      throw new Error(`Unexpected request ${request.method()} ${path}`);
    });
    const zh = locale === "zh-CN";
    await page.goto(`/tasks/${taskId}/review/S1/q1`);
    for (const n of [1, 2]) {
      const card = page.locator(`[data-question-id="q${n}"]`);
      await expect(card.getByText(`Complete AI explanation ${n}.`)).toBeVisible();
      await expect(card.getByRole("region", { name: zh ? "SmarTAI 批改结果" : "SmarTAI Grading Result" })).toContainText("8.5 / 10");
      await expect(card.getByRole("textbox", { name: `Q${n} ${zh ? "最终得分" : "final score"}` })).toHaveValue("8.5");
      await card.getByText(zh ? "查看各专家原始结果" : "View Original Model Results").click();
      await expect(card.getByText("Expert 0 reasoning")).toBeVisible();
      await expect(card.getByText("Original expert explanation 1.")).toBeVisible();
      await expect(card.getByText(zh ? /failed-model.*无有效分数/ : /failed-model.*No valid score/)).toBeVisible();
    }
    await expect(page.getByText(/已隐藏|details are hidden/)).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('[data-question-id="q2"]').screenshot({ path: testInfo.outputPath("disputed-evidence.png") });
    expect(errors).toEqual([]);
  });
}
