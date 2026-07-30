import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";

describe("UnsavedChangesDialog", () => {
  it("lets the user save without returning to the original form", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    render(
      <UnsavedChangesDialog
        title="Unsaved grading changes"
        description="Choose what to do with the current edit."
        stayLabel="Keep editing"
        leaveLabel="Discard changes"
        saveLabel="Save & continue"
        onStay={vi.fn()}
        onLeave={vi.fn()}
        onSave={onSave}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Save & continue" }));

    expect(onSave).toHaveBeenCalledOnce();
  });

  it("keeps every decision disabled while saving", () => {
    const onStay = vi.fn();
    render(
      <UnsavedChangesDialog
        title="Unsaved grading changes"
        description="Choose what to do with the current edit."
        stayLabel="Keep editing"
        leaveLabel="Discard changes"
        saveLabel="Save & continue"
        savingLabel="Saving…"
        saving
        onStay={onStay}
        onLeave={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Keep editing" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("alertdialog")).toHaveAttribute("aria-busy", "true");

    fireEscape();
    expect(onStay).not.toHaveBeenCalled();
  });
});

function fireEscape() {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
}
