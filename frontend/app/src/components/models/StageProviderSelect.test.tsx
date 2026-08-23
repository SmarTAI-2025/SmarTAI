import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExpertConfig } from "@/types";
import { StageProviderSelect } from "./StageProviderSelect";

const experts: ExpertConfig[] = [
  {
    provider_id: "provider-default",
    provider_type: "openai",
    model: "gpt-test",
    enabled: true,
    is_default: true,
    max_concurrent: 5,
    rpm: 0,
  },
  {
    provider_id: "provider-vision",
    provider_type: "gemini",
    model: "gemini-vision-test",
    enabled: true,
    is_default: false,
    max_concurrent: 5,
    rpm: 0,
  },
];

describe("StageProviderSelect", () => {
  it("shows the automatic default and lets the teacher explicitly switch stages", async () => {
    const onChange = vi.fn();
    render(
      <StageProviderSelect
        id="recognition-provider"
        label="题目识别模型"
        hint="默认已选，可改选"
        experts={experts}
        value="provider-default"
        locale="zh-CN"
        onChange={onChange}
      />,
    );

    const select = screen.getByRole("combobox", { name: "题目识别模型" });
    expect(select).toHaveValue("provider-default");
    expect(screen.getByRole("option", { name: /默认/ })).toBeInTheDocument();
    await userEvent.selectOptions(select, "provider-vision");
    expect(onChange).toHaveBeenCalledWith("provider-vision");
  });
});
