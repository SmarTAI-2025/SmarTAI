import { describe, expect, it } from "vitest";
import { messages } from "./messages";

function placeholders(value: string) {
  return [...value.matchAll(/\{[^{}]+\}/g)].map((match) => match[0]).sort();
}

describe("interface translations", () => {
  it("keeps Chinese and English keys and placeholders aligned", () => {
    const chineseKeys = Object.keys(messages["zh-CN"]);
    const englishKeys = Object.keys(messages["en-US"]);

    expect(englishKeys).toEqual(chineseKeys);
    for (const key of chineseKeys) {
      const messageKey = key as keyof typeof messages["zh-CN"];
      expect(placeholders(messages["en-US"][messageKey])).toEqual(
        placeholders(messages["zh-CN"][messageKey]),
      );
    }
  });

  it("uses the shared workflow terminology and avoids parenthetical plurals", () => {
    const english = messages["en-US"];

    expect([
      english.newTaskStepUpload,
      english.newTaskStepQuestionReview,
      english.newTaskStepSubmissions,
      english.newTaskStepGradingSetup,
      english.newTaskStepGrading,
      english.newTaskStepReview,
      english.newTaskStepComplete,
    ]).toEqual([
      "Add Questions",
      "Review Questions",
      "Upload Submissions",
      "Review Submissions",
      "Run Grading",
      "Review Results",
      "Results & Analysis",
    ]);
    expect(Object.values(english).join("\n")).not.toMatch(/\b(?:model|item|case|task|problem|answer)\(s\)/i);
  });
});
