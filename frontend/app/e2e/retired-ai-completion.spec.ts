import { test, expect } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`retired AI completion URLs show not found at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 950 });
    const errors: string[] = [];
    const requests: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-retired-completion");
      localStorage.setItem("smartai_locale", "zh-CN");
    });
    const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
    await page.route(`${backend}/**`, async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const headers = {
        "Access-Control-Allow-Origin": new URL(testInfo.project.use.baseURL!).origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "*",
      };
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers });
      requests.push(path);
      const json = (value: unknown) => route.fulfill({ json: value, headers });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-retired-completion" });
      return json([]);
    });
    for (const suffix of ["ai-complete", "ai-complete/", "ai-complete/progress/job", "ai-complete/content", "ai-complete/progress", "AI-COMPLETE", "%61i-complete"]) {
      const path = `/tasks/retired-completion/questions/${suffix}`;
      await page.goto(path);
      await expect(page.getByText("页面不存在", { exact: true })).toBeVisible();
      expect(new URL(page.url()).pathname).toBe(path);
      await expect(page.locator('a[href*="/ai-complete"]')).toHaveCount(0);
      await expect(page.getByRole("combobox")).toHaveCount(0);
      await expect(page.getByRole("button", { name: /补全|重新选择/ })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.reload();
    await expect(page.getByText("页面不存在", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("retired-ai-completion.png"), fullPage: true });
    expect(requests.some(path => path.startsWith("/tasks/retired-completion"))).toBe(false);
    expect(errors).toEqual([]);
  });
}
