import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { getTaskSourceFiles, loadSourcePreviewFile } from "@/api/sourcePreview";
import { useSourcePreview } from "@/hooks/useSourcePreview";
import { messages, type MessageKey } from "@/i18n/messages";
import type { SourceFileDescriptor } from "@/types/sourcePreview";
import { OriginalFilePreviewPanel } from "./OriginalFilePreviewPanel";
import { OriginalFilePreviewTrigger } from "./OriginalFilePreviewTrigger";

vi.mock("./PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ url, title }: { url: string; title: string }) => <object data={url} title={title} />,
}));

vi.mock("@/api/sourcePreview", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/sourcePreview")>(),
  getTaskSourceFiles: vi.fn(),
  loadSourcePreviewFile: vi.fn(),
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
const availableImage: SourceFileDescriptor = {
  ...availablePdf,
  file_id: "file-image-1",
  display_name: "answer.webp",
  mime_type: "image/webp",
  size_bytes: 45,
  preview_kind: "image",
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
    render(
      <OriginalFilePreviewPanel descriptor={availableImage} displayName={availableImage.display_name} previewKind="image" loadState="ready" errorCode={null} previewUrl="blob:safe-image" onClose={vi.fn()} onRetry={vi.fn()} t={t} />,
    );

    const image = screen.getByAltText("answer.webp");
    expect(image).toHaveAttribute("src", "blob:safe-image");
    expect(screen.getByRole("status")).toHaveTextContent(t("sourcePreviewLoading"));
    expect(screen.queryByText(t("sourcePreviewReadyBadge"))).not.toBeInTheDocument();
    fireEvent.load(image);
    expect(image).toBeVisible();
    expect(screen.getByText(t("sourcePreviewReadyBadge"))).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("retries a failed image through the existing authenticated Blob loader", async () => {
    vi.mocked(getTaskSourceFiles).mockResolvedValue({
      task_id: "task-1", workflow_revision: 1, problem_source: availableImage, submission_sources: {},
    });
    vi.mocked(loadSourcePreviewFile).mockResolvedValue(new Blob(["image"], { type: "image/webp" }));
    const createObjectURL = vi.fn().mockReturnValueOnce("blob:image-first").mockReturnValueOnce("blob:image-retry");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });

    const { unmount } = render(<ImagePreviewWithSource />);
    fireEvent.click(screen.getByRole("button", { name: "Open source" }));
    const firstImage = await screen.findByAltText("answer.webp");
    expect(firstImage).toHaveAttribute("src", "blob:image-first");
    fireEvent.error(firstImage);
    expect(screen.getByText(t("sourcePreviewErrorTitle"))).toBeInTheDocument();
    expect(screen.queryByText(t("sourcePreviewReadyBadge"))).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: t("sourcePreviewRetry") }));
    const retryImage = await screen.findByAltText("answer.webp");
    expect(retryImage).toHaveAttribute("src", "blob:image-retry");
    expect(retryImage).not.toBe(firstImage);
    expect(loadSourcePreviewFile).toHaveBeenCalledTimes(2);
    expect(loadSourcePreviewFile).toHaveBeenLastCalledWith("task-1", availableImage);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:image-first");
    expect(screen.queryByText(t("sourcePreviewReadyBadge"))).not.toBeInTheDocument();
    fireEvent.load(retryImage);
    expect(screen.getByText(t("sourcePreviewReadyBadge"))).toBeInTheDocument();
    expect(screen.queryByText(t("sourcePreviewErrorTitle"))).not.toBeInTheDocument();
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:image-retry");
  });

  it("resets image status on source changes and ignores detached image events", () => {
    const panel = (url: string) => (
      <OriginalFilePreviewPanel descriptor={availableImage} displayName={availableImage.display_name} previewKind="image" loadState="ready" errorCode={null} previewUrl={url} onClose={vi.fn()} onRetry={vi.fn()} t={t} />
    );
    const { rerender } = render(panel("blob:first"));
    const firstImage = screen.getByAltText("answer.webp");
    fireEvent.load(firstImage);
    expect(screen.getByText(t("sourcePreviewReadyBadge"))).toBeInTheDocument();
    rerender(panel("blob:second"));
    expect(screen.queryByText(t("sourcePreviewReadyBadge"))).not.toBeInTheDocument();
    const secondImage = screen.getByAltText("answer.webp");
    fireEvent.error(secondImage);
    expect(screen.getByText(t("sourcePreviewErrorTitle"))).toBeInTheDocument();
    rerender(panel("blob:first"));
    expect(screen.queryByText(t("sourcePreviewReadyBadge"))).not.toBeInTheDocument();
    expect(screen.queryByText(t("sourcePreviewErrorTitle"))).not.toBeInTheDocument();
    fireEvent.load(screen.getByAltText("answer.webp"));
    fireEvent.error(firstImage);
    expect(screen.getByText(t("sourcePreviewReadyBadge"))).toBeInTheDocument();
    expect(screen.queryByText(t("sourcePreviewErrorTitle"))).not.toBeInTheDocument();
  });
});

function ImagePreviewWithSource() {
  const source = useSourcePreview({ taskId: "task-1", workflowRevision: 1, sourceKind: "problem" });
  return <>
    <button type="button" onClick={source.openPreview}>Open source</button>
    {source.isOpen ? <OriginalFilePreviewPanel {...source} onClose={source.closePreview} onRetry={source.retryPreview} t={t} /> : null}
  </>;
}
