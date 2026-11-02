import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import type { ExpertResult } from "../src/types/task";

// Exercise actual backend formatting, including the old saved-text projection.
// The Python helper uses fixed assessments and never invokes a provider.
const fixture = JSON.parse(execFileSync(process.env.SMARTAI_E2E_PYTHON ?? "python", ["-c",
  "import json; from backend.tests.test_math_feedback_presentation import browser_fixture; print(json.dumps(browser_fixture()))"],
{ cwd: path.resolve(process.cwd(), "../.."), encoding: "utf8" })) as {
  new: ExpertResult[]; legacy: ExpertResult[]; stored: ExpertResult[]; criterion: string; answer: string;
};
const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "asg_feedback";

for (const mode of ["new", "legacy"] as const) for (const mobile of [false, true]) {
  test(`${mode} feedback stays concise and keeps model differences on ${mobile ? "mobile" : "desktop"}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width: mobile ? 390 : 1440, height: mobile ? 844 : 1000 });
    await page.addInitScript(() => { localStorage.setItem("smartai_locale", "zh-CN"); localStorage.setItem("smartai_token", "synthetic-teacher-token"); });
    const problem_data = { q1: { q_id: "q1", number: "1", stem: "Synthetic group proof.", criterion: fixture.criterion, max_score: 10, type: "reasoning" } };
    const student_data = { S1: { stu_id: "S1", stu_name: "Synthetic student", identity_status: "matched",
      stu_ans: [{ q_id: "q1", content: fixture.answer, review_status: "confirmed", flag: [] }] } };
    const correction = { q_id: "q1", score: 6.5, provisional_score: 6.5, max_score: 10, confidence: .9,
      comment: "Synthetic synthesis.", steps: fixture[mode][0].steps, expert_results: fixture[mode],
      synthesis_method: "weighted_average", requires_human_review: false, review_status: "pending", review_reasons: [] };
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      const json = (value: unknown) => route.fulfill({ json: value });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (pathname === "/auth/me") return json({ id: "owner", username: "Synthetic teacher", role: "teacher", is_active: true });
      if (pathname === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (pathname.startsWith("/experts") || pathname === "/tags/") return json([]);
      if (pathname === `/tasks/${taskId}`) return json({ task_id: taskId, owner_id: "owner", name: "Feedback QA", status: "graded", workflow_revision: 1,
        final_result_version: 0, problem_data, student_data, problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 });
      if (pathname.endsWith("/result")) return json({ task_id: taskId, grading_run_id: "run", primary_provider_id: "model-0", problem_data, student_data,
        results: [{ student_id: "S1", student_name: "Synthetic student", corrections: [correction] }], timestamp: 1 });
      if (pathname.endsWith("/finalization")) return json({ task_id: taskId, workflow_revision: 1, final_result_version: 0,
        final_result_dirty: false, analysis_status: "not_started", remaining_review_count: 0, ready_for_confirmation: true });
      throw new Error(`Unexpected request ${request.method()} ${pathname}`);
    });
    await page.goto(`/tasks/${taskId}/review/S1/q1`);
    await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}/review/S1/q1`));
    expect(await page.title()).toBeTruthy();
    const card = page.locator('[data-question-id="q1"]');
    const result = card.getByRole("region", { name: "SmarTAI 批改结果" });
    await expect(result).toContainText("6.5 / 10");
    await expect(result).toContainText("[5,2,0]");
    await expect(result).toContainText("[5,1,0]");
    const mainSteps = result.locator("ol").first();
    await expect(mainSteps.locator("li")).toHaveCount(3);
    await expect(mainSteps.locator("li").nth(2)).toHaveText("3未完成生成整个群的证明，本项不得分。0 分");
    await expect(mainSteps).not.toContainText("证明所给两个元素生成整个群。");
    await expect(mainSteps.locator(".katex-error")).toHaveCount(0);
    await result.getByText("查看各专家原始结果", { exact: true }).click();
    await result.getByRole("tab", { name: "Synthetic Model B", exact: true }).click();
    const panel = result.getByRole("tabpanel");
    await expect(panel).toContainText("6/10, [5,1,0]");
    await expect(panel).toContainText("未说明原因，得部分分");
    await expect(panel.locator("ol li").nth(2)).toHaveText("3缺少生成群的论证，本项不得分。0 分");
    // The selected expert tab must not change the fixed primary-model block.
    await expect(mainSteps).toContainText("阶为 2 的论证成立");
    await expect(panel).not.toContainText("证明所给两个元素生成整个群。");
    await expect(card.getByRole("textbox", { name: "Q1 最终得分" })).toHaveValue("6.5");
    await expect(card.getByRole("region", { name: "评分标准", exact: true })).toContainText(fixture.criterion.split("\n")[2]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    const screenshotPath = testInfo.outputPath(`${mode}-${mobile ? "mobile" : "desktop"}.png`);
    if (mobile) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: screenshotPath, fullPage: true });
    } else {
      await result.screenshot({ path: screenshotPath });
    }
    await page.reload();
    await expect(result.locator("ol").first().locator("li").nth(2)).toHaveText("3未完成生成整个群的证明，本项不得分。0 分");
    expect(errors).toEqual([]);
  });
}
