import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import type { StepScore } from "../src/types/task";

const examples = JSON.parse(execFileSync(process.env.SMARTAI_E2E_PYTHON ?? "python", ["-c",
  "import json; from backend.tests.test_display_followup import browser_fixture; print(json.dumps(browser_fixture()))"],
{ cwd: path.resolve(process.cwd(), "../.."), encoding: "utf8" })) as { name: string; score: number; steps: StepScore[] }[];
const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const taskId = "asg_display_followup";

for (const surface of ["teacher", "demo"]) for (const width of [390, 1440]) for (const locale of ["zh-CN", "en-US"]) {
  test(`display followup ${surface} ${width}px ${locale}`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN";
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(({ surface, locale, taskId }) => {
      localStorage.setItem("smartai_locale", locale);
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
      if (surface === "demo") {
        sessionStorage.setItem("smartai_frontier_token", "synthetic-guest-token");
        sessionStorage.setItem("smartai_frontier_owner", "display-owner");
        sessionStorage.setItem("smartai_frontier_generation", "1");
        sessionStorage.setItem("smartai_frontier_tasks", JSON.stringify([taskId]));
      }
    }, { surface, locale, taskId });
    const corrections = examples.map((example, index) => ({ q_id: `q${index + 1}`, type: "calculation",
      score: example.score, provisional_score: example.score, max_score: 10, confidence: .99,
      comment: `【${example.name} A（${example.score}.0/10, conf=0.99）】\nShared ${example.name} explanation.\n\n$$\nx^2=${index + 1}\n$$\n\nOriginal ${example.name}.\n\n———\n\n【${example.name} B（${example.score}.0/10, conf=0.99）】\nIndependent ${example.name} assessment.`,
      steps: example.steps, requires_human_review: false, review_reasons: [], synthesis_method: "weighted_average",
      expert_results: ["A", "B"].map((name, expertIndex) => ({ provider: `${example.name} ${name}`, provider_id: `model-${name}`,
        score: example.score, max_score: 10, confidence: .99, steps: example.steps,
        comment: expertIndex ? `Independent ${example.name} assessment.\n\nUnique ${example.name} review.` : `Shared ${example.name} explanation.\n\n$$\nx^2=${index + 1}\n$$\n\nOriginal ${example.name}.\n\nFinal ${example.name} review.` })) }));
    const problem_data = Object.fromEntries(corrections.map((correction, index) => [correction.q_id,
      { q_id: correction.q_id, number: String(index + 1), type: "calculation", stem: "Synthetic calculation.", criterion: "Frozen teacher rule.", max_score: 10 }]));
    const student_data = { S1: { stu_id: "S1", stu_name: "Synthetic student", identity_status: "matched",
      stu_ans: corrections.map((c, index) => ({ q_id: c.q_id, number: String(index + 1), type: "calculation", content: "Synthetic answer.", review_status: "confirmed", flag: [] })) } };
    const task = { task_id: taskId, owner_id: "display-owner", name: "Display followup", status: "graded", workflow_revision: 1,
      final_result_version: 0, problem_data, student_data, problem_count: 3, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
    await page.route(`${backend}/**`, async route => {
      const url = new URL(route.request().url()).pathname;
      if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (url === "/auth/me") return route.fulfill({ json: { id: "display-owner", username: "Synthetic teacher", role: "teacher", is_active: true } });
      if (url === "/auth/activity") return route.fulfill({ json: { token: "synthetic-teacher-token" } });
      if (url === `/tasks/${taskId}` || url.endsWith("/state")) return route.fulfill({ json: task });
      if (url.endsWith("/result")) return route.fulfill({ json: { ...task, primary_provider_id: "model-A", results: [{ student_id: "S1", student_name: "Synthetic student", corrections }] } });
      if (url.endsWith("/finalization")) return route.fulfill({ json: { task_id: taskId, remaining_review_count: 0, ready_for_confirmation: true } });
      if (url.endsWith("/grading-setup")) return route.fulfill({ json: { configured: true, grading_setup: {}, available_experts: [] } });
      return route.fulfill({ json: [] });
    });
    if (surface === "demo") await page.goto(`/frontier/live?taskId=${taskId}`);
    for (const [index, example] of examples.entries()) {
      if (surface === "teacher") await page.goto(`/tasks/${taskId}/review/S1/q${index + 1}`);
      const area = surface === "teacher" ? page.locator(`[data-question-id="q${index + 1}"]`).getByRole("region", { name: zh ? "SmarTAI 批改结果" : "SmarTAI Grading Result" }) : page.locator(".demo-content-tabs");
      if (surface === "teacher") await area.getByText(zh ? /^查看各专家(?:批改|原始)结果$/ : /^View (?:Expert Grading Details|Original Model Results)$/).click();
      const panel = surface === "teacher" ? area.getByRole("tabpanel") : area.locator("section").filter({ has: page.getByText(`${example.name} A`, { exact: true }) }).last();
      if (surface === "demo") {
        for (const text of [`Shared ${example.name} explanation.`, `Original ${example.name}.`, `Independent ${example.name} assessment.`, `Final ${example.name} review.`, `Unique ${example.name} review.`])
          await expect(area.getByText(text, { exact: true })).toHaveCount(1);
        await expect(area.getByText(/【/)).toHaveCount(0);
        await panel.locator("summary").click();
      }
      const amounts = await panel.locator("li > span:last-child").allTextContents();
      await expect(panel.locator(".katex-display")).toHaveCount(1);
      expect(amounts).toEqual((index < 2 ? ["5", "0", "-1"] : ["-1", "-1", "-1"]).map(value => `${value} ${zh ? "分" : "pts"}`));
      await expect(panel.getByText(`Final ${example.name} review.`, { exact: true })).toBeVisible();
      if (index === 2) {
        await expect(panel.getByText(zh ? "另解批改" : surface === "teacher" ? "Alternative solution" : "Alternative method", { exact: true })).toBeVisible();
        for (const n of [1, 2, 3]) await expect(panel.getByText(`Independent error ${n}`, { exact: true })).toBeVisible();
      }
      await panel.screenshot({ path: testInfo.outputPath(`display-${surface}-${width}-${locale}-${index}.png`) });
    }
    await expect(page).toHaveTitle(/SmarTAI/i);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(errors).toEqual([]);
    await page.reload();
    await expect(page.getByText("Final Historical alternative review.", { exact: true }).first()).toBeVisible();
  });
}
