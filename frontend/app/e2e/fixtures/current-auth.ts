import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, type Page } from "@playwright/test";

export async function currentLogin(page: Page, role: "teacher" | "student" | "admin", label: string): Promise<string> {
  const credentials = JSON.parse(execFileSync(process.env.SMARTAI_E2E_PYTHON ?? "python",
    ["-m", "backend.tests.browser_auth_fixture", role, label],
    { cwd: path.resolve(process.cwd(), "../.."), encoding: "utf8" }));
  const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
  const response = await page.request.post(`${backend}/auth/login`, { data: credentials });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).token;
}
