import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SmarTAIMascot } from "./SmarTAIMascot";

describe("SmarTAIMascot", () => {
  it("renders the requested animation as decorative content with a reduced-motion fallback", () => {
    const { container } = render(<SmarTAIMascot variant="grading" size="sm" />);
    const mascot = container.querySelector('[data-smartai-mascot="grading"]');
    const images = mascot?.querySelectorAll("img");

    expect(mascot).toHaveAttribute("aria-hidden", "true");
    expect(images).toHaveLength(2);
    expect(images?.[0]).toHaveAttribute("src", "/brand/smartai-loading-rapid-grading.svg");
    expect(images?.[0]).toHaveClass("motion-reduce:hidden");
    expect(images?.[1]).toHaveAttribute("src", "/brand/smartai-mascot.svg");
    expect(images?.[1]).toHaveClass("motion-reduce:block");
  });

  it("uses only the static companion for the idle variant", () => {
    const { container } = render(<SmarTAIMascot variant="idle" />);
    const images = container.querySelectorAll('[data-smartai-mascot="idle"] img');

    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute("src", "/brand/smartai-mascot.svg");
  });
});
