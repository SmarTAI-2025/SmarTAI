import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SourceComparisonWorkspace } from "./SourceComparisonWorkspace";

describe("SourceComparisonWorkspace", () => {
  it("starts at 50/50 and supports keyboard resizing and reset", () => {
    render(
      <SourceComparisonWorkspace open preview={<div>Original</div>} separatorLabel="Resize comparison">
        <div>Recognized</div>
      </SourceComparisonWorkspace>,
    );

    const separator = screen.getByRole("separator", { name: "Resize comparison" });
    expect(separator).toHaveAttribute("aria-valuenow", "50");

    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator).toHaveAttribute("aria-valuenow", "48");

    fireEvent.keyDown(separator, { key: "ArrowRight", shiftKey: true });
    expect(separator).toHaveAttribute("aria-valuenow", "58");

    fireEvent.keyDown(separator, { key: "End" });
    expect(separator).toHaveAttribute("aria-valuenow", "65");

    fireEvent.doubleClick(separator);
    expect(separator).toHaveAttribute("aria-valuenow", "50");
  });

  it("updates the split with pointer dragging without requiring a render per move", () => {
    Object.defineProperty(window, "PointerEvent", { configurable: true, value: MouseEvent });
    const setPointerCapture = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: setPointerCapture });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });

    render(
      <SourceComparisonWorkspace open preview={<div>Original</div>} separatorLabel="Resize comparison">
        <div>Recognized</div>
      </SourceComparisonWorkspace>,
    );

    const separator = screen.getByRole("separator", { name: "Resize comparison" });
    const workspace = separator.parentElement as HTMLDivElement;
    vi.spyOn(workspace, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 1200,
      bottom: 800,
      width: 1200,
      height: 800,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(separator, { button: 0, clientX: 594, pointerId: 1 });
    fireEvent.pointerMove(separator, { clientX: 700, pointerId: 1 });

    expect(workspace.style.getPropertyValue("--source-preview-share")).not.toBe("50fr");
    fireEvent.pointerUp(separator, { clientX: 700, pointerId: 1 });
    expect(Number(separator.getAttribute("aria-valuenow"))).toBeGreaterThan(50);
    expect(document.documentElement.style.userSelect).toBe("");
  });

  it("keeps the content mounted while the preview is closed", () => {
    const { rerender } = render(
      <SourceComparisonWorkspace open={false} preview={<div>Original</div>} separatorLabel="Resize comparison">
        <input aria-label="Draft" defaultValue="teacher edit" />
      </SourceComparisonWorkspace>,
    );

    const input = screen.getByRole("textbox", { name: "Draft" });
    fireEvent.change(input, { target: { value: "unsaved edit" } });
    rerender(
      <SourceComparisonWorkspace open preview={<div>Original</div>} separatorLabel="Resize comparison">
        <input aria-label="Draft" defaultValue="teacher edit" />
      </SourceComparisonWorkspace>,
    );

    expect(screen.getByRole("textbox", { name: "Draft" })).toHaveValue("unsaved edit");
  });
});
