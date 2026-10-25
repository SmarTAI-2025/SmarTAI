import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "asg_review_evidence";
const problem_data = Object.fromEntries([1, 2].map(n => [`q${n}`, { q_id: `q${n}`, number: String(n), stem: `Explain synthetic result ${n}.`, criterion: "Credit valid reasoning.", max_score: 10, type: "reasoning" }]));
const student_data = { S1: { stu_id: "S1", stu_name: "Example teacher data", identity_status: "matched", stu_ans: [1, 2].map(n => ({ q_id: `q${n}`, content: "Synthetic reasoning.", review_status: "confirmed", flag: [] })) } };
const experts = [7, 10, 6, 8, 9].map((score, index) => ({ provider_id: `provider-${index}`, provider: index < 2 ? `model-${index}` : `provider:long-model-name-${index}-for-responsive-layout`, score, max_score: 10, confidence: index ? .99 : .95,
  comment: `Original expert explanation ${index}.`, steps: [0, 0, 1, score - 1].map((score, step) => ({ step_no: step + 1, desc: step ? `Model ${index} step ${step + 1}` : `Expert ${index} reasoning`, score, is_correct: score > 0 })) }));
const results = [{ student_id: "S1", student_name: "Example teacher data", corrections: [1, 2].map(n => ({ q_id: `q${n}`, score: 8.53, provisional_score: 8.53, max_score: 10,
  confidence: n === 1 ? .99 : .4, requires_human_review: n === 2, review_reasons: n === 2 ? ["high_indecisiveness", "low_confidence"] : [], review_status: "pending",
  comment: `Complete AI explanation ${n}.`, steps: [{ step_no: 1, desc: `Combined reasoning ${n}`, score: 8.53, is_correct: true }], expert_results: [...experts,
    { provider_id: "failed-provider", provider: "failed-model", score: 0, max_score: 10, confidence: 0, error_kind: "parse_failed", comment: "Could not parse the score.", steps: [] }], synthesis_method: "weighted_average" })) }];
const task = { task_id: taskId, owner_id: "shared-owner", name: "Expert evidence", status: "graded", workflow_revision: 1, final_result_version: 0, problem_data, student_data,
  problem_count: 2, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };

