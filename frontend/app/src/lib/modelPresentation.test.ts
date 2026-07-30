import { describe, expect, it } from "vitest";
import {
  hasFriendlyModelName,
  modelDisplayName,
  modelSecondaryLabel,
  providerDisplayName,
} from "./modelPresentation";

const unnamedGemini = {
  provider_id: "105dd07ce3a6453ebbb8927cb5260767",
  provider_type: "gemini",
  model: "gemini-3.1-flash-lite-preview",
  display_name: "105dd07ce3a6453ebbb8927cb5260767",
};

describe("model presentation", () => {
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
    expect(providerDisplayName("openai")).toBe("OpenAI");
    expect(providerDisplayName("zhipu")).toBe("Zhipu AI");
  });
});
