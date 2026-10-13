import { expect, test, type Page } from "@playwright/test";

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
  }])),
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
  await expect(page.getByRole("button", { name: "确认题目资料并继续", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "重新开始", exact: true })).toBeEnabled();
  expect(polls).toBe(3);
  expect(errors).toEqual([]);
});
