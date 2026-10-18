import { expect, test, type Page } from "@playwright/test";
import fixtures from "../src/test/fixtures/questionAnalysisMaterials.json" with { type: "json" };

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const path = "/tasks/pause-acceptance/results/questions/q1";
const originalProblem = {
  q_id: "q1", number: "1", stem: "What is 4 + 4?", max_score: 10, criterion: "Eight",
  reference_answer: "8", type: "programming", review_status: "needs_review",
  ai_completion_provenance: { reference_answer: { model: "synthetic" }, test_cases: { model: "synthetic" } },
  test_cases: fixtures.functionCases,
};

// Complete controlled routes from the LOCAL-05/06 report. The student's confirmed
// answer and grading finalization are deliberately independent of material review.
async function mockResults(page: Page, locale: string, problem: Record<string, unknown> = originalProblem) {
  await page.addInitScript(locale => {
    localStorage.setItem("smartai_token", "synthetic-token"); // Explicit fake value, never a credential.
    localStorage.setItem("smartai_locale", locale);
  }, locale);
  const correction = {
    q_id: "q1", score: 8, provisional_score: 8, max_score: 10, confidence: .95, comment: "Valid grading", steps: [],
    requires_human_review: false, review_reasons: [], initial_review_reasons: [], review_status: "pending",
    teacher_score: null, teacher_comment: "", synthesis_method: "single",
    expert_results: [
      { provider: "expert-valid", score: 8, max_score: 10, confidence: .95, comment: "Valid completed assessment", steps: [] },
      { provider: "expert-cancelled", score: 0, max_score: 10, confidence: 0, comment: "cancelled", steps: [], error_kind: "cancelled" },
    ],
  };
  const task = {
    task_id: "pause-acceptance", owner_id: "teacher", name: "Paused user acceptance", status: "graded",
    workflow_revision: 1, problem_count: 1, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {},
    problem_data: { q1: problem },
    student_data: { S1: { stu_id: "S1", stu_name: "Local student", identity_status: "matched", stu_ans: [{ q_id: "q1", content: "8", review_status: "confirmed", flag: [] }] } },
  };
  await page.route(`${backend}/**`, route => {
    const routePath = new URL(route.request().url()).pathname;
    if (routePath === "/auth/me") return route.fulfill({ json: { id: "teacher", username: "Local teacher", role: "teacher", is_active: true, auth_scope: "user" } });
    if (routePath === "/auth/activity") return route.fulfill({ json: { token: "synthetic-token" } });
    if (routePath.endsWith("/result")) return route.fulfill({ json: { task_id: task.task_id, problem_data: task.problem_data, student_data: task.student_data, results: [{ student_id: "S1", student_name: "Local student", corrections: [correction] }], timestamp: 1 } });
    if (routePath.startsWith("/experts") || routePath === "/tags/") return route.fulfill({ json: [] });
    if (routePath.endsWith("/finalization")) return route.fulfill({ json: { task_id: task.task_id, task_status: "graded", workflow_revision: 1, remaining_review_count: 0, required_review_count: 0, confirmed_required_count: 0, remaining_reviews: [{ student_id: "S1", q_id: "q1", reason: "grading_paused" }], ready_for_confirmation: true } });
    if (routePath.endsWith("/teacher_comments")) return route.fulfill({ json: { comments: {} } });
    if (routePath.endsWith("/artifacts")) return route.fulfill({ json: { versions: [] } });
    return route.fulfill({ json: task });
  });
  return (updated: Record<string, unknown>) => { task.problem_data.q1 = updated; };
}

function panel(page: Page, title: string) {
  return page.getByRole("heading", { name: title, exact: true }).locator("xpath=ancestor::section[1]");
}

const browserErrors = new Map<string, string[]>();
test.beforeEach(async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  // Check browser errors after assertions, including all reloads in state matrices.
  browserErrors.set(info.testId, errors);
});

test.afterEach(async ({}, info) => {
  const errors = browserErrors.get(info.testId);
  browserErrors.delete(info.testId);
  expect(errors).toEqual([]);
});

