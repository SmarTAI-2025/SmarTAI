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

test("production router keeps the Figma teacher surface and normalized workspaces dormant", async ({ page }) => {
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
