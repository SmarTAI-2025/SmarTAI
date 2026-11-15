import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const width of [1440, 390]) {
  test(`${width}px: shared presets stay read-only, probes refresh the shared allowance, BYOK remains editable`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    const models = ["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"].map(model => ({
      provider_id: "openai:" + model, provider_type: "openai", model, display_name: model,
      enabled: true, is_shared: true, scope: "shared", editable: false, rpm: 0, max_concurrent: 1,
      verification_status: "platform_managed", image_capability_status: "unverified",
      image_checked_at: null as string | null, image_reason: null as string | null,
    }));
    const all = [...models, { ...models[0]!, provider_id: "pc-own", display_name: "My personal model",
      is_shared: false, scope: "owner", editable: true, is_default: true, verification_status: "unverified" }];
    let probes = 0, sol = 0;
    await page.setViewportSize({ width, height: 1000 });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_locale", "en-US");
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
    });
    await page.route(`${backend}/**`, async route => {
      const req = route.request(), path = decodeURIComponent(new URL(req.url()).pathname);
      const json = (value: unknown) => route.fulfill({ json: value });
      if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" } });
      if (path === "/auth/me") return json({ id: "shared-test", username: "Synthetic teacher", role: "teacher", is_active: true, auth_scope: "user" });
      if (path === "/auth/activity") return json({ token: "synthetic-teacher-token" });
      if (path === "/experts/available" || path === "/experts/stage-options") return json(all);
      if (path === "/experts/catalog") return json([]);
      if (path === "/experts/quota") return json({
        shared_pool_enabled: true, resets_at: "2026-11-17T00:00:00Z",
        shared: { requests: probes, request_limit: 100, estimated_input_tokens: probes * 1024, estimated_token_limit: 100000 },
        shared_sol: { requests: sol, request_limit: 50, estimated_input_tokens: sol * 1024, estimated_token_limit: 50000 },
        history: { requests: 0, request_limit: 10 },
      });
      if (/^\/experts\/openai:.*\/verify(-image)?$/.test(path)) {
        probes++;
        if (path.includes("gpt-6.1-sol")) sol++;
        const model = all.find(item => path.includes(item.provider_id))!;
        if (path.endsWith("verify-image")) {
          model.image_capability_status = "passed";
          model.image_checked_at = "2026-11-16T12:00:00Z";
          model.image_reason = "image_probe_answer_correct";
          return json(model);
        }
        return json({ status: "success", provider_id: model.provider_id, verification_status: "verified" });
      }
      if (path.includes("baidu")) return json({ credential_id: null, credentials_configured: false, verification_status: "not_configured" });
      if (path === "/tasks/") return json({});
      if (path === "/tags/") return json([]);
      throw new Error(`Unexpected request ${req.method()} ${path}`);
    });
    await page.goto("/settings/byok");
    await expect(page).toHaveURL(/\/settings\/byok/);
    await expect(page).toHaveTitle(/SmarTAI/i);
    await expect(page.getByRole("heading", { name: "Models & BYOK" })).toBeVisible();
    const quota = page.getByRole("region", { name: "Daily model allowance" });
    await expect(quota).toContainText("0 / 100");
    const row = (name: string) => width >= 640
      ? page.getByRole("row").filter({ has: page.getByText(name, { exact: true }) })
      : page.locator("article").filter({ has: page.getByText(name, { exact: true }) });
    for (const model of models) {
      const item = row(model.model);
      await expect(item.getByText("Platform managed, read-only")).toBeVisible();
      await expect(item.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
      await expect(item.getByRole("button", { name: "Delete", exact: true })).toHaveCount(0);
      await expect(item.getByRole("button", { name: "Disable", exact: true })).toHaveCount(0);
    }
    const target = row("gpt-6.1-sol");
    await target.getByRole("button", { name: "Verify (optional)" }).click();
    expect(probes).toBe(0);
    await page.getByRole("dialog").getByRole("button", { name: "Run connectivity check" }).click();
    await expect(quota).toContainText("1 / 100");
    await expect(quota).toContainText("1 / 50");
    await target.getByRole("button", { name: "Verify vision capability" }).click();
    expect(probes).toBe(1);
    await page.getByRole("dialog").getByRole("button", { name: "Run image test" }).click();
    await expect(target).toContainText("Passed");
    await expect(quota).toContainText("2 / 100");
    await expect(quota).toContainText("2 / 50");
    await expect(row("gpt-6.1-terra")).toContainText("Unverified");
    await row("My personal model").getByRole("button", { name: "Edit", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: "Add configuration", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
    await target.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("shared-models.png") });
    await quota.screenshot({ path: testInfo.outputPath("shared-allowance.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
