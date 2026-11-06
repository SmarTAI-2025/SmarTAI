import { test, expect, type Page } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

async function loginAs(
  page: Page,
  role: "teacher" | "student" | "admin",
  name: string,
  target = "/dashboard",
) {
  const token = `demo-${role}-${name}`;
  await page.goto("/login");
  await page.evaluate((t) => {
    window.localStorage.setItem("smartai_token", t);
    window.localStorage.setItem("smartai_locale", "en-US");
  }, token);
  await page.goto(target);
}

test("production router keeps the current teacher surface and rejects retired workspaces", async ({ page }) => {
  await loginAs(page, "teacher", "e2e-teacher");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Workspace" })).toBeVisible({ timeout: 15000 });

  await loginAs(page, "student", "e2e-student", "/student");
  await expect(page).toHaveURL(/\/student/);
  await expect(
    page.getByRole("heading", { name: "Student workspace is not available yet" }),
  ).toBeVisible({ timeout: 15000 });

  await loginAs(page, "admin", "e2e-admin", "/admin");
  await expect(page).toHaveURL(/\/admin/);
  await expect(page.getByText("Page not found")).toBeVisible({ timeout: 15000 });

  await loginAs(page, "teacher", "e2e-teacher", "/teacher");
  await expect(page.getByText("Page not found")).toBeVisible({ timeout: 15000 });
  await page.goto("/teacher/courses");
  await expect(page.getByText("Page not found")).toBeVisible({ timeout: 15000 });
});

