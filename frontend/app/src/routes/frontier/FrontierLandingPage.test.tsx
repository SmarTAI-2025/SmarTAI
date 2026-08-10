import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { FrontierLandingPage } from "./FrontierLandingPage";

describe("FrontierLandingPage", () => {
  beforeEach(() => window.localStorage.setItem("smartai_locale", "en-US"));

  it("presents an honest walkthrough and routes to the live demo", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    expect(screen.getByRole("heading", { name: /auditable first pass/i })).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic student data/i).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /enter (the )?live demo/i })[0]).toHaveAttribute("href", "/frontier/enter");
  });

  it("lets the reviewer step through the product walkthrough", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("tab", { name: /03 grade/i }));
    expect(screen.getByText("Tie every point to a rubric signal.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /03 grade/i })).toHaveAttribute("aria-selected", "true");
  });

  it("shows analysis as a distinct stage and answers example questions", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("tab", { name: /04 analyze/i }));
    expect(screen.getByText("Turn one grading run into class insight.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /which hidden code test fails most often/i }));
    expect(screen.getByText(/empty-input handling is the most common gap/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /hidden-test pass rate/i })).toBeInTheDocument();
  });

  it("switches the full showcase to Chinese", () => {
    render(<I18nProvider><MemoryRouter><FrontierLandingPage /></MemoryRouter></I18nProvider>);

    fireEvent.click(screen.getByRole("button", { name: /switch to chinese/i }));
    expect(screen.getByRole("heading", { name: /从成堆作业/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /04 分析/ })).toBeInTheDocument();
  });
});
