import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { FrontierLandingPage } from "./FrontierLandingPage";

describe("FrontierLandingPage", () => {
  beforeEach(() => window.localStorage.setItem("smartai_locale", "en-US"));

  it("presents an honest walkthrough and routes to the live demo", () => {
    const { container } = render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    expect(screen.getByRole("heading", { name: /faster review.*evidence intact/i })).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic student data/i).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /enter (the )?live demo/i })[0]).toHaveAttribute("href", "/frontier/enter");
    expect(container.querySelectorAll('[data-smartai-wordmark="blue"]')).toHaveLength(4);
    expect(container.querySelector('[data-smartai-app-mark="crystal-blue"]')).toBeInTheDocument();
    expect(screen.getByText("Smart AI Teaching Assistant")).toBeInTheDocument();
    expect(container.querySelector('[data-smartai-app-mark="silver"]')).toBeInTheDocument();
    expect(container.querySelector('[data-smartai-wordmark="white"]')).toBeInTheDocument();
    expect(container.querySelector(".frontier-wordmark-mark")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Film" })).toHaveAttribute("href", "#film");
    expect(screen.getByLabelText("SmarTAI promotional film with English narration")).toBeInTheDocument();
  });

  it("lets the reviewer step through the product walkthrough", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("tab", { name: /03 grade/i }));
    expect(screen.getByText(/Tie every point/)).toBeInTheDocument();
    expect(screen.getByText(/to its evidence/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /03 grade/i })).toHaveAttribute("aria-selected", "true");
  });

  it("presents source comparison without claiming unsupported glyph confidence", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("tab", { name: /02 recognize/i }));
    expect(screen.getByText("Source comparison open")).toBeInTheDocument();
    expect(screen.getByText("Complete source")).toBeInTheDocument();
    expect(screen.getByText("Editable")).toBeInTheDocument();
    expect(screen.queryByText(/OCR candidate/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/source confidence/i)).not.toBeInTheDocument();
  });

  it("shows analysis as a distinct stage with varied supported charts", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("tab", { name: /04 analyze/i }));
    expect(screen.getByText(/Turn grading results/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Scatter chart: Attainment by question/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /how spread out are overall scores/i }));
    expect(screen.getByRole("img", { name: /Box chart: Overall score distribution/i })).toBeInTheDocument();
    expect(screen.getByText(/77% median/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /what share of students scored below 70%/i }));
    expect(screen.getByRole("img", { name: /Pie chart: Score threshold share/i })).toBeInTheDocument();
    expect(screen.getAllByText("25%").length).toBeGreaterThan(0);
  });

  it("switches the full showcase to Chinese", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("button", { name: /switch to chinese/i }));
    expect(screen.getByRole("heading", { name: /从成堆作业/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /04 分析/ })).toBeInTheDocument();
  });
});
