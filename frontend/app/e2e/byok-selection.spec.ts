import { expect, test } from "@playwright/test";
import type { ByokConnection } from "../src/types";

const backend = process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000";
const longModel = "openai/reasoning-model-with-an-intentionally-long-provider-identifier";
const relay: ByokConnection = {
  id: "relay", provider_type: "openai", display_name: "School relay",
  base_url: "https://relay.example.com/v1", endpoint_identity: "https://relay.example.com/v1",
  wire_protocol: "openai_chat_completions", rpm: 12, catalog_status: "ready",
  models: [{ id: longModel, reasoning_efforts: ["low", "high"] }, { id: "anthropic/relay-unknown", reasoning_efforts: [] }],
};
const manual: ByokConnection = { ...relay, id: "manual", display_name: "Manual relay", catalog_status: "unavailable", models: [] };

for (const width of [1280, 390]) {
  test(`BYOK saves the connection, selects model and effort, and supports manual fallback at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem("smartai_token", "synthetic-byok-token");
      localStorage.setItem("smartai_locale", "en-US");
    });

    const pageErrors: string[] = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    const official: ByokConnection = {
      ...manual, id: "official", provider_type: "minimax", display_name: "MiniMax",
      base_url: null, endpoint_identity: "https://api.minimax.cn/v1", catalog_status: "partial",
      models: [{ id: "MiniMax-M3", reasoning_efforts: [] }],
      manual_model_efforts: { "MiniMax-M3.1-Flash": ["low", "medium", "high", "xhigh", "max"] },
    };
    const connections: ByokConnection[] = [manual, official];
    const choices: unknown[] = [];
    const saved: unknown[] = [];
    let defaultModel = "";
    const experts: Record<string, unknown>[] = [];
    let gradingSelection: unknown;
    const suggestedGrading = () => ({
      schema_version: 1, selected_provider_ids: [defaultModel], primary_provider_id: defaultModel,
      aggregation_method: "single", multi_sample_n: 1, knowledge_scope: "none",
      feedback_tone: "neutral", feedback_length: "medium", feedback_language: "en",
      suggest_corrections: true, low_confidence_threshold: .6,
    });
    await page.route(`${backend}/**`, async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*",
      } });
      const json = (value: unknown) => route.fulfill({ json: value });
      if (path === "/auth/me") return json({ id: "teacher", username: "Teacher", role: "teacher", is_active: true });
      if (path === "/auth/activity") return json({ token: "synthetic-byok-token" });
      if (path === "/experts/quota") return json({
        shared_pool_enabled: false, resets_at: "2026-12-01T00:00:00Z",
        shared: { requests: 0, request_limit: 0, estimated_input_tokens: 0, estimated_token_limit: 0 },
        shared_sol: { requests: 0, request_limit: 0, estimated_input_tokens: 0, estimated_token_limit: 0 },
        history: { requests: 0, request_limit: 0 },
      });
      if (path === "/experts/catalog") return json([{
        provider_type: "openai", display_name: "OpenAI", default_base_url: "https://api.openai.com/v1",
        wire_protocol: "openai_chat_completions", custom_base_url_supported: true, custom_base_url_enabled: true,
      }, {
        provider_type: "deepseek", display_name: "DeepSeek", default_base_url: "https://api.deepseek.com/v1",
        wire_protocol: "openai_chat_completions", custom_base_url_supported: true, custom_base_url_enabled: true,
      }]);
      if (path === "/experts/connections") {
        if (request.method() === "POST") {
          saved.push(request.postDataJSON());
          connections.push(relay);
          return json(relay);
        }
        return json(connections);
      }
      if (path.endsWith("/model")) {
        const choice = request.postDataJSON();
        const id = `bound-${choices.length}`;
        choices.push(choice);
        const connection = connections.find(row => path.includes(`/${row.id}/`))!;
        experts.push({
          provider_id: id, provider_type: connection.provider_type, model: choice.model, byok_connection_id: connection.id,
          reasoning_effort: choice.reasoning_effort, max_concurrent: 1, rpm: 12, enabled: true, scope: "owner",
        });
        return json({ provider_id: id });
      }
      if (path === "/experts/default") {
        defaultModel = request.postDataJSON().provider_id;
        return json({ provider_id: defaultModel });
      }
      if (path === "/tasks/byok-fixture/grading-setup") {
        if (request.method() === "PUT") gradingSelection = request.postDataJSON().grading_setup;
        return json({
          task_id: "byok-fixture", task_status: "submissions_ready", workflow_revision: gradingSelection ? 2 : 1,
          configured: Boolean(gradingSelection), grading_setup: gradingSelection ?? null, suggested_setup: suggestedGrading(),
          available_experts: experts,
          knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: 0, task_docs: [] },
          readiness: { ready: true, blocking_issues: [], warnings: [] },
        });
      }
      if (path === "/tasks/byok-fixture") return json({
        task_id: "byok-fixture", name: "Synthetic grading task", status: "submissions_ready",
        workflow_revision: 1, problem_data: {}, student_data: {}, kb_docs: {},
      });
      if (path.endsWith("/kb")) return json({ docs: [] });
      if (path === "/course-materials/") return json({ items: [] });
      if (path === "/experts/available" || path === "/experts/stage-options") {
        return json(experts.map(expert => ({ ...expert, is_default: expert.provider_id === defaultModel })));
      }
      return json([]);
    });

    await page.goto("/settings/byok");
    await expect(page.getByRole("heading", { name: "BYOK connections" })).toBeVisible();
    await page.getByRole("button", { name: "Add configuration" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("combobox", { name: "Provider", exact: true }).selectOption("openai");
    await dialog.getByLabel(/API key/).fill("synthetic-key");
    await dialog.getByLabel(/API Base URL/).fill(relay.base_url!);
    await expect(dialog.getByLabel("Model name")).toHaveCount(0);
    await dialog.getByRole("button", { name: "Save configuration" }).click();
    await expect(page.getByRole("combobox", { name: "BYOK connection" })).toHaveValue("connection:relay");
    expect(saved).toEqual([expect.objectContaining({ api_key: "synthetic-key", base_url: relay.base_url })]);
    expect(saved[0]).not.toHaveProperty("model");

    const model = page.getByRole("combobox", { name: "Model ID" });
    const effort = page.getByRole("combobox", { name: "Reasoning effort" });
    await expect(effort).toBeDisabled();
    await model.selectOption(longModel);
    await expect(effort.locator("option")).toHaveText(["Provider default", "Low", "High"]);
    await effort.selectOption("high");
    await page.getByRole("button", { name: "Set as default model" }).click();
    await expect.poll(() => defaultModel).toBe("bound-0");
    expect(choices[0]).toEqual({ model: longModel, reasoning_effort: "high" });

    await model.selectOption("anthropic/relay-unknown");
    await expect(effort).toBeDisabled();
    await expect(effort).toHaveValue("");
    await page.getByRole("button", { name: "Set as default model" }).click();
    await expect.poll(() => defaultModel).toBe("bound-1");
    expect(choices[1]).toEqual({ model: "anthropic/relay-unknown", reasoning_effort: null });

    await page.getByRole("combobox", { name: "BYOK connection" }).selectOption("connection:manual");
    await page.getByRole("textbox", { name: "Model ID" }).fill("organization/manual-model");
    await expect(effort).toBeDisabled();
    await page.getByRole("button", { name: "Set as default model" }).click();
    await expect.poll(() => defaultModel).toBe("bound-2");
    expect(choices[2]).toEqual({ model: "organization/manual-model", reasoning_effort: null });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(pageErrors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("byok-selection.png") });

    await page.goto("/tasks/byok-fixture/grading-setup");
    await page.getByRole("combobox", { name: "BYOK connection" }).selectOption("connection:relay");
    await page.getByRole("combobox", { name: "Model ID" }).selectOption(longModel);
    await page.getByRole("combobox", { name: "Reasoning effort" }).selectOption("low");
    await page.getByRole("button", { name: "Add or update expert" }).click();
    await expect.poll(() => choices[3]).toEqual({ model: longModel, reasoning_effort: "low" });
    await expect(page.getByRole("checkbox", { name: new RegExp(longModel) })).toBeChecked();
    await page.getByRole("button", { name: "Save & Review Grading" }).click();
    await expect.poll(() => gradingSelection).toMatchObject({
      selected_provider_ids: ["bound-2", "bound-3"], primary_provider_id: "bound-2",
    });
    expect(pageErrors).toEqual([]);

    await page.goto("/settings/byok");
    await page.getByRole("combobox", { name: "BYOK connection" }).selectOption("connection:official");
    await model.selectOption("MiniMax-M3");
    await expect(effort).toBeDisabled();
    await page.getByRole("button", { name: "Enter ID" }).click();
    const input = page.getByRole("textbox", { name: "Model ID" });
    await input.fill("MiniMax-M3.1-Flash");
    await expect(effort.locator("option")).toHaveText(["Provider default", "Low", "Medium", "High", "Extra high", "Maximum"]);
    await effort.selectOption("max");
    await page.getByRole("button", { name: "Set as default model" }).click();
    await expect.poll(() => defaultModel).toBe("bound-4");
    await page.reload();
    await expect(input).toHaveValue("MiniMax-M3.1-Flash");
    await expect(effort).toHaveValue("max");
    await effort.selectOption("");
    await page.getByRole("button", { name: "Set as default model" }).click();
    await expect.poll(() => defaultModel).toBe("bound-5");
    expect(choices[5]).toEqual({ model: "MiniMax-M3.1-Flash", reasoning_effort: null });
    await input.fill("MiniMax-M3.1-Future");
    await expect(effort).toHaveValue("");
    await expect(effort).toBeDisabled();
    await page.getByRole("button", { name: "Choose from catalog" }).click();
    await expect(model).toBeVisible();
    expect(pageErrors).toEqual([]);
  });
}
