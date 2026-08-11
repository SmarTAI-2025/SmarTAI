import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SmarTAIAppMark, SmarTAIWordmark } from "./SmarTAIBrand";

describe("SmarTAIBrand", () => {
  it("uses the approved blue wordmark by default", () => {
    render(<SmarTAIWordmark />);

    expect(screen.getByRole("img", { name: "SmarTAI" })).toHaveAttribute(
      "src",
      "/brand/smartai-wordmark-blue.svg",
    );
  });

  it("supports the silver promotional app mark and white wordmark", () => {
    const { container } = render(
      <div>
        <SmarTAIAppMark finish="silver" />
        <SmarTAIWordmark tone="white" alt="" />
      </div>,
    );

    expect(container.querySelector('[data-smartai-app-mark="silver"]')).toHaveAttribute(
      "src",
      "/brand/smartai-app-mark-silver.png",
    );
    expect(container.querySelector('[data-smartai-wordmark="white"]')).toHaveAttribute(
      "src",
      "/brand/smartai-wordmark-white.svg",
    );
  });
});
