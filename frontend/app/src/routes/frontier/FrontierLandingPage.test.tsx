import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { FrontierLandingPage } from "./FrontierLandingPage";

describe("FrontierLandingPage", () => {
  it("presents an honest walkthrough and routes to the live demo", () => {
    render(<MemoryRouter><FrontierLandingPage /></MemoryRouter>);

    expect(screen.getByRole("heading", { name: /auditable first pass/i })).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic student data/i).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /enter (the )?live demo/i })[0]).toHaveAttribute("href", "/login?returnTo=%2Ffrontier%2Flive");
  });

  it("lets the reviewer step through the product walkthrough", () => {
    render(<MemoryRouter><FrontierLandingPage /></MemoryRouter>);

    fireEvent.click(screen.getByRole("tab", { name: /03 grade/i }));
    expect(screen.getByText("Tie every point to a rubric signal.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /03 grade/i })).toHaveAttribute("aria-selected", "true");
  });
});
