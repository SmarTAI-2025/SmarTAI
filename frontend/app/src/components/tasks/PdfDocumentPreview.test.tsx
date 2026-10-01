import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    expect(mocks.getDocument).toHaveBeenCalledWith(expect.objectContaining({
      url: "/fixture.pdf", wasmUrl: expect.stringMatching(/\/pdfjs\/wasm\/$/),
      cMapUrl: expect.stringMatching(/\/pdfjs\/cmaps\/$/), cMapPacked: true,
      standardFontDataUrl: expect.stringMatching(/\/pdfjs\/standard_fonts\/$/),
      iccUrl: expect.stringMatching(/\/pdfjs\/iccs\/$/), stopAtErrors: true,
    }));
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
    const fallback = document.querySelector('object[data="/fixture.pdf#page=1"]');
    expect(fallback).toHaveAttribute("type", "application/pdf");
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it("keeps the cited page when rendering falls back to a browser viewer", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 20, getPage: mocks.getPage }), destroy: mocks.destroy });
    mocks.render.mockImplementation(() => ({ promise: Promise.reject(new Error("decode failed")), cancel: mocks.cancel }));
    render(<PdfDocumentPreview url="/fixture.pdf#page=1" initialPage={17} title="Cited page" loadingLabel="Loading" errorTitle="Failed" errorDescription="Fallback" retryLabel="Retry" openLabel="Open" />);
    await screen.findByText("Fallback");
    expect(document.querySelector("object")).toHaveAttribute("data", "/fixture.pdf#page=17");
    expect(screen.getAllByRole("link", { name: "Open" })).toSatisfy((links: HTMLElement[]) => links.every((link) => link.getAttribute("href") === "/fixture.pdf#page=17"));
  });

  it("keeps the citation page in the open-original link after a load error", async () => {
    mocks.getDocument.mockReturnValue({ promise: Promise.reject(new Error("decode failed")), destroy: mocks.destroy });
    render(<PdfDocumentPreview url="/fixture.pdf" initialPage={17} title="Cited page" loadingLabel="Loading" errorTitle="Failed" errorDescription="Fallback" retryLabel="Retry" openLabel="Open" />);
    await screen.findByRole("alert");
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/fixture.pdf#page=17");
  });

  it("reaches the last page of a thousand-page book with only three mounted canvases", async () => {
    mocks.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 1000, getPage: mocks.getPage }), destroy: mocks.destroy });
    render(<PdfDocumentPreview url="/long.pdf" title="Long book" initialPage={800} loadingLabel="Loading" errorTitle="Failed" errorDescription="Retry" retryLabel="Retry" openLabel="Open" />);
    expect(await screen.findByLabelText("PDF page 800")).toBeVisible();
    expect(document.querySelectorAll("canvas")).toHaveLength(3);
    fireEvent.change(screen.getByRole("spinbutton", { name: "PDF page" }), { target: { value: "1000" } });
    expect(await screen.findByLabelText("PDF page 1000")).toBeVisible();
    expect(document.querySelectorAll("canvas")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Next pages" })).toBeDisabled();
  });
});
