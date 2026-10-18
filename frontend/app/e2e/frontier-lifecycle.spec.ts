import { expect, test, type Page } from "@playwright/test";
import type { ProblemInfo } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const task = {
  task_id: "asg_browser_demo", name: "Synthetic Demo", owner_id: "guest-owner",
  status: "problems_ready", workflow_revision: 1, problem_count: 4, student_count: 0,
  kb_docs: {}, kb_doc_count: 0, created_at: 1, updated_at: 1,
  problem_data: Object.fromEntries([1, 2, 3, 4].map(n => [`q${n}`, {
    q_id: `q${n}`, number: String(n), type: n === 4 ? "programming" : "short_answer", stem: `Question ${n}`,
    criterion: "One point for the answer", reference_answer: "42", max_score: 1,
    review_status: "needs_review",
    solution_code: n === 4 ? "def answer(): return 42" : null,
    test_cases: n === 4 ? [{ input: "[]", expected_output: "42", description: "Synthetic test", source: "llm_generated", sandbox_feasible: true }] : null,
  } satisfies ProblemInfo])),
  student_data: {},
};

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

async function guest(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("smartai_locale", "en-US");
    sessionStorage.setItem("smartai_frontier_token", "synthetic-guest-token");
    sessionStorage.setItem("smartai_frontier_owner", "guest-owner");
    sessionStorage.setItem("smartai_frontier_generation", "1");
    sessionStorage.setItem("smartai_frontier_tasks", '["asg_browser_demo"]');
  });
  return errors;
}

// Real production router, session guard, and async workflow; synthetic API
// responses only, so these timing regressions cannot spend provider quota.
test("a late task creation cannot reopen Demo after confirmed exit", async ({ page }) => {
  const errors = await guest(page);
  const creation = deferred();
  const received = deferred();
  const calls: string[] = [];
  await page.route(`${backend}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    calls.push(path);
    if (path === "/auth/me") return route.fulfill({ json: { id: "guest-owner", username: "Guest", role: "teacher", is_active: true } });
    if (path === "/auth/frontier-demo-session/end") return route.fulfill({ json: { status: "ended" } });
    if (path === "/tasks/") {
      received.resolve();
      await creation.promise;
      return route.fulfill({ json: { ...task, status: "draft", problem_count: 0 } });
    }
    return route.fulfill({ status: 401, json: { detail: "Unexpected guest request" } });
  });
  await page.goto("/frontier/live");
  await page.getByRole("button", { name: "Start real OCR + grading" }).click();
  await received.promise;
  page.once("dialog", dialog => dialog.accept());
  await page.getByRole("link", { name: "Register / Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  const response = page.waitForResponse(`${backend}/tasks/`);
  creation.resolve();
  await response;
  await expect(page.getByRole("heading", { name: "SmarTAI Intelligent Grading Workspace", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => ({ token: sessionStorage.getItem("smartai_frontier_token"), tasks: sessionStorage.getItem("smartai_frontier_tasks") }))).toEqual({ token: null, tasks: null });
  expect(calls).not.toContain("/auth/frontier-demo-session");
  expect(calls.some(path => path.startsWith(`/tasks/${task.task_id}/`))).toBe(false);
  expect(errors).toEqual([]);
});

test("switching language while restoring recognition releases the workflow controls", async ({ page }) => {
  const errors = await guest(page);
  const recognition = deferred();
  const received = deferred();
  let polls = 0;
  await page.route(`${backend}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/auth/me") return route.fulfill({ json: { id: "guest-owner", username: "Guest", role: "teacher", is_active: true } });
    if (path === `/tasks/${task.task_id}/state`) {
      polls += 1;
      if (polls === 1) return route.fulfill({ json: { ...task, status: "extracting_problems", problem_count: 0 } });
      if (polls === 2) { received.resolve(); await recognition.promise; }
      return route.fulfill({ json: task });
    }
    if (path === `/tasks/${task.task_id}`) return route.fulfill({ json: task });
    return route.fulfill({ status: 401, json: { detail: "Unexpected guest request" } });
  });
  await page.goto(`/frontier/live?taskId=${task.task_id}`);
  await received.promise;
  await page.getByRole("button", { name: "Switch to Chinese", exact: true }).click();
  recognition.resolve();
  await expect(page.getByRole("button", { name: "继续识别作答", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "重新开始", exact: true })).toBeEnabled();
  expect(polls).toBe(3);
  expect(errors).toEqual([]);
});

