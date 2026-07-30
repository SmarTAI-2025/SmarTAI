import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { LanguageToggle } from "./LanguageToggle";

describe("LanguageToggle", () => {
  beforeEach(() => {
    window.localStorage.removeItem("smartai_locale");
    document.documentElement.lang = "";
  });

  it("switches the interface language and persists the choice", async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <LanguageToggle />
      </I18nProvider>,
    );

    const englishButton = screen.getByRole("button", { name: "切换至英文" });
    expect(englishButton).toHaveTextContent("EN");

    await user.click(englishButton);

    expect(screen.getByRole("button", { name: "Switch to Chinese" })).toHaveTextContent("中文");
    expect(window.localStorage.getItem("smartai_locale")).toBe("en-US");
    expect(document.documentElement.lang).toBe("en-US");
  });
});
