import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  SmarTAIAppMark,
  SmarTAINavigationBrand,
  SmarTAIWordmark,
} from "./SmarTAIBrand";

describe("SmarTAIBrand", () => {
  it("uses only the restrained formal-product marks", () => {
    const { container } = render(
      <div>
        <SmarTAIAppMark />
        <SmarTAIWordmark />
      </div>,
    );

    expect(screen.getByRole("img", { name: "SmarTAI" })).toHaveAttribute(
      "src",
      "/brand/smartai-wordmark-blue.svg",
    );
    expect(container.querySelector('[data-smartai-app-mark="flat-blue"]')).toHaveAttribute(
      "src",
      "/brand/smartai-app-mark-flat-blue.svg",
    );
  });

  it("uses the compact app mark below the safe wordmark breakpoint", () => {
    const { container } = render(<SmarTAINavigationBrand />);

    expect(container.querySelector('[data-smartai-app-mark="flat-blue"]')).toHaveClass(
      "min-[390px]:hidden",
    );
    expect(container.querySelector('[data-smartai-wordmark="blue"]')).toHaveClass(
      "hidden",
      "min-[390px]:block",
    );
  });
});
