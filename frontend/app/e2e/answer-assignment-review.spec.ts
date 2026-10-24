import { expect, test, type Page } from "@playwright/test";

async function setup(page: Page, matching = false) {
  const errors: string[] = [], mutations: Record<string, unknown>[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.addInitScript(() => {
    localStorage.setItem("smartai_token", "synthetic-review"); localStorage.setItem("smartai_locale", "zh-CN");
  });
  const problems = Object.fromEntries([1, 2].map(n => [`q${n}`, {
    q_id: `q${n}`, number: `Section ${n} / 1`, type: n === 1 ? "short" : "proof", stem: `Question in section ${n}`,
    criterion: "Award 10 points", max_score: 10, review_status: "confirmed", max_score_review_status: "confirmed",
  }]));
  const answers = [1, 2].map(n => ({ answer_id: `a${n}`, q_id: `q${n}`, number: problems[`q${n}`].number,
    type: problems[`q${n}`].type, content: `Original answer ${n}`, review_status: "pending", flag: matching && n === 1
      ? ["recognition_manual_pending", "recognition_matching_review", "recognition_failed"] : [] as string[] }));
  const task = { task_id: "assignment-review", name: "Assignment review", status: "submissions_ready", workflow_revision: 1,
    problem_data: problems, student_data: { S1: { stu_id: "S1", stu_name: "Synthetic student", identity_status: "matched", stu_ans: answers } },
    problem_count: 2, student_count: 1, created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path === "/auth/me") return json({ id: "teacher", username: "Synthetic teacher", role: "teacher", is_active: true });
    if (path === "/auth/activity") return json({ token: "synthetic-review" });
    if (request.method() === "PUT") {
      const body = request.postDataJSON(); mutations.push(body);
      expect(body.expected_workflow_revision).toBe(task.workflow_revision);
      const answer = answers.find(a => a.answer_id === body.answer_id)!;
      expect(path).toBe(`/tasks/assignment-review/students/S1/answers/${answer.q_id}`);
      if (body.target_q_id) {
        const problem = problems[body.target_q_id]; expect(problem).toBeTruthy();
        Object.assign(answer, { q_id: problem.q_id, number: problem.number, type: problem.type, review_status: "pending" });
      }
      if (body.content !== undefined) answer.content = body.content;
      if (body.review_status) {
        answer.review_status = answers.filter(a => a.q_id === answer.q_id).length > 1 ? "pending" : body.review_status;
        answer.flag = answer.flag.filter(f => !["recognition_manual_pending", "recognition_matching_review", "recognition_failed"].includes(f));
      }
      for (const a of answers) {
        a.flag = a.flag.filter(f => f !== "answer_assignment_conflict");
        if (answers.filter(other => other.q_id === a.q_id).length > 1) a.flag.push("answer_assignment_conflict");
      }
      task.workflow_revision++;
      return json({ status: "ok", answer, answers, workflow_revision: task.workflow_revision });
    }
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(task);
    return json([]);
  });
  return { errors, mutations, answers };
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test.describe(`assignment review ${viewport.width}`, () => {
    test.use({ viewport });
    test("save duplicate, reload, locate other answer, resolve swap and proceed", async ({ page }, testInfo) => {
      const { errors, mutations, answers } = await setup(page);
      await page.goto("/tasks/assignment-review/students/S1?question=a2");
      const first = page.locator('[data-question-id="a1"]'), second = page.locator('[data-question-id="a2"]');
      await expect(second.getByRole("combobox", { name: "题号", exact: true })).toHaveValue("q2");
      expect(await second.locator("select option").allTextContents()).toEqual(["Section 1 / 1", "Section 2 / 1"]);
      await second.getByRole("combobox", { name: "题号", exact: true }).selectOption("q1");
      await second.getByRole("button", { name: "保存", exact: true }).click();
      const dialog = page.getByRole("alertdialog");
      await expect(dialog).toBeVisible();
      expect(answers.map(a => a.q_id)).toEqual(["q1", "q1"]);
      await expect(dialog.getByRole("link")).toHaveAttribute("href", /question=a1$/);
      await page.screenshot({ path: testInfo.outputPath("saved-conflict.png") });
      await dialog.getByRole("link").click();
      await expect(page).toHaveURL(/question=a1$/);
      await page.reload();
      await expect(page.locator("article[data-question-id]")).toHaveCount(2);
      await expect(first).toContainText("Original answer 1"); await expect(second).toContainText("Original answer 2");
      await page.getByRole("button", { name: "进入批改设置", exact: true }).click();
      await expect(dialog).toBeVisible(); await dialog.getByRole("button", { name: "返回", exact: true }).click();
      for (const [card, text] of [[first, "Teacher edited first answer"], [second, "Teacher edited second answer"]] as const) {
        await card.getByRole("button", { name: "修改", exact: true }).click();
        await card.locator("textarea").fill(text);
        await card.getByRole("button", { name: /保存并确认/ }).click();
        await expect(dialog).toBeVisible(); await dialog.getByRole("button", { name: "返回", exact: true }).click();
      }
      await page.reload();
      await expect(first).toContainText("Teacher edited first answer"); await expect(second).toContainText("Teacher edited second answer");
      await page.goto("/tasks/assignment-review/submissions");
      await expect(page.getByRole("tab", { name: "需要处理 (2)" })).toBeVisible();
      const queue = page.getByRole("region", { name: "待复核队列" });
      await expect(queue.getByRole("link")).toHaveCount(2);
      await queue.locator('a[href*="question=a1"]').click();
      await first.getByRole("combobox", { name: "题号", exact: true }).selectOption("q2");
      await first.getByRole("button", { name: "保存", exact: true }).click();
      await expect(first.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
      await expect(page.getByText("同一题号有多份作答", { exact: false })).toHaveCount(0);
      await expect(dialog).toHaveCount(0);
      await expect(first).toContainText("Teacher edited first answer"); await expect(second).toContainText("Teacher edited second answer");
      await page.reload();
      await expect(first.getByRole("combobox", { name: "题号", exact: true })).toHaveValue("q2");
      await expect(second.getByRole("combobox", { name: "题号", exact: true })).toHaveValue("q1");
      await page.screenshot({ path: testInfo.outputPath("resolved-swap.png") });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.goto("/tasks/assignment-review/submissions");
      await expect(page.getByRole("tab", { name: "需要处理 (0)" })).toBeVisible();
      await page.getByRole("button", { name: "进入批改设置", exact: true }).click();
      await expect(page).toHaveURL(/grading-setup$/);
      expect(mutations.map(m => m.answer_id)).toEqual(["a2", "a1", "a2", "a1"]); expect(errors).toEqual([]);
    });
  });
}

