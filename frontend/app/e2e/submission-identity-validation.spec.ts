import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { expect, test as base, type Page } from "@playwright/test";

type UploadReceiver = { url: string; requests: string[] };
const test = base.extend<{ uploadReceiver: UploadReceiver }>({
  uploadReceiver: async ({}, use) => {
    const requests: string[] = [];
    // WebKit's intercepted postData omits file bytes. Read the actual multipart
    // upload locally so every browser verifies file preservation after restore.
    const server = createServer(async (request, response) => {
      response.setHeader("Access-Control-Allow-Origin", request.headers.origin ?? "*");
      response.setHeader("Access-Control-Allow-Credentials", "true");
      response.setHeader("Access-Control-Allow-Headers", request.headers["access-control-request-headers"] ?? "Authorization, Content-Type");
      response.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
      if (request.method === "OPTIONS") { response.writeHead(204).end(); return; }
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      requests.push(Buffer.concat(chunks).toString());
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ status: "started", task_id: "roster-audit", job_id: "local-mock-job", workflow_revision: 2 }));
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Upload receiver did not bind");
    try { await use({ url: `http://127.0.0.1:${address.port}`, requests }); }
    finally { await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())); }
  },
});
test.use({ reducedMotion: "reduce" });

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const uploadPath = "/tasks/roster-audit/submissions/upload";
// Real file containers, used only against mocked APIs; these tests do not run OCR.
const pdf = { name: "S1_Alex.pdf", mimeType: "application/pdf", buffer: readFileSync("e2e/fixtures/single.pdf") };
const zip = { name: "answers.zip", mimeType: "application/zip", buffer: readFileSync("../../SmarTAI_test_case/SmarTAI_test_stu_hw1.zip") };
const roster = { name: "roster.csv", mimeType: "text/csv", buffer: Buffer.from("student_id,student_name\nS1,Alex Chen\n") };
const copy = {
  "en-US": {
    title: "Upload Student Submissions", roster: "Import Roster", manual_review: "Manual Review", filename: "Match IDs from filenames",
    start: "Start Submission Recognition", required: "Upload a roster when “Import Roster” is selected.",
    save: "Save draft", saved: /Draft saved ·/, model: "Submission recognition model", deleteDraft: "Delete page draft",
  },
  "zh-CN": {
    title: "上传学生作答", roster: "导入名单", manual_review: "手动校正", filename: "按文件名识别学号",
    start: "开始识别作答", required: "选择“导入名单”时，请同时上传名单文件。",
    save: "暂存", saved: /已暂存 ·/, model: "作答识别模型", deleteDraft: "删除本页草稿",
  },
} as const;

async function setup(page: Page, locale: keyof typeof copy, uploadReceiver: UploadReceiver) {
  const { requests } = uploadReceiver;
  const errors: string[] = [];
  const task = {
    task_id: "roster-audit", owner_id: "teacher", name: "Roster validation", status: "problems_ready",
    workflow_revision: 1, problem_count: 1, student_count: 0, created_at: 1, updated_at: 1, kb_docs: {}, student_data: {},
    problem_data: { q1: { q_id: "q1", number: "1", stem: "What is 4 + 4?", max_score: 10, criterion: "Eight", reference_answer: "8", review_status: "confirmed" } },
  };
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.addInitScript(value => {
    localStorage.setItem("smartai_token", "synthetic-teacher-token");
    localStorage.setItem("smartai_locale", value);
  }, locale);
  await page.route(`${backend}/**`, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path === "/auth/me") return json({ id: "teacher", username: "Local teacher", role: "teacher", is_active: true, auth_scope: "user" });
    if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
    if (path.startsWith("/experts")) return json([{ provider_id: "test-provider", provider_type: "gemini", model: "synthetic", enabled: true, is_default: true, image_capability_status: "verified" }]);
    if (request.method() === "POST" && path === "/tasks/roster-audit/parse_submissions") {
      task.status = "parsing_submissions";
      return route.continue({ url: `${uploadReceiver.url}/parse_submissions` });
    }
    if (path === "/tasks/roster-audit/state") return json({ ...task, progress: { phase: "parsing", job_id: "local-mock-job", current_step: "recognition", total_steps: 2, completed_steps: 0, active: [], messages: [] } });
    if (path.endsWith("/input")) return json({ task_id: task.task_id, workflow_revision: 1, input: null });
    if (path.endsWith("/source-files")) return json({ task_id: task.task_id, workflow_revision: 1, problem_source: null, submission_sources: {} });
    if (path === "/tasks/roster-audit") return json(task);
    throw new Error(`Unexpected request ${request.method()} ${path}`);
  });
  await page.goto(uploadPath);
  await expect(page).toHaveTitle("SmarTAI");
  await expect(page.getByRole("heading", { name: copy[locale].title, exact: true })).toBeVisible();
  await expect(page.getByLabel(copy[locale].model)).toHaveValue("test-provider");
  return { requests, errors };
}

