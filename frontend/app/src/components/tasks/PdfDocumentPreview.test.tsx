import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PdfDocumentPreview } from "./PdfDocumentPreview";

const mocks = vi.hoisted(() => ({
  getDocument: vi.fn(),
  getPage: vi.fn(),
  render: vi.fn(),
  cancel: vi.fn(),
  destroy: vi.fn(),
}));

vi.mock("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url", () => ({ default: "/pdf.worker.mjs" }));
vi.mock("pdfjs-dist/legacy/build/pdf.mjs", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: mocks.getDocument,
}));

describe("PdfDocumentPreview", () => {
  const canvasContext = {} as CanvasRenderingContext2D;

  beforeEach(() => {
    mocks.render.mockReturnValue({ promise: Promise.resolve(), cancel: mocks.cancel });
    mocks.getPage.mockResolvedValue({
      getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: 140 * scale }),
      render: mocks.render,
    });
    mocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 1, getPage: mocks.getPage }),
      destroy: mocks.destroy,
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(canvasContext);
    vi.stubGlobal("ResizeObserver", class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        Object.defineProperty(target, "clientWidth", { configurable: true, value: 600 });
        this.callback([], this as unknown as ResizeObserver);
      }
      disconnect() {}
      unobserve() {}
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders through an explicit 2D context for Safari compatibility", async () => {
    render(<PdfDocumentPreview
      url="/fixture.pdf"
      title="Fixture PDF"
      loadingLabel="Loading"
      errorTitle="Failed"
      errorDescription="Try again"
      retryLabel="Retry"
      openLabel="Open"
    />);

    expect(await screen.findByRole("document", { name: "Fixture PDF" })).toBeInTheDocument();
    await waitFor(() => expect(mocks.render).toHaveBeenCalled());
    expect(mocks.render).toHaveBeenCalledWith(expect.objectContaining({
      canvas: null,
      canvasContext,
      background: "rgb(255,255,255)",
    }));
    expect(screen.getByLabelText("PDF page 1")).toBeVisible();
  });

  it("falls back to the browser PDF viewer when canvas rendering fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.render.mockImplementationOnce(() => ({
      promise: Promise.reject(new Error("canvas failed")),
      cancel: mocks.cancel,
    }));

    render(<PdfDocumentPreview
      url="/fixture.pdf"
      title="Fixture PDF"
      loadingLabel="Loading"
      errorTitle="Failed"
      errorDescription="Compatibility fallback"
      retryLabel="Retry"
      openLabel="Open"
    />);

    expect(await screen.findByText("Compatibility fallback")).toBeInTheDocument();
    const fallback = document.querySelector('object[data="/fixture.pdf"]');
    expect(fallback).toHaveAttribute("type", "application/pdf");
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });
});
