import { expect, test, type Page } from "@playwright/test";
import type { ProblemInfo, Task } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const task = {
  task_id: "asg_browser_demo", name: "Synthetic Demo", owner_id: "guest-owner",
  status: "problems_ready", workflow_revision: 1, problem_count: 4, student_count: 0,
  kb_docs: {}, kb_doc_count: 0, created_at: 1, updated_at: 1,
  problem_data: Object.fromEntries([1, 2, 3, 4].map(n => [`q${n}`, {
    q_id: `q${n}`, number: String(n), type: n === 4 ? "programming" : "concept", stem: `Question ${n}`,
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

for (const locale of ["zh-CN", "en-US"]) {
  test(`required review uses existing editors and returns to the same Quick Demo (${locale})`, async ({ page }, testInfo) => {
    const errors = await guest(page), zh = locale === "zh-CN";
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    const problems: Task["problem_data"] = structuredClone(task.problem_data);
    problems.q1.stem = "";
    const students: Task["student_data"] = { S1: { stu_id: "S1", stu_name: "[Unknown Student]", identity_status: "needs_review", source_id: "source1",
      stu_ans: Object.values(problems).map(problem => ({ q_id: problem.q_id, number: problem.number, type: problem.type, content: "Saved answer", flag: [], review_status: "pending" })) } };
    let status = "problems_ready", revision = 1;
    const writes: string[] = [];
    const current = () => ({ ...task, status, workflow_revision: revision, problem_data: problems, student_data: status === "problems_ready" ? {} : students, student_count: status === "problems_ready" ? 0 : 1 });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
      if (request.method() !== "GET") {
        writes.push(path);
        if (path.endsWith("/problems/q1")) {
          const patch = request.postDataJSON();
          expect(patch.expected_workflow_revision).toBe(revision);
          const repairedEmptyStem = !problems.q1.stem.trim() && Boolean(patch.stem?.trim());
          Object.assign(problems.q1, patch);
          if (repairedEmptyStem) {
            problems.q1.review_status = "needs_review";
            problems.q1.preparation_issues = [{ issue_id: "required-repair-q1", q_id: "q1", field: "stem",
              severity: "blocking", status: "resolved", code: "required_review_confirmation" }];
          }
          if (patch.review_status === "confirmed") {
            expect(problems.q1.stem).toBe("Saved question text");
            problems.q1.max_score_review_status = "confirmed";
          }
          return json({ status: "ok", q_id: "q1", problem: problems.q1, workflow_revision: ++revision });
        }
        if (path.endsWith("/students/S1/identity")) {
          const patch = request.postDataJSON();
          expect(patch.expected_workflow_revision).toBe(revision);
          students.S1 = { ...students.S1, stu_name: patch.student_name, identity_status: "matched" };
          return json({ status: "ok", previous_student_id: "S1", student: students.S1, workflow_revision: ++revision });
        }
        if (path.endsWith("/parse_submissions")) { status = "submissions_ready"; return json({ status: "started", job_id: "parse" }); }
        if (path.endsWith("/grade")) { status = "graded"; return json({ status: "started", job_id: "grade" }); }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(current());
      if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, workflow_revision: revision, configured: true,
        grading_setup: { primary_provider_id: "demo:vision" }, available_experts: [{ provider_id: "demo:vision", enabled: true }] });
      if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: revision, problem_source: null, submission_sources: {} });
      if (path.endsWith("/result")) return json({ ...current(), results: [], timestamp: 1 });
      return json([]);
    });
    async function returnToDemo(hoverNotice = false) {
      const quick = page.getByRole("link", { name: zh ? "简明演示" : "Quick Demo", exact: true });
      if (!await quick.isVisible()) {
        if (hoverNotice) {
          const notice = page.locator('[data-sonner-toast][data-type="success"]').first();
          await expect(notice).toBeVisible();
          // Hover pauses toast dismissal: navigation must remain usable while
          // the real notice is present, not only after its timer expires.
          await notice.hover();
        }
        await page.getByRole("button", { name: zh ? "打开导航" : "Open navigation", exact: true }).click({ timeout: 3000 });
        if (hoverNotice) {
          await expect(page.locator('[data-sonner-toast][data-type="success"]').first()).toBeVisible();
          // Check the hit target immediately, while the notice is still present.
          // A click that only succeeds after toast dismissal would hide the bug.
          expect(await quick.evaluate(link => {
            const rect = link.getBoundingClientRect();
            return link.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
          })).toBe(true);
          await page.screenshot({ path: testInfo.outputPath("mobile-navigation-with-notice.png") });
        }
      }
      await quick.click();
      await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}$`));
    }
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    const questionReview = page.getByRole("alert").getByRole("link", { name: zh ? "审核题目" : "Review questions", exact: true });
    await expect(questionReview).toHaveAttribute("href", `/tasks/${task.task_id}/questions`);
    await page.getByRole("alert").screenshot({ path: testInfo.outputPath(`required-questions-${locale}.png`) });
    await questionReview.click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${task.task_id}/questions$`));
    // Returning without saving leaves the blocker in place and performs no write.
    await returnToDemo();
    await expect(questionReview).toBeVisible();
    expect(writes).toEqual([]);
    await questionReview.click();
    await page.locator(`a[href="/tasks/${task.task_id}/questions/q1/content#question-q1"]`).first().click();
    const firstQuestion = page.locator('[data-question-id="q1"]');
    await firstQuestion.getByRole("button", { name: zh ? "题目 · 修改" : "Question · Edit", exact: true }).click();
    await firstQuestion.getByRole("textbox", { name: zh ? "题目" : "Question", exact: true }).fill("Saved question text");
    await firstQuestion.getByRole("button", { name: zh ? "保存" : "Save", exact: true }).click();
    await expect(firstQuestion.getByText("Saved question text", { exact: true })).toBeVisible();
    await returnToDemo();
    const continueButton = page.getByRole("button", { name: zh ? "继续识别作答" : "Continue to submissions", exact: true });
    // Real repairs retain a required-confirmation marker until the teacher confirms.
    await expect(questionReview).toBeVisible();
    await expect(continueButton).toHaveCount(0);
    expect(writes).toEqual([`/tasks/${task.task_id}/problems/q1`]);
    await questionReview.click();
    await page.locator(`a[href="/tasks/${task.task_id}/questions/q1/content#question-q1"]`).first().click();
    await firstQuestion.getByRole("button", { name: zh ? "第 1 题 · 待确认" : "Question 1 · Confirm review", exact: true }).click();
    await expect(firstQuestion.getByRole("button", { name: zh ? "第 1 题 · 已确认" : "Question 1 · Confirmed", exact: true })).toBeVisible();
    await returnToDemo(true);
    await expect(continueButton).toBeEnabled();
    expect(writes).toEqual([`/tasks/${task.task_id}/problems/q1`, `/tasks/${task.task_id}/problems/q1`]);
    await continueButton.click();
    const submissionReview = page.getByRole("alert").getByRole("link", { name: zh ? "校对作答" : "Review submissions", exact: true });
    await expect(submissionReview).toHaveAttribute("href", `/tasks/${task.task_id}/submissions`);
    await expect(page.getByRole("button", { name: zh ? "重试失败项" : "Retry failed items", exact: true })).toHaveCount(0);
    await page.getByRole("alert").screenshot({ path: testInfo.outputPath(`required-submissions-${locale}.png`) });
    await submissionReview.click();
    await page.locator(`a[href="/tasks/${task.task_id}/students/S1?identity=edit"]`).first().click();
    await page.getByRole("textbox", { name: zh ? "姓名" : "Name", exact: true }).fill("Saved student name");
    await page.getByRole("button", { name: zh ? "保存并确认身份" : "Save and confirm identity", exact: true }).click();
    await expect(page.getByRole("textbox", { name: zh ? "姓名" : "Name", exact: true })).toHaveCount(0);
    await returnToDemo(true);
    const grade = page.getByRole("button", { name: zh ? "开始批改" : "Start grading", exact: true }).first();
    await expect(grade).toBeEnabled();
    expect(writes).toEqual([`/tasks/${task.task_id}/problems/q1`, `/tasks/${task.task_id}/problems/q1`, `/tasks/${task.task_id}/parse_submissions`, `/tasks/${task.task_id}/students/S1/identity`]);
    await grade.click();
    await expect(page.getByRole("heading", { name: zh ? "本次批改的具体情况" : "Detailed grading results", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: zh ? "学情分析" : "Learning analysis", exact: true }).click();
    await expect(page.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores", exact: true })).toBeVisible();
    expect(writes.at(-1)).toBe(`/tasks/${task.task_id}/grade`);
    expect(writes).toHaveLength(5);
    expect(errors).toEqual([]);
  });
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

for (const locale of ["zh-CN", "en-US"]) for (const width of [1440, 390, 320]) {
  test(`Quick Demo content tabs preserve recognition and grading (${locale}, ${width}px)`, async ({ page }, testInfo) => {
    const errors = await guest(page), zh = locale === "zh-CN";
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const students: Task["student_data"] = Object.fromEntries(["Alex Chen", "Maya Lin"].map((name, index) => {
      const id = `S${index + 1}`;
      return [id, { stu_id: id, stu_name: name, identity_status: "matched", source_filename: `${id}.pdf`,
        stu_ans: Object.values(task.problem_data).map(problem => ({ q_id: problem.q_id, number: problem.number, type: problem.type,
          content: `Saved answer ${id} ${problem.q_id}`, flag: [], review_status: "confirmed" })) }];
    }));
    let status: Task["status"] = "problems_ready";
    const writes: string[] = [];
    const current = () => ({ ...task, status, student_data: status === "problems_ready" ? {} : students, student_count: status === "problems_ready" ? 0 : 2 });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
      if (request.method() !== "GET") {
        writes.push(path);
        if (path.endsWith("/parse_submissions")) { status = "submissions_ready"; return json({ status: "started", job_id: "parse" }); }
        if (path.endsWith("/grade")) { status = "grading"; return json({ status: "started", job_id: "grade" }); }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(current());
      if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, workflow_revision: 1, configured: true,
        grading_setup: { primary_provider_id: "demo:vision" }, available_experts: [{ provider_id: "demo:vision", enabled: true }] });
      if (path.endsWith("/result")) return json({ ...current(), results: Object.values(students).map((student, index) => ({
        student_id: student.stu_id, student_name: student.stu_name, student_answers: student.stu_ans,
        corrections: Object.values(task.problem_data).map(problem => ({ q_id: problem.q_id, type: problem.type,
          score: index === 0 ? 0 : 1, provisional_score: index === 0 ? 0 : 1, max_score: 1, confidence: 0.9,
          comment: `Feedback ${student.stu_id} ${problem.q_id}`, steps: [{ step_no: 1, desc: `Scoring basis ${student.stu_id} ${problem.q_id}`, score: index === 0 ? 0 : 1, is_correct: index !== 0 }],
          expert_results: [], requires_human_review: false, review_reasons: [],
        })),
      })) });
      return json([]);
    });
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    const tabs = page.locator(".demo-content-tabs"), list = tabs.getByRole("tablist", { name: zh ? "演示内容" : "Demo content" });
    const tab = (name: string, en: string) => list.getByRole("tab", { name: zh ? name : en, exact: true });
    await expect(list.getByRole("tab")).toHaveCount(1);
    await page.getByRole("button", { name: zh ? "继续识别作答" : "Continue to submissions", exact: true }).click();
    await expect(list.getByRole("tab")).toHaveCount(2);
    await expect(tab("学生作答", "Student answers")).toHaveAttribute("aria-selected", "true");
    await expect(tabs.getByText("Saved answer S1 q1", { exact: true })).toBeVisible();
    await tab("题目资料", "Materials").click();
    await page.getByRole("button", { name: zh ? "开始批改" : "Start grading", exact: true }).first().click();
    await expect(page.getByRole("button", { name: zh ? "正在运行…" : "Running…", exact: true })).toBeDisabled();
    await expect(tab("题目资料", "Materials")).toHaveAttribute("aria-selected", "true");
    await tab("学生作答", "Student answers").click();
    await expect(tabs.getByText("Saved answer S1 q1", { exact: true })).toBeVisible();
    await expect(list.getByRole("tab")).toHaveCount(2);
    await page.reload();
    await expect(list.getByRole("tab")).toHaveCount(2);
    await tab("题目资料", "Materials").click();
    await expect(tabs.getByRole("heading", { name: zh ? "本次生成的题目资料" : "Generated question materials" })).toBeVisible();
    await tab("学生作答", "Student answers").click();
    status = "graded";
    await expect(list.getByRole("tab")).toHaveCount(4);
    await expect(tab("批改详情", "Grading details")).toHaveAttribute("aria-selected", "true");
    const detail = tabs.getByRole("tabpanel", { name: zh ? "批改详情" : "Grading details" });
    await expect(detail.getByText("Feedback S1 q1", { exact: true })).toBeVisible();
    await expect(detail.getByText("Saved answer S1 q1", { exact: true })).toHaveCount(0);
    await expect(detail.getByText("Question 1", { exact: true })).toHaveCount(0);
    await expect(detail.getByText("Scoring basis S1 q1", { exact: true })).not.toBeVisible();
    await detail.locator("summary").first().click();
    await expect(detail.getByText("Scoring basis S1 q1", { exact: true })).toBeVisible();
    await detail.locator("summary").first().click();
    await expect(detail.getByText("Scoring basis S1 q1", { exact: true })).not.toBeVisible();
    expect(await list.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgba(0, 0, 0, 0)");
    const splitWords = await list.evaluate(element => [...element.querySelectorAll("button")].flatMap(button => {
      const node = button.firstChild;
      if (!node || node.nodeType !== Node.TEXT_NODE) return [];
      return [...(node.textContent ?? "").matchAll(/\S+/g)].flatMap(match => {
        const range = document.createRange();
        range.setStart(node, match.index!);
        range.setEnd(node, match.index! + match[0].length);
        return range.getClientRects().length > 1 ? [match[0]] : [];
      });
    }));
    expect(splitWords).toEqual([]);
    expect(await tab("批改详情", "Grading details").evaluate(element => getComputedStyle(element).borderTopLeftRadius)).toBe("10px");
    await list.scrollIntoViewIfNeeded();
    await page.evaluate(() => window.scrollBy(0, -90));
    await tabs.screenshot({ path: testInfo.outputPath(`demo-content-${locale}-${width}.png`) });
    await tab("学情分析", "Learning analysis").click();
    await expect(tabs.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores" })).toBeVisible();
    const charts = tabs.getByRole("tablist", { name: zh ? "选择分析图" : "Choose analysis chart" });
    for (const number of ["01", "02", "03", "04"]) await charts.getByRole("tab", { name: number, exact: true }).click();
    await tab("批改详情", "Grading details").click();
    await tab("学情分析", "Learning analysis").click();
    await expect(charts.getByRole("tab", { name: "04" })).toHaveAttribute("aria-selected", "true");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    expect(writes).toEqual([`/tasks/${task.task_id}/parse_submissions`, `/tasks/${task.task_id}/grade`]);
    expect(errors).toEqual([]);
  });
}