for (const locale of ["en-US", "zh-CN"] as const) {
  test(`${locale}: resetting the draft clears roster validation without bypassing the next submission`, async ({ page, uploadReceiver }, info) => {
    const text = copy[locale];
    const { requests, errors } = await setup(page, locale, uploadReceiver);
    await page.locator('input[type="file"]').first().setInputFiles(pdf);
    await page.getByRole("radio", { name: text.roster, exact: true }).click();
    const start = page.getByRole("button", { name: text.start, exact: true });
    await start.click();
    await expect(page.locator("#submission-roster-required")).toHaveText(text.required);
    page.once("dialog", dialog => dialog.dismiss());
    await page.getByRole("button", { name: text.deleteDraft, exact: true }).click();
    await expect(page.locator("#submission-roster-required")).toBeVisible();
    await expect(page.getByText(pdf.name, { exact: true })).toBeVisible();
    page.once("dialog", dialog => dialog.accept());
    await page.getByRole("button", { name: text.deleteDraft, exact: true }).click();
    await expect(page.getByRole("radio", { name: text.filename, exact: true })).toHaveAttribute("aria-checked", "true");
    await expect(page.locator("#submission-roster-required")).toHaveCount(0);
    await expect(page.locator('[role="alert"]')).toHaveCount(0);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.screenshot({ path: info.outputPath("draft-reset.png"), fullPage: true, animations: "disabled" });
    await expect(page.getByText(pdf.name, { exact: true })).toHaveCount(0);
    expect(requests).toHaveLength(0);
    await start.click();
    await expect(page.locator("#submission-file-required")).toBeVisible();
    await page.locator('input[type="file"]').first().setInputFiles(pdf);
    await page.getByRole("radio", { name: text.roster, exact: true }).click();
    await start.click();
    await expect(page.locator("#submission-roster-required")).toHaveText(text.required);
    expect(requests).toHaveLength(0);
    await page.locator('input[type="file"]').nth(1).setInputFiles(roster);
    await expect(page.locator("#submission-roster-required")).toHaveCount(0);
    await start.click();
    await expect(page).toHaveURL(/\/tasks\/roster-audit\/submissions\/progress$/);
    expect(requests).toHaveLength(1);
    expect(requests[0]).toContain('name="roster_file"; filename="roster.csv"');
    expect(errors).toEqual([]);
  });

  for (const mode of ["manual_review", "filename"] as const) {
    test(`${locale}: roster error clears on ${mode} and is revalidated after switching back`, async ({ page, uploadReceiver }, info) => {
      const text = copy[locale];
      const { requests, errors } = await setup(page, locale, uploadReceiver);
      const file = mode === "manual_review" ? pdf : zip;
      await page.locator('input[type="file"]').first().setInputFiles(file);
      const chooseMode = (value: "roster" | typeof mode) => page.getByRole("radio", { name: text[value], exact: true }).click();
      const start = page.getByRole("button", { name: text.start, exact: true });
      const error = page.locator("#submission-roster-required");
      await chooseMode("roster");
      await start.click();
      await expect(error).toHaveText(text.required);
      await expect(page.locator('[aria-describedby="submission-roster-required"]')).toBeFocused();
      expect(requests).toHaveLength(0);
      await chooseMode(mode);
      await expect(page.getByRole("radio", { name: text[mode], exact: true })).toHaveAttribute("aria-checked", "true");
      await expect(error).toHaveCount(0);
      await expect(page.locator('[aria-describedby="submission-roster-required"]')).toHaveCount(0);
      await expect(page.getByText(file.name, { exact: true })).toBeVisible();
      await expect(page.getByLabel(text.model)).toHaveValue("test-provider");
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
      await page.screenshot({ path: info.outputPath("roster-error-cleared.png"), fullPage: true, animations: "disabled" });
      await chooseMode("roster");
      await expect(error).toHaveCount(0);
      await start.click();
      await expect(error).toHaveText(text.required);
      expect(requests).toHaveLength(0);

      // Persist the selected file/mode, but not transient validation errors.
      await chooseMode(mode);
      await page.getByRole("button", { name: text.save, exact: true }).click();
      await expect(page.getByText(text.saved)).toBeVisible();
      await page.reload();
      await expect(page.getByText(file.name, { exact: true })).toBeVisible();
      await expect(page.getByRole("radio", { name: text[mode], exact: true })).toHaveAttribute("aria-checked", "true");
      await expect(error).toHaveCount(0);
      await chooseMode("roster");
      await start.click();
      await expect(error).toHaveText(text.required);
      expect(requests).toHaveLength(0);
      await chooseMode(mode);
      await start.click();
      await expect(page).toHaveURL(/\/tasks\/roster-audit\/submissions\/progress$/);
      expect(requests).toHaveLength(1);
      expect(requests[0]).toContain(`name="identity_mode"\r\n\r\n${mode}\r\n`);
      expect(requests[0]).toContain(`filename="${file.name}"`);
      expect(requests[0]).not.toContain('name="roster_file"');
      await expect(page.locator("vite-error-overlay")).toHaveCount(0);
      expect(errors).toEqual([]);
    });
  }
}

