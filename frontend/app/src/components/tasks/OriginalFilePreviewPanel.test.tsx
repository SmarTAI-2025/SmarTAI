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
  source_id: "source-problem-1",
  file_id: "file-problem-1",
  display_name: "期中题目.pdf",
  mime_type: "application/pdf",
  size_bytes: 42,
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};

describe("OriginalFilePreviewPanel", () => {
  it("shows loading, PDF content, and a focused close action", async () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <OriginalFilePreviewPanel descriptor={availablePdf} displayName={availablePdf.display_name} previewKind="pdf" loadState="loading" errorCode={null} previewUrl={null} onClose={onClose} onRetry={vi.fn()} t={t} />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在读取原文件");
    expect(screen.getByRole("button", { name: "关闭对照" })).toHaveFocus();

    rerender(
      <OriginalFilePreviewPanel descriptor={availablePdf} displayName={availablePdf.display_name} previewKind="pdf" loadState="ready" errorCode={null} previewUrl="/test/source.pdf" onClose={onClose} onRetry={vi.fn()} provenanceNote="Verified source note" t={t} />,
    );
    await waitFor(() => expect(document.querySelector("object")).toHaveAttribute("data", "/test/source.pdf"));
    expect(screen.getByText("Verified source note")).toBeInTheDocument();
  });

  it("distinguishes safe storage failures from permanent missing files", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const processing: SourceFileDescriptor = { ...availablePdf, status: "processing" };
    const { rerender } = render(
      <OriginalFilePreviewPanel descriptor={processing} displayName={processing.display_name} previewKind="pdf" loadState="idle" errorCode={null} previewUrl={null} onClose={vi.fn()} onRetry={onRetry} t={t} />,
    );
    expect(screen.getByText("文件仍在处理中")).toBeInTheDocument();

    rerender(
      <OriginalFilePreviewPanel descriptor={null} displayName={availablePdf.display_name} previewKind="pdf" loadState="error" errorCode="source_preview_not_found" previewUrl={null} onClose={vi.fn()} onRetry={onRetry} t={t} />,
    );
    expect(screen.getByText("原文件缺失或未记录，当前无法预览。请重新上传后再试。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新读取" })).not.toBeInTheDocument();
    expect(onRetry).not.toHaveBeenCalled();

    rerender(
      <OriginalFilePreviewPanel descriptor={availablePdf} displayName={availablePdf.display_name} previewKind="pdf" loadState="error" errorCode="source_preview_storage_unavailable" previewUrl={null} onClose={vi.fn()} onRetry={onRetry} t={t} />,
    );
    expect(screen.getByText("原文件存储暂时不可用，请稍后重新读取。")).toBeInTheDocument();
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
      <OriginalFilePreviewTrigger state="unavailable" unavailableReason={unavailable.unavailable_reason} open={false} onOpen={vi.fn()} onClose={vi.fn()} t={t} />,
    );

    expect(screen.getByRole("button", { name: "查看原文件" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "当前只支持 PDF 和常见图片格式。" })).toBeInTheDocument();
  });

  it("settles a pending open panel into the safe missing state", () => {
    render(
      <OriginalFilePreviewPanel descriptor={null} displayName="" previewKind="unsupported" loadState="idle" errorCode={null} previewUrl={null} unavailableReason="missing" onClose={vi.fn()} onRetry={vi.fn()} t={t} />,
    );

    expect(screen.getByText("原文件缺失或未记录，当前无法预览。请重新上传后再试。")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders an allowed image Blob without inventing a source URL", () => {
    const image: SourceFileDescriptor = {
      ...availablePdf,
      file_id: "file-image-1",
      display_name: "answer.webp",
      mime_type: "image/webp",
      size_bytes: 45,
      preview_kind: "image",
    };
    render(
      <OriginalFilePreviewPanel descriptor={image} displayName={image.display_name} previewKind="image" loadState="ready" errorCode={null} previewUrl="blob:safe-image" onClose={vi.fn()} onRetry={vi.fn()} t={t} />,
    );

    expect(screen.getByRole("img", { name: "answer.webp" })).toHaveAttribute("src", "blob:safe-image");
  });
});
