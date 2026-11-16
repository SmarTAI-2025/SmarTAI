import { describe, expect, it } from "vitest";
import {
  hasFriendlyModelName,
  modelDisplayName,
  modelSecondaryLabel,
  providerDisplayName,
  sortByProvider,
} from "./modelPresentation";

const unnamedGemini = {
  provider_id: "105dd07ce3a6453ebbb8927cb5260767",
  provider_type: "gemini",
  model: "gemini-3.1-flash-lite-preview",
  display_name: "105dd07ce3a6453ebbb8927cb5260767",
};

describe("model presentation", () => {
  it.each(["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol"])("keeps shared %s concise even with a cached disambiguated label", (model) => {
    const oldLabel = `OpenAI · ${model} · api.openai.com/v1 · OpenAI Chat Completions · ${model.slice(-8)}`;
    const expert = { provider_id: `openai:${model}`, provider_type: "openai", model, is_shared: true,
      display_name: oldLabel, resolved_display_name: oldLabel };
    expect(modelDisplayName(expert)).toBe(`OpenAI · ${model}`);
    expect(modelDisplayName({ ...expert, provider_id: "personal", is_shared: false })).toBe(oldLabel);
  });
  it("orders providers without mutating records or changing order within a provider", () => {
    const records = [
      { provider_type: "qwen", provider_id: "q", is_default: true },
      { provider_type: "openai", provider_id: "o2" },
      { provider_type: "gemini", provider_id: "g" },
      { provider_type: "openai", provider_id: "o1" },
      { provider_type: "baidu_unlimited_ocr", provider_id: "b" },
    ];
    expect(sortByProvider(records).map((item) => item.provider_id)).toEqual(["g", "o2", "o1", "q", "b"]);
    expect(records[0]).toEqual({ provider_type: "qwen", provider_id: "q", is_default: true });
  });
  it("replaces an internal provider id with the actual model name", () => {
    expect(hasFriendlyModelName(unnamedGemini)).toBe(false);
    expect(modelDisplayName(unnamedGemini)).toBe("gemini-3.1-flash-lite-preview");
    expect(modelSecondaryLabel(unnamedGemini)).toBe("Google Gemini");
  });

  it("keeps a user-defined configuration name and retains model context", () => {
    const named = { ...unnamedGemini, display_name: "Calculus Gemini" };

    expect(hasFriendlyModelName(named)).toBe(true);
    expect(modelDisplayName(named)).toBe("Calculus Gemini");
    expect(modelSecondaryLabel(named)).toBe(
      "Google Gemini · gemini-3.1-flash-lite-preview",
    );
  });

  it("uses stable provider labels", () => {
    expect(providerDisplayName("openai")).toBe("GPT (OpenAI)");
    expect(providerDisplayName("zhipu")).toBe("Zhipu (智谱)");
  });
});