for (const demo of [false, true]) for (const locale of ["zh-CN", "en-US"]) {
  test(`${demo ? "Demo" : "Teacher"} ${locale}: same evidence and score precision for both disagreement signals`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    let singleModel = false;
    let editablePrimary = "provider-1";
    let resultPrimary = "provider-1";
    let setupReads = 0;
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
      if (path.endsWith("/grading-setup")) { setupReads += 1; return json({ grading_setup: { primary_provider_id: editablePrimary } }); }
      if (path.endsWith("/result")) return json({ task_id: taskId, grading_run_id: `run-${resultPrimary}`, primary_provider_id: resultPrimary, problem_data, student_data, results: singleModel ? results.map(student => ({ ...student, corrections: student.corrections.map(correction => ({ ...correction, expert_results: [experts[1]], synthesis_method: "single" })) })) : results, timestamp: 1 });
      if (path.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, final_result_version: 0, final_result_dirty: false, analysis_status: "not_started", remaining_review_count: 0, ready_for_confirmation: true });
      throw new Error(`Unexpected request ${request.method()} ${path}`);
    });
    const zh = locale === "zh-CN";
    await page.goto(`/tasks/${taskId}/review/S1/q1`);
    for (const n of [1, 2]) {
      const card = page.locator(`[data-question-id="q${n}"]`);
      const result = card.getByRole("region", { name: zh ? "SmarTAI 批改结果" : "SmarTAI Grading Result" });
      await expect(result.getByText("model-1（10/10, [0,0,1,9], conf=0.99）")).toBeVisible();
      await expect(result.getByText("Original expert explanation 1.", { exact: true })).toBeVisible();
      await expect(card.getByText(`Complete AI explanation ${n}.`)).toHaveCount(0);
      await expect(result).toContainText("8.5 / 10");
      const scoreCards = result.locator("dl").nth(1);
      await expect(scoreCards.locator(":scope > div")).toHaveCount(6);
      await expect(scoreCards).toContainText("[0,0,1,6]");
      await expect(scoreCards).toContainText("failed-model--/10");
      await expect(scoreCards).not.toContainText("No valid score");
      const boxes = await scoreCards.locator(":scope > div").evaluateAll(nodes => nodes.map(node => ({ y: node.getBoundingClientRect().y, width: node.getBoundingClientRect().width })));
      expect(boxes.every(box => box.width >= 150)).toBe(true);
      expect(new Set(boxes.map(box => box.y)).size).toBeGreaterThan(1);
      await expect(card.getByRole("textbox", { name: `Q${n} ${zh ? "最终得分" : "final score"}` })).toHaveValue("8.5");
      await card.getByText(zh ? "查看各专家原始结果" : "View Original Model Results").click();
      const panel = card.getByRole("tabpanel");
      await expect(panel.getByText("Expert 0 reasoning")).toBeVisible();
      await expect(panel.getByText("Original expert explanation 1.")).toHaveCount(0);
      await card.getByRole("tab", { name: "model-1", exact: true }).click();
      await expect(panel.getByText("Original expert explanation 1.")).toBeVisible();
      await expect(panel.getByText("Expert 0 reasoning")).toHaveCount(0);
      await card.getByRole("tab", { name: "failed-model", exact: true }).click();
      await expect(panel).toContainText(zh ? "无有效分数，不参与合成" : "No valid score; excluded from synthesis");
      await expect(panel).not.toContainText("0/10");
      await card.getByRole("tab", { name: "model-0", exact: true }).click();
      await page.keyboard.press("ArrowRight");
      await expect(card.getByRole("tab", { name: "model-1", exact: true })).toBeFocused();
      await expect(panel).toContainText("Original expert explanation 1.");
      await expect(card.getByRole("textbox", { name: `Q${n} ${zh ? "最终得分" : "final score"}` })).toHaveValue("8.5");
    }
    await expect(page.getByText(/已隐藏|details are hidden/)).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('[data-question-id="q2"]').screenshot({ path: testInfo.outputPath("disputed-evidence.png") });
    for (const width of [320, 768, 1024, 1920]) {
      await page.setViewportSize({ width, height: 1000 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      const panel = page.locator('[data-question-id="q2"]').getByRole("tabpanel");
      await expect(panel.getByText("Original expert explanation 1.")).toBeVisible();
    }
    // Editing setup without regrading must not change the completed result on reload.
    editablePrimary = "provider-0";
    await page.reload();
    const oldResult = page.locator('[data-question-id="q1"]').getByRole("region", { name: zh ? "SmarTAI 批改结果" : "SmarTAI Grading Result" });
    await expect(oldResult.getByText("model-1（10/10, [0,0,1,9], conf=0.99）")).toBeVisible();
    await expect(oldResult.getByText("Original expert explanation 1.", { exact: true })).toBeVisible();
    await expect(oldResult.getByText("Expert 1 reasoning", { exact: true })).toBeVisible();
    await expect(oldResult).toContainText("8.5 / 10");
    // Only a new result carrying its own frozen primary selects the other model.
    resultPrimary = editablePrimary;
    await page.reload();
    await expect(oldResult.getByText("model-0（7/10, [0,0,1,6], conf=0.95）")).toBeVisible();
    await expect(oldResult.getByText("Original expert explanation 0.", { exact: true }).first()).toBeVisible();
    expect(setupReads).toBe(0);
    singleModel = true;
    resultPrimary = "provider-1";
    await page.reload();
    const singleResult = page.locator('[data-question-id="q1"]').getByRole("region", { name: zh ? "SmarTAI 批改结果" : "SmarTAI Grading Result" });
    await expect(singleResult.getByText("model-1（10/10, [0,0,1,9], conf=0.99）")).toBeVisible();
    await expect(singleResult.locator("dl")).toHaveCount(1);
    await singleResult.getByText(zh ? "查看各专家原始结果" : "View Original Model Results").click();
    await expect(singleResult.getByRole("tab")).toHaveCount(1);
    await expect(singleResult.getByRole("tabpanel").getByText("Original expert explanation 1.")).toBeVisible();
    expect(errors).toEqual([]);
  });
}