for (const locale of ["en-US", "zh-CN"] as const) {
  const zh = locale === "zh-CN";
  const title = zh ? "代码 / 测试资料" : "Code / test materials";
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    test(`six function samples and unconfirmed sources ${locale} ${viewport.width}`, async ({ page }, info) => {
      await page.setViewportSize(viewport);
      await mockResults(page, locale);
      await page.goto(path);
      const materials = panel(page, title);
      await expect(materials.getByText(zh ? "前 2 项 / 共 6 项" : "First 2 of 6 test cases", { exact: true })).toBeVisible();
      await expect(materials.getByRole("article")).toHaveCount(2);
      await expect(materials.getByText("synthetic_function", { exact: true })).toHaveCount(2);
      for (const heading of [title, zh ? "标答 / 参考答案" : "Reference answer"]) {
        await expect(panel(page, heading).getByText(zh ? "SmarTAI 补全" : "SmarTAI completion", { exact: true })).toBeVisible();
      }
      await expect(page.getByText(/teacher-confirmed version|教师已确认版本/)).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("materials-preview.png"), fullPage: true });
      const toggle = materials.getByRole("button");
      await expect(toggle).toHaveAccessibleName(zh ? "查看全部 6 项" : "View all 6 test cases");
      await toggle.focus();
      await page.keyboard.press("Enter");
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      await expect(materials.getByRole("article")).toHaveCount(6);
      for (const [index, sample] of fixtures.functionCases.entries()) {
        const article = materials.getByRole("article", { name: zh ? `测试样例 ${index + 1}` : `Test case ${index + 1}`, exact: true });
        await article.scrollIntoViewIfNeeded();
        await expect(article.getByText("synthetic_function", { exact: true })).toBeVisible();
        await expect(article.locator("dd")).toHaveText(["synthetic_function", JSON.stringify(sample.function_args, null, 2), sample.expected_return], { useInnerText: false });
      }
      await expect(materials.getByText(zh ? "共 6 个测试样例" : "6 test cases", { exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: info.outputPath("materials-expanded.png"), fullPage: true });
      await toggle.focus();
      await page.keyboard.press("Space");
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
      await expect(materials.getByRole("article")).toHaveCount(2);
    });
  }

  test(`stdin, nested arguments, empty and missing fields ${locale}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const update = await mockResults(page, locale, { ...originalProblem, test_cases: fixtures.stdinCases, solution_code: "def solve():\\n    print(8)" });
    await page.goto(path);
    const materials = panel(page, title);
    await materials.getByRole("button", { name: zh ? "查看全部 6 项" : "View all 6 test cases", exact: true }).click();
    await expect(materials.getByRole("article")).toHaveCount(6);
    for (const [index, sample] of fixtures.stdinCases.entries()) {
      const article = materials.getByRole("article").nth(index);
      await expect(article.locator("dt")).toHaveText(zh ? ["输入", "期望输出"] : ["Input", "Expected output"]);
      // textContent preserves newlines and full long strings without normalizing whitespace.
      expect(await article.locator("dd").allTextContents()).toEqual([sample.input || '""', sample.expected_output || '""']);
    }
    await expect(materials.getByText("ignored_function", { exact: true })).toHaveCount(0);
    await expect(materials.getByText("ignored_return", { exact: true })).toHaveCount(0);
    await expect(materials.getByText("def solve():\n    print(8)", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: info.outputPath("stdin-mobile.png"), fullPage: true });
    update({ ...originalProblem, test_cases: fixtures.functionBoundaries });
    await page.reload();
    await materials.getByRole("button", { name: zh ? "查看全部 3 项" : "View all 3 test cases", exact: true }).click();
    const articles = materials.getByRole("article");
    await expect(articles).toHaveCount(3);
    expect(await articles.nth(0).locator("dd").allTextContents()).toEqual(["no_arguments", "[]", '""']);
    expect(await articles.nth(1).locator("dd").allTextContents()).toEqual(["nested_arguments", JSON.stringify(fixtures.functionBoundaries[1].function_args, null, 2), "{'ok': False}"]);
    await expect(articles.nth(2).locator("dd")).toHaveText(Array(3).fill(zh ? "未提供" : "Not provided"));
  });

  for (const source of ["ai", "imported", "recognized", "teacher"] as const) {
    test(`source and material lifecycle ${source} ${locale}`, async ({ page }) => {
      const update = await mockResults(page, locale);
      const expectedSource = source === "ai" ? (zh ? "SmarTAI 补全" : "SmarTAI completion") : source === "imported"
        ? (zh ? "资料导入 · synthetic.txt" : "Imported · synthetic.txt") : (zh ? "题目识别 / 教师维护" : "Recognition / teacher maintained");
      for (const state of ["pending", "confirmed", "edited", "regenerated", "stale-confirmed"] as const) {
        const changed = ["edited", "regenerated", "stale-confirmed"].includes(state);
        const status = state === "confirmed" || state === "stale-confirmed" ? "confirmed" : state === "edited" ? "edited" : "pending";
        const provenance = source === "ai"
          ? { job_id: "synthetic", candidate_id: "synthetic", source_kind: "ai_generated", provider_id: "synthetic", review_status: status, generated_at: 1, updated_at: changed ? 2 : 1 }
          : { import_job_id: "synthetic", candidate_id: "synthetic", source_kind: "upload", source_filename: "synthetic.txt", confidence: 1, match_status: "exact", source_excerpt: "8", source_location: "1", reason: "Exact match", review_status: status, imported_at: 1, updated_at: changed ? 2 : 1 };
        update({
          ...originalProblem, reference_answer: changed ? "Updated answer" : "8",
          test_cases: changed ? fixtures.functionBoundaries : source === "teacher" ? fixtures.stdinCases : fixtures.functionCases,
          review_status: status === "pending" ? "needs_review" : status, max_score_review_status: "confirmed", preparation_issues: [],
          ai_completion_provenance: source === "ai" ? { reference_answer: provenance, test_cases: provenance } : {},
          material_provenance: source === "imported" ? { reference_answer: provenance, test_cases: provenance } : {},
        });
        await page.goto(path);
        for (const heading of [title, zh ? "标答 / 参考答案" : "Reference answer"]) {
          await expect(panel(page, heading).getByText(expectedSource, { exact: true })).toBeVisible();
          await expect(panel(page, heading).getByText(/teacher-confirmed|教师已确认/)).toHaveCount(0);
        }
        await expect(panel(page, zh ? "标答 / 参考答案" : "Reference answer").getByText(changed ? "Updated answer" : "8", { exact: true })).toBeVisible();
      }
    });
  }
}
