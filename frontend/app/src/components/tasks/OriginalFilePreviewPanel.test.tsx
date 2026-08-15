import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { messages, type MessageKey } from "@/i18n/messages";
import type { SourceFileDescriptor } from "@/types/sourcePreview";
import { OriginalFilePreviewPanel } from "./OriginalFilePreviewPanel";
import { OriginalFilePreviewTrigger } from "./OriginalFilePreviewTrigger";

vi.mock("./PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ url, title }: { url: string; title: string }) => <object data={url} title={title} />,
}));

const t = (key: MessageKey) => messages["zh-CN"][key];
const availablePdf: SourceFileDescriptor = {
  file_id: "mock:problem:task-1",
  display_name: "期中题目.pdf",
  mime_type: "application/pdf",
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};

describe("OriginalFilePreviewPanel", () => {
  it("shows loading, PDF content, and a focused close action", async () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <OriginalFilePreviewPanel descriptor={availablePdf} loadState="loading" previewUrl={null} onClose={onClose} onRetry={vi.fn()} t={t} />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在读取原文件");
    expect(screen.getByRole("button", { name: "关闭对照" })).toHaveFocus();

    rerender(
      <OriginalFilePreviewPanel descriptor={availablePdf} loadState="ready" previewUrl="blob:mock-pdf" onClose={onClose} onRetry={vi.fn()} provenanceNote="Verified source note" t={t} />,
    );
    await waitFor(() => expect(document.querySelector("object")).toHaveAttribute("data", "blob:mock-pdf"));
    expect(screen.getByText("Verified source note")).toBeInTheDocument();
  });

  it("explains processing and supports a recoverable read failure", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const processing: SourceFileDescriptor = { ...availablePdf, status: "processing" };
    const { rerender } = render(
      <OriginalFilePreviewPanel descriptor={processing} loadState="idle" previewUrl={null} onClose={vi.fn()} onRetry={onRetry} t={t} />,
    );
    expect(screen.getByText("文件仍在处理中")).toBeInTheDocument();

    rerender(
      <OriginalFilePreviewPanel descriptor={availablePdf} loadState="error" previewUrl={null} onClose={vi.fn()} onRetry={onRetry} t={t} />,
    );
    await user.click(screen.getByRole("button", { name: "重新读取" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("keeps unavailable actions disabled and gives keyboard-readable reasons", () => {
    const unavailable: SourceFileDescriptor = {
      ...availablePdf,
      status: "unavailable",
      preview_kind: "unsupported",
      unavailable_reason: "unsupported_type",
    };
    render(
      <OriginalFilePreviewTrigger descriptor={unavailable} open={false} onOpen={vi.fn()} onClose={vi.fn()} t={t} />,
    );

    expect(screen.getByRole("button", { name: "查看原文件" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "当前只支持 PDF 和常见图片格式。" })).toBeInTheDocument();
  });
});
