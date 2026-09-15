import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MessageKey } from "@/i18n/messages";
import type { SourceFileDescriptor } from "@/types/sourcePreview";
import { OriginalFilePreviewPanel } from "./OriginalFilePreviewPanel";

vi.mock("./PdfDocumentPreview", () => ({ PdfDocumentPreview: () => null }));

const descriptor: SourceFileDescriptor = {
  file_id: "frontier:handwritten.png",
  display_name: "handwritten.png",
  mime_type: "image/png",
  status: "available",
  preview_kind: "image",
};
const t = (key: MessageKey) => key;

function panel(url: string, source = descriptor) {
  return <OriginalFilePreviewPanel
    descriptor={source}
    loadState="ready"
    previewUrl={url}
    onClose={vi.fn()}
    onRetry={vi.fn()}
    t={t}
  />;
}

describe("OriginalFilePreviewPanel images", () => {
  it("reports a failed image and retries a fresh request before showing it", () => {
    render(panel("/frontier-demo/live/handwritten.png"));
    const firstImage = screen.getByAltText("handwritten.png");
    expect(screen.getByRole("status")).toHaveTextContent("sourcePreviewLoading");
    expect(screen.queryByText("sourcePreviewReadyBadge")).not.toBeInTheDocument();

    fireEvent.error(firstImage);
    expect(screen.getByRole("alert")).toHaveTextContent("sourcePreviewErrorTitle");
    expect(screen.queryByText("sourcePreviewReadyBadge")).not.toBeInTheDocument();
    expect(screen.queryByAltText("handwritten.png")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "sourcePreviewRetry" }));
    const retryImage = screen.getByAltText("handwritten.png");
    expect(retryImage).not.toBe(firstImage);
    const retryUrl = new URL(retryImage.getAttribute("src")!);
    expect(retryUrl.pathname).toBe("/frontier-demo/live/handwritten.png");
    expect(retryUrl.searchParams.get("preview_retry")).toBe("1");
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("sourcePreviewReadyBadge")).not.toBeInTheDocument();

    fireEvent.load(retryImage);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(retryImage).toBeVisible();
    expect(screen.getByText("sourcePreviewReadyBadge")).toBeInTheDocument();
  });

  it("resets image failure and retry state when the selected source changes", () => {
    const { rerender } = render(panel("/frontier-demo/live/handwritten.png"));
    const firstImage = screen.getByAltText("handwritten.png");
    fireEvent.error(firstImage);
    fireEvent.click(screen.getByRole("button", { name: "sourcePreviewRetry" }));
    fireEvent.error(screen.getByAltText("handwritten.png"));

    rerender(panel("/frontier-demo/live/scan.png", { ...descriptor, file_id: "frontier:scan.png", display_name: "scan.png" }));
    const nextImage = screen.getByAltText("scan.png");
    expect(nextImage).toHaveAttribute("src", "/frontier-demo/live/scan.png");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("sourcePreviewReadyBadge")).not.toBeInTheDocument();
    fireEvent.load(nextImage);
    fireEvent.error(firstImage);
    expect(nextImage).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("sourcePreviewReadyBadge")).toBeInTheDocument();

    rerender(panel("/frontier-demo/live/handwritten.png"));
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("sourcePreviewReadyBadge")).not.toBeInTheDocument();
    fireEvent.load(screen.getByAltText("handwritten.png"));
    expect(screen.getByText("sourcePreviewReadyBadge")).toBeInTheDocument();
  });
});