for (const mode of ["roster", "filename", "manual_review"] as const) {
  test(`saved roster is retained across mode switches and sent only for ${mode}`, async ({ page, uploadReceiver }) => {
    const text = copy["en-US"];
    const { requests, errors } = await setup(page, "en-US", uploadReceiver);
    await page.locator('input[type="file"]').first().setInputFiles(pdf);
    await page.getByRole("radio", { name: text.roster, exact: true }).click();
    await page.locator('input[type="file"]').nth(1).setInputFiles(roster);
    await page.getByRole("radio", { name: text.manual_review, exact: true }).click();
    await page.getByRole("button", { name: text.save, exact: true }).click();
    await expect(page.getByText(text.saved)).toBeVisible();
    await page.reload();
    await expect(page.getByRole("radio", { name: text.manual_review, exact: true })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("radio", { name: text.roster, exact: true }).click();
    await expect(page.getByText(roster.name, { exact: true })).toBeVisible();
    await expect(page.getByText(pdf.name, { exact: true })).toBeVisible();
    await page.getByRole("radio", { name: text[mode], exact: true }).click();
    await page.getByRole("button", { name: text.start, exact: true }).click();
    await expect(page).toHaveURL(/\/tasks\/roster-audit\/submissions\/progress$/);
    expect(requests).toHaveLength(1);
    expect(requests[0]).toContain(`name="identity_mode"\r\n\r\n${mode}\r\n`);
    expect(requests[0]).toContain(`filename="${pdf.name}"`);
    if (mode === "roster") {
      expect(requests[0]).toContain('name="roster_file"; filename="roster.csv"');
      expect(requests[0]).toContain(roster.buffer.toString());
    } else {
      expect(requests[0]).not.toContain('name="roster_file"');
      expect(requests[0]).not.toContain(roster.buffer.toString());
      expect(requests[0]).not.toContain('name="reuse_roster_from_job_id"');
    }
    expect(errors).toEqual([]);
  });
}