for (const stage of ["problems_ready", "submissions_ready"] as const) for (const locale of ["zh-CN", "en-US"]) {
  test(`Quick Demo syncs two-page content and restores paused grading (${stage}, ${locale})`, async ({ page, context }, testInfo) => {
    const second = await context.newPage(), zh = locale === "zh-CN";
    const errors = await guest(page), secondErrors = await guest(second);
    for (const currentPage of [page, second]) {
      await currentPage.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
      await currentPage.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    }
    let status: Task["status"] = stage, revision = 1, gradingJob: string | null = null;
    const problems = structuredClone(task.problem_data);
    const students: Task["student_data"] = { S1: { stu_id: "S1", stu_name: "Alex Chen", identity_status: "matched",
      stu_ans: Object.values(problems).map(problem => ({ q_id: problem.q_id, number: problem.number, type: problem.type,
        content: `Original answer ${problem.q_id}`, flag: [], review_status: "confirmed" })) } };
    const writes: Array<{ page: string; path: string }> = [];
    const current = () => ({ ...task, status, workflow_revision: revision, problem_data: problems,
      student_data: status === "problems_ready" ? {} : students, student_count: status === "problems_ready" ? 0 : 1,
      grading_job_id: gradingJob, active_job_id: status === "grading" ? gradingJob : null });
    await context.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
      if (request.method() !== "GET") {
        writes.push({ page: request.frame().page() === page ? "A" : "B", path });
        if (path.endsWith("/grade")) {
          expect(request.postDataJSON().expected_workflow_revision).toBe(revision);
          if (status === "grading_paused") expect(request.postDataJSON().retry_scope).toBe("failed_only");
          status = "grading"; gradingJob = `grade-${++revision}`;
          return json({ status: "started", job_id: gradingJob });
        }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(current());
      if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, workflow_revision: revision, configured: true,
        grading_setup: { primary_provider_id: "demo:vision" }, available_experts: [{ provider_id: "demo:vision", enabled: true }] });
      if (path.endsWith("/result")) return json({ ...current(), results: [] });
      return json([]);
    });
    const url = `/frontier/live?taskId=${task.task_id}`;
    await page.goto(url);
    const tabs = page.locator(".demo-content-tabs"), tab = (name: string, en: string) => tabs.getByRole("tab", { name: zh ? name : en, exact: true });
    await expect(tabs.getByRole("tab")).toHaveCount(stage === "problems_ready" ? 1 : 2);
    if (stage === "submissions_ready") await expect(tabs.getByText("Original answer q1", { exact: true })).toBeVisible();
    // Persisted recognition/edits made in B while A retains its previous view.
    status = "submissions_ready"; revision++;
    students.S1.stu_ans[0].content = "Latest saved answer from page B";
    problems.q1.stem = "Latest saved question from page B";
    await second.goto(url);
    await second.getByRole("button", { name: zh ? "开始批改" : "Start grading", exact: true }).first().click();
    await expect(second.getByRole("button", { name: zh ? "正在运行…" : "Running…", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: stage === "problems_ready" ? (zh ? "继续识别作答" : "Continue to submissions") : (zh ? "开始批改" : "Start grading"), exact: true }).first().click();
    await expect(tabs.getByRole("tab")).toHaveCount(2);
    await tab("学生作答", "Student answers").click();
    await expect(tabs.getByText("Latest saved answer from page B", { exact: true })).toBeVisible();
    await expect(tabs.getByText("Original answer q1", { exact: true })).toHaveCount(0);
    await tab("题目资料", "Materials").click();
    await expect(tabs.getByText("Latest saved question from page B", { exact: true })).toBeVisible();
    expect(writes).toEqual([{ page: "B", path: `/tasks/${task.task_id}/grade` }]);
    // The server has persisted a pause; refresh must reload content without
    // restarting recognition or grading. Only explicit Continue may resume it.
    status = "grading_paused"; revision++;
    await expect(page.getByRole("button", { name: zh ? "继续批改" : "Continue grading", exact: true })).toBeEnabled();
    await page.reload();
    await expect(tabs.getByRole("tab")).toHaveCount(2);
    await expect(tabs.getByText("Latest saved answer from page B", { exact: true })).toBeVisible();
    await tab("题目资料", "Materials").click();
    await expect(tabs.getByText("Latest saved question from page B", { exact: true })).toBeVisible();
    expect(writes).toHaveLength(1);
    await tabs.scrollIntoViewIfNeeded();
    await page.evaluate(() => window.scrollBy(0, -90));
    await page.screenshot({ path: testInfo.outputPath("paused-restored-content.png") });
    await page.getByRole("button", { name: zh ? "继续批改" : "Continue grading", exact: true }).click();
    await expect(page.getByRole("button", { name: zh ? "正在运行…" : "Running…", exact: true })).toBeDisabled();
    await expect(tab("题目资料", "Materials")).toHaveAttribute("aria-selected", "true");
    await tab("学生作答", "Student answers").click();
    await expect(tabs.getByText("Latest saved answer from page B", { exact: true })).toBeVisible();
    status = "graded";
    await expect(tabs.getByRole("tab")).toHaveCount(4);
    expect(writes).toEqual([{ page: "B", path: `/tasks/${task.task_id}/grade` }, { page: "A", path: `/tasks/${task.task_id}/grade` }]);
    expect(errors).toEqual([]);
    expect(secondErrors).toEqual([]);
  });
}

