import { expect, test } from "@playwright/test";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

for (const locale of ["zh-CN", "en-US"]) for (const width of [1440, 390]) {
  test(`${locale}, ${width}px: shared presets stay read-only, probes refresh the shared allowance, BYOK remains editable`, async ({ page }, testInfo) => {
    const zh = locale === "zh-CN";
    const errors: string[] = [];
    const models = ["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"].map(model => ({
      provider_id: "openai:" + model, provider_type: "openai", model, display_name: `OpenAI · ${model}`,
      enabled: true, is_shared: true, scope: "shared", editable: false, rpm: 0, max_concurrent: 1,
      base_url: "https://api.openai.com/v1", endpoint_descriptor: "api.openai.com/v1 · OpenAI Chat Completions",
      configured_display_name: null,
      verification_status: "platform_managed", image_capability_status: "unverified",
      last_checked_at: null as string | null,
      image_checked_at: null as string | null, image_reason: null as string | null,
    }));
    // Exercise the older Luna label too, as cached API data may still contain it.
    models[0]!.display_name += " · api.openai.com/v1 · OpenAI Chat Completions · t-6-luna";
    const all = [...models, { ...models[0]!, provider_id: "pc-own", display_name: "My personal model",
      configured_display_name: "My personal model",
      is_shared: false, scope: "owner", editable: true, is_default: true, verification_status: "unverified" }];
    let probes = 0;
    await page.setViewportSize({ width, height: 1000 });
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.addInitScript((locale) => {
      localStorage.setItem("smartai_locale", locale);
      localStorage.setItem("smartai_token", "synthetic-teacher-token");
    }, locale);
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
        shared_quota_mode: "weekly_usd",
        shared_weekly: {limit_usd: 5, spent_usd: probes * 0.01, reserved_usd: 0, remaining_usd: 5 - probes * 0.01, unknown_requests: 0, resets_at: "2026-11-23T00:00:00+08:00"},
        shared_pool_enabled: true, resets_at: "2026-11-17T00:00:00Z",
        shared: { requests: probes, request_limit: 100, estimated_input_tokens: probes * 1024, estimated_token_limit: 100000 },
        shared_sol: { requests: 50, request_limit: 50, estimated_input_tokens: 50000, estimated_token_limit: 50000 },
        history: { requests: 0, request_limit: 10 },
      });
      if (/^\/experts\/openai:.*\/verify(-image)?$/.test(path)) {
        probes++;

        const model = all.find(item => path.includes(item.provider_id))!;
        if (path.endsWith("verify-image")) {
          model.image_capability_status = "passed";
          model.image_checked_at = "2026-11-16T12:00:00Z";
          model.image_reason = "image_probe_answer_correct";
          return json(model);
        }
        model.verification_status = "verified";
        model.last_checked_at = "2026-11-16T12:00:00Z";
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
    await expect(page.getByRole("heading", { name: zh ? "模型与 BYOK" : "Models & BYOK" })).toBeVisible();
    const quota = page.getByRole("region", { name: zh ? "共享模型周额度" : "Weekly shared allowance" });
    await expect(quota).toContainText("$0.00 / $5.00");
    const row = (name: string) => width >= 640
      ? page.getByRole("row").filter({ has: page.getByText(name, { exact: true }) })
      : page.locator("article").filter({ has: page.getByText(name, { exact: true }) });
    for (const model of models) {
      const item = row(`OpenAI · ${model.model}`);
      await expect(item.getByText(zh ? "平台托管" : "Platform managed", { exact: true })).toBeVisible();
      if (width >= 640) {
        const center = await item.evaluate(row => {
          const cell = row.querySelector("td:last-child")!.getBoundingClientRect();
          const badge = row.querySelector("td:last-child span")!.getBoundingClientRect();
          return Math.abs((cell.left + cell.right - badge.left - badge.right) / 2);
        });
        expect(center).toBeLessThan(1);
      }
      await expect(item.getByText(zh ? "不可编辑修改" : "Cannot be edited", { exact: true })).toBeVisible();
      const textState = item.getByRole("button", { name: zh ? "验证（可选）" : "Verify (optional)" }).locator("..");
      await expect(textState).toContainText(zh ? "未验证" : "Unchecked");
      await expect(item.getByRole("button", { name: zh ? "编辑" : "Edit", exact: true })).toHaveCount(0);
      await expect(item.getByRole("button", { name: zh ? "删除" : "Delete", exact: true })).toHaveCount(0);
      await expect(item.getByRole("button", { name: zh ? "停用" : "Disable", exact: true })).toHaveCount(0);
    }
    const target = row("OpenAI · gpt-6.1-sol");
    await target.getByRole("button", { name: zh ? "验证（可选）" : "Verify (optional)" }).click();
    expect(probes).toBe(0);
    await page.getByRole("dialog").getByRole("button", { name: zh ? "发起连通性验证" : "Run connectivity check" }).click();
    await expect(quota).toContainText("$0.01 / $5.00");
    await expect(target.getByText(zh ? "验证通过" : "Verified", { exact: true })).toBeVisible();
    await page.reload();
    await expect(target.getByText(zh ? "验证通过" : "Verified", { exact: true })).toBeVisible();
    await expect(quota).not.toContainText("half");
    await target.getByRole("button", { name: zh ? "验证视觉能力" : "Verify vision capability" }).click();
    expect(probes).toBe(1);
    await page.getByRole("dialog").getByRole("button", { name: zh ? "发起图像测试" : "Run image test" }).click();
    await expect(target).toContainText(zh ? "已通过" : "Passed");
    await expect(quota).toContainText("$0.02 / $5.00");
    await expect(quota).toContainText(zh ? "每周一新加坡时间 00:00" : "Monday at 00:00 Singapore time");
    await expect(row("OpenAI · gpt-6.1-terra")).toContainText(zh ? "未验证" : "Unverified");
    await row("My personal model").getByRole("button", { name: zh ? "编辑" : "Edit", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: zh ? "取消" : "Cancel", exact: true }).click();
    await page.getByRole("button", { name: zh ? "添加模型配置" : "Add configuration", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: zh ? "取消" : "Cancel", exact: true }).click();
    await target.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("shared-models.png") });
    await quota.screenshot({ path: testInfo.outputPath("shared-allowance.png") });
    await page.goto("/settings/byok?imageRecovery=1&returnTo=%2Ftasks%2Fsynthetic%2Fupload%2Fproblems&providerId=openai%3Agpt-6-luna");
    const stageSelect = page.getByRole("combobox", { name: zh ? "继续识别所用模型" : "Model for continuing" });
    await expect(stageSelect).toHaveValue("openai:gpt-6-luna");
    for (const model of models) {
      await expect(stageSelect.locator(`option[value="${model.provider_id}"]`)).toHaveText(`OpenAI · ${model.model}`);
    }
    await expect(stageSelect.locator('option[value="pc-own"]')).toContainText("api.openai.com/v1");
    await stageSelect.selectOption("openai:gpt-6.1-terra");
    await expect(stageSelect).toHaveValue("openai:gpt-6.1-terra");
    await stageSelect.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("short-stage-options.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
