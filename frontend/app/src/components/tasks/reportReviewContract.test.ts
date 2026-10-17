import { expect, it } from "vitest";
import type { Correction } from "@/types";
import contract from "@/test/fixtures/report_review_contract.json";
import { correctionReviewAttention, correctionReviewNextBlocked } from "./resultsModel";

it.each(contract.cases)("report and review queue agree: $name", ({ patch, pending, required }) => {
  expect(correctionReviewAttention({ ...contract.base, ...patch } as Correction) !== "none").toBe(pending);
  expect(correctionReviewNextBlocked({ ...contract.base, ...patch } as Correction)).toBe(required);
});