for (const stage of ["problems_ready", "submissions_ready"] as const) for (const locale of ["zh-CN", "en-US"]) {
  test(`stale ${stage} review retries the latest recognition failure inline (${locale})`, async ({ page }) => {
    const errors = await guest(page), zh = locale === "zh-CN", submissions = stage === "submissions_ready";
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    let status: Task["status"] = stage, revision = 1;
    const writes: { path: string; body: Record<string, unknown> }[] = [];
    const retryPath = `/tasks/${task.task_id}/${submissions ? "submission-recognition" : "question-preparation"}/recognition-job/retry`;
    const current = () => ({ ...task, status, workflow_revision: revision,
      error: status === "error" ? "provider_timeout" : null, last_failed_job_id: status === "error" ? "recognition-job" : null,
      extract_job_id: submissions ? "successful-questions" : "recognition-job", parse_job_id: submissions ? "recognition-job" : null,
      problem_data: status === "error" && !submissions ? {} : task.problem_data,
      student_data: submissions && status !== "error" ? { S1: { stu_id: "S1", stu_name: "Example", identity_status: "matched", stu_ans: [] } } : {},
    });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname, json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "guest-owner", username: "Guest", role: "teacher", is_active: true });
      if (request.method() !== "GET") {
        writes.push({ path, body: request.postDataJSON() });
        expect(path).toBe(retryPath);
        expect(writes[0].body.expected_workflow_revision).toBe(2);
        expect(writes[0].body.acknowledge_possible_duplicate_call).toBe(true);
        status = stage; revision = 3;
        return json({ status: "started", job_id: "recognition-job" });
      }
      if (path === `/tasks/${task.task_id}` || path.endsWith("/state")) return json(current());
      if (path.endsWith("/grading-setup")) return json({ task_id: task.task_id, workflow_revision: revision, configured: true,
        grading_setup: { primary_provider_id: "demo:vision" }, available_experts: [{ provider_id: "demo:vision", enabled: true }] });
      return json([]);
    });
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    const nextLabel = submissions ? (zh ? "开始批改" : "Start grading") : (zh ? "继续识别作答" : "Continue to submissions");
    const next = page.getByRole("button", { name: nextLabel, exact: true }).first();
    await expect(next).toBeEnabled();
    // Simulate another tab restarting recognition and receiving a model failure.
    status = "error"; revision = 2;
    await next.click();
    const alert = page.getByRole("alert");
    await expect(alert.getByRole("link")).toHaveCount(0);
    const retry = alert.getByRole("button", { name: zh ? "重试失败项" : "Retry failed items", exact: true });
    await expect(retry).toBeEnabled();
    expect(writes).toEqual([]);
    await retry.click();
    await expect(next).toBeEnabled();
    await expect(alert).toHaveCount(0);
    expect(writes.map(item => item.path)).toEqual([retryPath]);
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}$`));
    expect(errors).toEqual([]);
  });
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
  test(`restoring questions after a network error waits for explicit Continue (${locale})`, async ({ page }) => {
    const errors = await guest(page), zh = locale === "zh-CN";
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    const writes: string[] = [];
    let failLoad = true, status = "problems_ready";
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path === "/auth/me") return route.fulfill({ json: { id: "guest-owner", username: "Guest", role: "teacher", is_active: true } });
      if (request.method() !== "GET") {
        writes.push(path);
        if (path.endsWith("/parse_submissions")) { status = "submissions_ready"; return route.fulfill({ json: { status: "started", job_id: "parse" } }); }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path.endsWith("/grading-setup")) return route.fulfill({ json: { available_experts: [{ provider_id: "demo:vision", enabled: true }] } });
      if (path.endsWith("/state")) return route.fulfill({ json: { ...task, status } });
      if (path === `/tasks/${task.task_id}`) {
        if (failLoad) { failLoad = false; return route.fulfill({ status: 503, json: { detail: "Temporary read failure" } }); }
        return route.fulfill({ json: { ...task, status } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    await page.getByRole("button", { name: zh ? "重试失败项" : "Retry failed items", exact: true }).click();
    const next = page.getByRole("button", { name: zh ? "继续识别作答" : "Continue to submissions", exact: true });
    await expect(next).toBeEnabled();
    expect(writes).toEqual([]);
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}`));
    await next.click();
    await expect(page.getByRole("heading", { name: zh ? "本次识别的学生作答" : "Recognized submissions", exact: true })).toBeVisible();
    expect(writes).toEqual([`/tasks/${task.task_id}/parse_submissions`]);
    expect(errors).toEqual([]);
  });

  test(`simple Demo keeps model errors and failed-item retries on the same page (${locale})`, async ({ page }, testInfo) => {
    const errors = await guest(page), zh = locale === "zh-CN";
    await page.setViewportSize(zh ? { width: 1440, height: 1000 } : { width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("smartai_locale", value), locale);
    const writes: { path: string; body: Record<string, unknown> }[] = [];
    let completed = false;
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path === "/auth/me") return route.fulfill({ json: { id: "guest-owner", username: "Guest", role: "teacher", is_active: true } });
      if (request.method() !== "GET") {
        writes.push({ path, body: request.postDataJSON() });
        if (path.endsWith("/grade")) {
          if (writes.length === 1) return route.fulfill({ status: 503, json: { detail: { code: "provider_upstream_unavailable" } } });
          completed = true;
          return route.fulfill({ json: { status: "started", job_id: "retry-job" } });
        }
        throw new Error(`Unexpected mutation ${path}`);
      }
      if (path.endsWith("/state")) return route.fulfill({ json: { ...task, status: completed ? "graded" : "error", grading_job_id: "failed-job", last_failed_job_id: "failed-job", error: completed ? null : "provider_rate_limited" } });
      if (path.endsWith("/result")) return route.fulfill({ json: { ...task, results: [], timestamp: 1 } });
      return route.fulfill({ json: [] });
    });
    await page.goto(`/frontier/live?taskId=${task.task_id}`);
    const alert = page.getByRole("alert"), retry = alert.getByRole("button", { name: zh ? "重试失败项" : "Retry failed items", exact: true });
    await expect(retry).toBeEnabled();
    await expect(alert.getByRole("button")).toHaveCount(1);
    await expect(alert.getByRole("link")).toHaveCount(0);
    await expect(page.getByRole("link", { name: /返回修改配置|Back to edit settings|查看进度与恢复|View progress and recovery|人工继续处理|Continue manually/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: zh ? "打开任务" : "Open task", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: zh ? "重新开始" : "Start over", exact: true })).toBeEnabled();
    await alert.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`demo-error-${locale}.png`) });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await retry.click();
    await expect(retry).toBeEnabled();
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}`));
    await retry.click();
    await expect(page.getByRole("heading", { name: zh ? "本次批改的具体情况" : "Detailed grading results", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: zh ? "学情分析" : "Learning analysis", exact: true }).click();
    await expect(page.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores", exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}`));
    expect(writes).toHaveLength(2);
    for (const write of writes) {
      expect(write.path).toBe(`/tasks/${task.task_id}/grade`);
      expect(write.body.retry_scope).toBe("failed_only");
    }
    expect(errors).toEqual([]);
  });

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
    await expect(page.getByRole("heading", { name: zh ? "本次批改的具体情况" : "Detailed grading results", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: zh ? "学情分析" : "Learning analysis", exact: true }).click();
    await expect(page.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores", exact: true })).toBeVisible();
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(() => page.getByRole("progressbar").evaluate(el => Math.abs(el.getBoundingClientRect().width - el.firstElementChild!.getBoundingClientRect().width))).toBeLessThan(1);
    await expect(page).toHaveURL(new RegExp(`/frontier/live\\?taskId=${task.task_id}`));
    expect(writes).toEqual([`/tasks/${task.task_id}/parse_submissions`, `/tasks/${task.task_id}/grade`]);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`demo-completed-${locale}.png`), fullPage: true });
  });
}
