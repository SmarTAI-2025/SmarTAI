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
  {
    provider_id: "ocr:baidu_unlimited_ocr:record-1",
    provider_type: "baidu_unlimited_ocr",
    provider_kind: "ocr",
    credential_id: "record-1",
    model: "unlimited-ocr-parser",
    enabled: true,
    is_default: false,
    max_concurrent: 1,
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

  it("keeps all three stages independent and does not disable the OCR record", async () => {
    const onQuestionChange = vi.fn();
    const onSubmissionChange = vi.fn();
    const onGradingChange = vi.fn();
    render(
      <>
        <StageProviderSelect
          id="question-provider"
          label="题目识别模型"
          hint=""
          experts={experts}
          value="provider-default"
          locale="zh-CN"
          onChange={onQuestionChange}
        />
        <StageProviderSelect
          id="submission-provider"
          label="作答识别模型"
          hint=""
          experts={experts}
          value="provider-vision"
          locale="zh-CN"
          onChange={onSubmissionChange}
        />
        <StageProviderSelect
          id="grading-provider"
          label="批改模型"
          hint=""
          experts={experts}
          value="ocr:baidu_unlimited_ocr:record-1"
          locale="zh-CN"
          onChange={onGradingChange}
        />
      </>,
    );

    expect(screen.getByRole("combobox", { name: "题目识别模型" })).toHaveValue("provider-default");
    expect(screen.getByRole("combobox", { name: "作答识别模型" })).toHaveValue("provider-vision");
    const grading = screen.getByRole("combobox", { name: "批改模型" });
    expect(grading).toHaveValue("ocr:baidu_unlimited_ocr:record-1");
    expect(screen.getAllByRole("option", { name: /OCR/ }).length).toBe(3);
    expect(grading).toBeEnabled();
    await userEvent.selectOptions(grading, "provider-default");
    expect(onGradingChange).toHaveBeenCalledWith("provider-default");
  });
});
