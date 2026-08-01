import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { useImeSafeQuery } from "./useImeSafeQuery";

function QueryHarness({ initialValue = "", onCommit = vi.fn() }: {
  initialValue?: string;
  onCommit?: (value: string) => void;
}) {
  const [value, setValue] = useState(initialValue);
  const query = useImeSafeQuery({
    value,
    onCommit: (nextValue) => {
      onCommit(nextValue);
      setValue(nextValue);
    },
  });
  return (
    <input
      aria-label="IME-safe search"
      value={query.draftValue}
      onBlur={query.handleBlur}
      onChange={query.handleChange}
      onCompositionEnd={query.handleCompositionEnd}
      onCompositionStart={query.handleCompositionStart}
    />
  );
}

function SubmitHarness({ onSubmit }: { onSubmit: (value: string) => void }) {
  const [value, setValue] = useState("");
  const query = useImeSafeQuery({ value, onCommit: setValue });

  return (
    <form onSubmit={(event) => {
      event.preventDefault();
      onSubmit(query.commitDraft());
    }}>
      <input
        aria-label="Immediate IME search"
        value={query.draftValue}
        onChange={query.handleChange}
        onCompositionEnd={(event) => {
          query.handleCompositionEnd(event);
          onSubmit(query.commitDraft());
        }}
        onCompositionStart={query.handleCompositionStart}
      />
      <button type="submit">Search</button>
    </form>
  );
}

describe("useImeSafeQuery", () => {
  it("commits only the final Chinese composition value", async () => {
    const onCommit = vi.fn();
    render(<QueryHarness onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "IME-safe search" });

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "s" } });
    fireEvent.change(input, { target: { value: "sa" } });
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.change(input, { target: { value: "三" } });

    expect(input).toHaveValue("三");
    expect(onCommit).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.change(input, { target: { value: "三" } });

    await waitFor(() => {
      expect(onCommit).toHaveBeenCalledTimes(1);
      expect(onCommit).toHaveBeenLastCalledWith("三");
    });
  });

  it("flushes the final value synchronously before a blur action continues", () => {
    const onCommit = vi.fn();
    render(<QueryHarness onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "IME-safe search" });

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.compositionEnd(input);
    fireEvent.change(input, { target: { value: "三" } });
    fireEvent.blur(input);

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenLastCalledWith("三");
  });

  it("exposes the final composition value before the zero-delay flush runs", () => {
    const onSubmit = vi.fn();
    render(<SubmitHarness onSubmit={onSubmit} />);
    const input = screen.getByRole("textbox", { name: "Immediate IME search" });

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.compositionEnd(input, { data: "三", target: { value: "三" } });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenLastCalledWith("三");
  });

  it("keeps the caret before the Chinese character through repeated deletions", async () => {
    const user = userEvent.setup();
    render(<QueryHarness initialValue="ssasan三" />);
    const input = screen.getByRole("textbox", { name: "IME-safe search" }) as HTMLInputElement;
    await user.click(input);
    input.setSelectionRange(6, 6);

    for (const [value, caret] of [
      ["ssasa三", 5],
      ["ssas三", 4],
      ["ssa三", 3],
      ["ss三", 2],
      ["s三", 1],
      ["三", 0],
    ] as const) {
      await user.keyboard("{Backspace}");
      await waitFor(() => expect(input).toHaveValue(value));
      expect(input.selectionStart).toBe(caret);
      expect(input.selectionEnd).toBe(caret);
      expect(input).toHaveFocus();
    }
  });
});
