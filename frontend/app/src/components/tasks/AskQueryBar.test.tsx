import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { AskQueryBar } from "./AskQueryBar";

function Harness({ pending = false, apply = vi.fn() }: { pending?: boolean; apply?: (value: string) => void }) {
  const [value, setValue] = useState("Q1");
  return <AskQueryBar locale="zh-CN" value={value} onChange={setValue} onApply={apply} pending={pending} placeholder="按满分升序" />;
}
describe("the shared Ask field", () => {
  it("does not submit a Chinese IME composition but submits the committed text once", () => {
    const apply = vi.fn(); render(<Harness apply={apply} />);
    const input = screen.getByRole("textbox");
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "按满分升序" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229, isComposing: true });
    fireEvent.submit(input.closest("form")!);
    expect(apply).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input, { data: "序" });
    fireEvent.submit(input.closest("form")!);
    expect(apply).toHaveBeenCalledExactlyOnceWith("按满分升序");
  });
  it("keeps the input editable and clearable during a slow request", () => {
    render(<Harness pending />);
    const input = screen.getByRole("textbox");
    expect(input).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "理解中…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "清空查询" }));
    expect(input).toHaveValue("");
  });
});
