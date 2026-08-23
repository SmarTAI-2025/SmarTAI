import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResultsSummaryMetric } from "./ResultsSummaryMetric";

describe("ResultsSummaryMetric", () => {
  it.each([
    ["primary", "Students"],
    ["accent", "Mean score"],
    ["secondary", "Median score"],
    ["warning", "Lowest score"],
    ["danger", "Review signals"],
  ] as const)("renders the %s result tone with readable content", (tone, label) => {
    render(<ResultsSummaryMetric label={label} value="72%" tone={tone} />);

    expect(screen.getByText(label).parentElement).toHaveAttribute("data-result-tone", tone);
    expect(screen.getByText(label).parentElement).toHaveClass("border");
    expect(screen.getByText("72%")).toHaveClass("text-primary");
  });
});