test('Full Workflow opens fixed metadata and preloaded source pages without implicit recognition', async ({ page }) => {
  const errors = await guest(page);
  const writes: string[] = [];
  const draft = { ...task, name: 'Preset Demo', status: 'draft', problem_count: 0, student_count: 0, problem_data: {}, semester_id: '2026-fall' };
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path.endsWith('/content-language/default')) return route.fulfill({ json: { language: 'en' } });
    if (request.method() === 'POST' || request.method() === 'PUT') writes.push(path);
    if (path === '/auth/me') return route.fulfill({ json: { id: 'guest-owner', username: 'Guest', role: 'teacher', is_active: true } });
    if (path === '/tasks/' || path === `/tasks/${task.task_id}` || path === `/tasks/${task.task_id}/state`) return route.fulfill({ json: draft });
    return route.fulfill({ json: [] });
  });
  await page.goto('/frontier/live');
  await page.getByRole('link', { name: 'Full Workflow', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${task.task_id}/edit$`));
  await expect(page.locator('#new-task-name')).toHaveValue('Preset Demo');
  await expect(page.locator('#new-task-name')).toBeDisabled();
  await expect(page.locator('form')).toHaveAttribute('title', /preset details and files are fixed/);
  await page.goto(`/tasks/${task.task_id}/upload/problems`);
  await expect(page.getByText('question_source.pdf', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Demo files cannot be replaced' })).toBeDisabled();
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  await page.goto(`/tasks/${task.task_id}/submissions/upload`);
  await expect(page.getByText('submissions_raw.zip', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Switch to Chinese', exact: true }).click();
  await expect(page.getByRole('button', { name: '演示文件不可替换' })).toBeDisabled();
  await expect(page.getByRole('link', { name: '详细流程', exact: true })).toBeVisible();
  expect(writes).toEqual(['/tasks/']);
  expect(errors).toEqual([]);
});

for (const locale of ["zh-CN", "en-US"]) {
  test(`simple Demo continues through ordinary confidence with live progress (${locale})`, async ({ page }, testInfo) => {
    const errors = await guest(page), zh = locale === "zh-CN";
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    const writes: string[] = [];
    let status = "problems_ready", gradingPolls = 0;
    const problems = Object.fromEntries(Object.entries(task.problem_data).map(([id, problem]) => [id, {
      ...problem, preparation_issues: [{ issue_id: id, q_id: id, field: "stem", code: "low_confidence", severity: "warning", status: "open", details: { confidence: .5 } }],
    }]));
    const students = { S1: { stu_id: "S1", stu_name: "Example", identity_status: "matched", stu_ans: Object.values(problems).map((problem, index) => ({
      q_id: problem.q_id, number: problem.number, type: problem.type, content: index === 1 ? "" : "A recognized answer", flag: ["recognition_low_confidence"],
    })) } };
    const setup = { schema_version: 1, selected_provider_ids: ["demo:vision"], primary_provider_id: "demo:vision", aggregation_method: "single", multi_sample_n: 1,
      knowledge_scope: "none", strictness: 50, allow_partial_credit: true, feedback_tone: "neutral", feedback_length: "medium", feedback_language: "en", suggest_corrections: true, low_confidence_threshold: .6, teacher_notes: "" };
    const current = () => ({ ...task, status, problem_data: problems, student_data: status === "problems_ready" ? {} : students, student_count: status === "problems_ready" ? 0 : 1 });
    await page.route(`${backend}/**`, async route => {
      const path = new URL(route.request().url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
      if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, workflow_revision: 1, configured: true, grading_setup: setup, suggested_setup: setup, available_experts: [{ provider_id: "demo:vision", enabled: true }], readiness: { ready: true, blocking_issues: [], warnings: [] } });
      if (route.request().method() !== "GET") {
        writes.push(path);
        if (path.endsWith("/parse_submissions")) { status = "submissions_ready"; return json({ status: "started", job_id: "parse" }); }
        if (path.endsWith("/grade")) { status = "grading"; return json({ status: "started", job_id: "grade" }); }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path.endsWith("/state")) {
        if (status === "grading" && ++gradingPolls >= 3) status = "graded";
        return json({ ...current(), progress: { phase: status === "grading" ? "grading" : "done", total_students: 1, total_questions: 4, completed_units: status === "graded" ? 4 : 1,
          total_steps: 4, completed_steps: 1, active: [], messages: status === "grading" || status === "graded" ? [] : [{ ts: 1, level: "info", message: "activity_completed" }],
          activity_latest: status === "grading" ? { sequence: gradingPolls, ts: 1, code: gradingPolls === 1 ? "responding" : "succeeded" } : null } });
      }
      if (path.endsWith("/result")) return json({ task_id: task.task_id, problem_data: problems, student_data: students, results: [{ student_id: "S1", student_name: "Example", corrections: Object.values(problems).map(problem => ({
        q_id: problem.q_id, score: 1, provisional_score: 1, max_score: 1, confidence: .5, comment: "Model feedback", steps: [], expert_results: [], requires_human_review: false, review_reasons: ["low_confidence"],
      })) }], timestamp: 1 });
      if (path === `/tasks/${task.task_id}`) return json(current());
      return json([]);
    });
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    await expect(page.getByRole("button", { name: zh ? "继续识别作答" : "Continue to submissions", exact: true })).toBeEnabled();
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(() => page.getByRole("progressbar").evaluate(el => Math.abs(el.getBoundingClientRect().width - el.firstElementChild!.getBoundingClientRect().width))).toBeLessThan(1);
    await page.getByRole("button", { name: zh ? "继续识别作答" : "Continue to submissions", exact: true }).click();
    await expect(page.getByRole("heading", { name: zh ? "本次识别的学生作答" : "Recognized submissions", exact: true })).toBeVisible();
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(() => page.getByRole("progressbar").evaluate(el => Math.abs(el.getBoundingClientRect().width - el.firstElementChild!.getBoundingClientRect().width))).toBeLessThan(1);
    await expect(page.getByRole("status")).toHaveText(zh ? "本次识别已完成" : "Recognition completed");
    await page.getByRole("progressbar").locator("..").screenshot({ path: testInfo.outputPath(`demo-recognition-completed-${locale}.png`) });
    await page.reload();
    await expect(page.getByRole("heading", { name: zh ? "本次识别的学生作答" : "Recognized submissions", exact: true })).toBeVisible();
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect(page.getByText(/recognition_low_confidence|置信度|Low confidence/)).toHaveCount(0);
    await page.getByRole("button", { name: zh ? "开始批改" : "Start grading", exact: true }).first().click();
    await expect(page.getByRole("status").filter({ hasText: zh ? "请求已发出，等待模型响应" : "Request sent; awaiting model response" })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: zh ? "已获得一份有效结果" : "Received a valid result" })).toBeVisible();
    await expect(page.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores", exact: true })).toBeVisible();
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(() => page.getByRole("progressbar").evaluate(el => Math.abs(el.getBoundingClientRect().width - el.firstElementChild!.getBoundingClientRect().width))).toBeLessThan(1);
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}`));
    expect(writes).toEqual([`/tasks/${task.task_id}/parse_submissions`, `/tasks/${task.task_id}/grade`]);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`demo-completed-${locale}.png`), fullPage: true });
  });
}