for (const edit of [false, true]) test(`confirm matching doubt with unchanged text: editor=${edit}`, async ({ page }) => {
  const { errors, mutations } = await setup(page, true);
  await page.goto("/tasks/assignment-review/students/S1?question=a1");
  const card = page.locator('[data-question-id="a1"]');
  if (edit) await card.getByRole("button", { name: "修改", exact: true }).click();
  await card.getByRole("button", { name: edit ? /保存并确认/ : /待确认/ }).click();
  await expect(card.getByRole("button", { name: /已确认/ })).toBeVisible(); await page.reload();
  await expect(card.getByRole("button", { name: /已确认/ })).toBeVisible(); await expect(card).toContainText("Original answer 1");
  await page.goto("/tasks/assignment-review/submissions");
  await expect(page.getByRole("tab", { name: "需要处理 (0)" })).toBeVisible();
  await page.getByRole("button", { name: "进入批改设置", exact: true }).click();
  await expect(page).toHaveURL(/grading-setup$/); expect(mutations).toHaveLength(1);
  if (edit) expect(mutations[0].content).toBe("Original answer 1"); expect(errors).toEqual([]);
});

test("duplicate answers restore separate text and question-number drafts", async ({ page }) => {
  const { answers, mutations, errors } = await setup(page);
  answers[1].q_id = "q1"; answers[1].number = answers[0].number;
  answers.forEach(answer => { answer.flag = ["answer_assignment_conflict"]; });
  await page.goto("/tasks/assignment-review/students/S1?question=a1");
  const first = page.locator('[data-question-id="a1"]'), second = page.locator('[data-question-id="a2"]');
  for (const [card, content] of [[first, "First private draft"], [second, "Second private draft"]] as const) {
    await card.getByRole("button", { name: "修改", exact: true }).click();
    await card.locator("textarea").fill(content);
  }
  await first.getByRole("combobox", { name: "题号", exact: true }).selectOption("q2");
  await page.getByRole("button", { name: "暂存", exact: true }).click();
  await expect(page.getByText(/已暂存 ·/)).toBeVisible();
  await page.reload();
  await expect(first.locator("textarea")).toHaveValue("First private draft");
  await expect(second.locator("textarea")).toHaveValue("Second private draft");
  await expect(first.getByRole("combobox", { name: "题号", exact: true })).toHaveValue("q2");
  await expect(second.getByRole("combobox", { name: "题号", exact: true })).toHaveValue("q1");
  expect(mutations).toHaveLength(0);
  await first.getByRole("button", { name: "保存", exact: true }).click();
  await expect(first.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
  await expect(first.locator("textarea")).toHaveValue("First private draft");
  await expect(second.locator("textarea")).toHaveValue("Second private draft");
  expect(answers.map(answer => answer.content)).toEqual(["Original answer 1", "Original answer 2"]);
  expect(errors).toEqual([]);
});