for (const width of [1280, 390]) {
  test(`retired task bookmarks stay rejected and current task entry works at ${width}px`, async ({ page, request }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    const retiredRequests: string[] = [];
    page.on("request", req => {
      if (/\/material-imports(?:\/|$)|\/upload_(?:reference|test_cases)(?:\?|$)/.test(new URL(req.url()).pathname)) retiredRequests.push(req.url());
    });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    const name = `cleanup-routes-${width}`;
    await loginAs(page, "teacher", name);
    const created = await request.post(`${backend}/tasks/`, {
      headers: { Authorization: `Bearer demo-teacher-${name}` },
      data: { name: `Current route verification ${width}` },
    });
    expect(created.ok()).toBeTruthy();
    const { task_id: taskId } = await created.json();
    expect(taskId).toBeTruthy();
    const me = await request.get(`${backend}/auth/me`, {
      headers: { Authorization: `Bearer demo-teacher-${name}` },
    });
    expect(me.ok()).toBeTruthy();
    const { id: owner } = await me.json();
    // Reproduce an existing device with drafts from the removed feature.
    // A current metadata draft proves the list did not simply stop loading.
    await page.evaluate(async ({ owner, taskId }) => {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const open = indexedDB.open("smartai-explicit-drafts", 1);
        open.onupgradeneeded = () => {
          open.result.createObjectStore("drafts", { keyPath: "key" });
          open.result.createObjectStore("meta");
        };
        open.onsuccess = () => resolve(open.result);
        open.onerror = () => reject(open.error);
      });
      const tx = db.transaction(["drafts", "meta"], "readwrite");
      const done = new Promise<void>((resolve, reject) => {
        tx.oncomplete = () => resolve();
        tx.onabort = tx.onerror = () => reject(tx.error);
      });
      const epoch = await new Promise<string>((resolve, reject) => {
        const read = tx.objectStore("meta").get("epoch");
        read.onsuccess = () => resolve(read.result ?? "initial");
        read.onerror = () => reject(read.error);
      });
      const savedAt = Date.now();
      for (const scope of [`material-import:${taskId}`, `material-review:${taskId}:old`, `metadata:${taskId}`]) {
        tx.objectStore("drafts").put({
          key: JSON.stringify([owner, scope]), version: 2, owner, scope,
          businessVersion: "", revision: crypto.randomUUID(), epoch, savedAt,
          expires: savedAt + 86_400_000, bytes: 2, data: { name: "Current task draft" }, files: [],
        });
      }
      await done;
      db.close();
    }, { owner, taskId });
    await page.goto("/history?view=drafts");
    const drafts = page.getByRole("region", { name: "Saved drafts" });
    await expect(drafts.locator(`a[href="/tasks/${taskId}/edit"]`)).toBeVisible();
    await expect(drafts.getByRole("listitem")).toHaveCount(1);
    await expect(drafts.locator('a[href*="/questions/import"]')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("current-draft-list.png"), fullPage: true });
    const retired = [
      `/tasks/${taskId}/setup`, `/tasks/${taskId}/upload/submissions`,
      `/tasks/${taskId}/upload/reference`, `/tasks/${taskId}/upload/test-cases`,
      `/tasks/${taskId}/questions/import`, `/tasks/${taskId}/questions/import/progress/old`,
      `/tasks/${taskId}/questions/import/review/old`,
      "/teacher", "/teacher/courses", "/teacher/courses/old",
      "/teacher/assignments/old", "/teacher/assignments/old/edit",
      "/teacher/grading", "/teacher/assignments/old/submissions", "/teacher/assignments/old/results",
      "/student/courses", "/student/courses/old", "/student/assignments/old", "/student/results/old",
      "/upload/problems", "/upload/homework", "/problems", "/students", "/grading/old", "/results/old",
    ];
    for (const path of retired) {
      await page.goto(path);
      await expect(page.getByText("Page not found", { exact: true })).toBeVisible();
      expect(new URL(page.url()).pathname).toBe(path);
      expect(await page.locator("vite-error-overlay").count()).toBe(0);
    }
    await page.screenshot({ path: testInfo.outputPath("retired-route.png"), fullPage: true });
    await page.getByRole("link", { name: "Back to workspace", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await page.goto(`/tasks/${taskId}`);
    await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}/upload/problems$`));
    await expect(page.locator("main")).not.toBeEmpty();
    for (const title of ["1. Questions", "2. Reference Answers", "3. Rubrics", "4. Test Cases"]) {
      await expect(page.getByText(title, { exact: true })).toBeVisible();
    }
    const links = await page.locator("a[href]").evaluateAll(nodes => nodes.map(node => node.getAttribute("href")));
    expect(links.filter(href => retired.includes(href ?? ""))).toEqual([]);
    expect(errors).toEqual([]);
    expect(retiredRequests).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("current-task-entry.png"), fullPage: true });
  });
}

test("retired HTTP workflows cannot be reached through direct URLs", async ({ request }) => {
  const paths = [
    ["GET", "/courses/old/students"], ["POST", "/courses/old/enroll"],
    ["POST", "/assignments/old/publish"], ["POST", "/submissions/submit"],
    ["POST", "/grading-runs"], ["GET", "/results/assignment/old/me"],
    ["POST", "/prob_preview/"], ["POST", "/hw_preview/"], ["POST", "/ai_grading/"],
  ];
  for (const [method, path] of paths) {
    const response = await request.fetch(`${backend}${path}`, {
      method, headers: { Authorization: "Bearer demo-teacher-e2e-retired" }, data: {},
    });
    expect(response.status(), `${method} ${path}`).toBe(404);
  }
});

test("retired walkthrough files are absent from the served production bundle", async ({ page, request }) => {
  await loginAs(page, "teacher", "e2e-retired-assets");
  for (const name of ["DEMO-001_typeset.pdf", "DEMO-002_handwritten.pdf", "DEMO-002_handwritten.png", "DEMO-003_mixed.pdf", "assignment_mixed_stem.pdf", "scan_004.pdf", "scan_004.png"]) {
    const path = `/frontier-demo/${name}`;
    const response = await request.get(path);
    expect(response.headers()["content-type"] ?? "").not.toMatch(/application\/pdf|image\//);
    // A static SPA host can return the shell for a missing asset; the router
    // must still reject the path instead of rendering a retired walkthrough.
    if (response.ok()) {
      await page.goto(path);
      await expect(page.getByText("Page not found")).toBeVisible();
    } else {
      expect([404, 410]).toContain(response.status());
    }
  }
  const active = await request.get("/frontier-demo/live/question_source.pdf");
  expect(active.ok()).toBeTruthy();
  expect(active.headers()["content-type"]).toContain("application/pdf");
});
