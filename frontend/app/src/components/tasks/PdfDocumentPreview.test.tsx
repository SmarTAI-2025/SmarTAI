import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PdfDocumentPreview } from "./PdfDocumentPreview";

const mocks = vi.hoisted(() => ({
  getDocument: vi.fn(),
  getPage: vi.fn(),
  render: vi.fn(),
  cancel: vi.fn(),
  destroy: vi.fn(),
  cleanup: vi.fn(),
}));

vi.mock("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url", () => ({ default: "/pdf.worker.mjs" }));
vi.mock("pdfjs-dist/legacy/build/pdf.mjs", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: mocks.getDocument,
}));

describe("PdfDocumentPreview", () => {
  const canvasContext = {} as CanvasRenderingContext2D;

  const props = { url: "/fixture.pdf", title: "Fixture PDF", loadingLabel: "Loading", errorTitle: "Failed", errorDescription: "Try again", retryLabel: "Retry", openLabel: "Open" };
  let resize: () => void;
  let width = 600;

  beforeEach(() => {
    vi.clearAllMocks();
    width = 600;
    mocks.destroy.mockResolvedValue(undefined);
    mocks.render.mockReturnValue({ promise: Promise.resolve(), cancel: mocks.cancel });
    mocks.getPage.mockResolvedValue({
      getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: 140 * scale }),
      render: mocks.render,
      cleanup: mocks.cleanup,
    });
    mocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 1, getPage: mocks.getPage }),
      destroy: mocks.destroy,
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(canvasContext);
    vi.stubGlobal("ResizeObserver", class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        Object.defineProperty(target, "clientWidth", { configurable: true, get: () => width });
        Object.defineProperty(target, "clientHeight", { configurable: true, value: 500 });
        resize = () => this.callback([], this as unknown as ResizeObserver);
        resize();
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

  it("reaches the last page of a thousand-page book without loading every page", async () => {
    mocks.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 1000, getPage: mocks.getPage }), destroy: mocks.destroy });
    render(<PdfDocumentPreview url="/long.pdf" title="Long book" initialPage={800} loadingLabel="Loading" errorTitle="Failed" errorDescription="Retry" retryLabel="Retry" openLabel="Open" />);
    expect(await screen.findByLabelText("PDF page 800")).toBeVisible();
    expect(document.querySelectorAll("canvas").length).toBeLessThanOrEqual(4);
    fireEvent.change(screen.getByRole("spinbutton", { name: "PDF page" }), { target: { value: "1000" } });
    fireEvent.keyDown(screen.getByRole("spinbutton", { name: "PDF page" }), { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("PDF page 1000")).toBeVisible());
    expect(document.querySelectorAll("canvas").length).toBeLessThanOrEqual(3);
    expect(mocks.getPage.mock.calls.length).toBeLessThan(12);
    expect(screen.getByRole("button", { name: "Next pages" })).toBeDisabled();
  });

  function longDocument(numPages = 15) {
    mocks.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages, getPage: mocks.getPage }), destroy: mocks.destroy });
  }

  async function scrollToPage(page: number) {
    const scroll = screen.getByTestId("pdf-scroll-container");
    fireEvent.scroll(scroll, { target: { scrollTop: 12 + ((width - 24) * 1.4 + 12) * (page - 1) } });
    await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(page));
    await waitFor(() => expect(screen.getByLabelText(`PDF page ${page}`)).toHaveAttribute("data-rendered", "true"));
  }

  it("scrolls through and back across every page including the former three-page stop", async () => {
    longDocument();
    render(<PdfDocumentPreview {...props} />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    for (const page of [2, 3, 4, 5, 8, 12, 15, 7, 1]) {
      await scrollToPage(page);
      expect(document.querySelectorAll("canvas").length).toBeLessThanOrEqual(4);
    }
    expect(mocks.getDocument).toHaveBeenCalledTimes(1);
    expect(mocks.cleanup).toHaveBeenCalled();
  });

  it("commits multi-digit input once, clamps boundaries, and restores invalid/empty input", async () => {
    longDocument();
    render(<PdfDocumentPreview {...props} />);
    const input = await screen.findByRole("spinbutton");
    const jump = (value: string) => {
      fireEvent.change(input, { target: { value } });
      fireEvent.keyDown(input, { key: "Enter" });
    };
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.change(input, { target: { value: "12" } });
    expect(screen.getByTestId("pdf-scroll-container").scrollTop).toBe(12);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input).toHaveValue(12);
    fireEvent.click(screen.getByRole("button", { name: "Next pages" }));
    expect(input).toHaveValue(13);
    fireEvent.click(screen.getByRole("button", { name: "Previous pages" }));
    expect(input).toHaveValue(12);
    jump("99");
    expect(input).toHaveValue(15);
    expect(screen.getByRole("button", { name: "Next pages" })).toBeDisabled();
    jump("-2");
    expect(input).toHaveValue(1);
    expect(screen.getByRole("button", { name: "Previous pages" })).toBeDisabled();
    jump("4.9");
    expect(input).toHaveValue(4);
    jump("");
    expect(input).toHaveValue(4);
    jump("invalid");
    expect(input).toHaveValue(4);
    fireEvent.change(input, { target: { value: "7" } });
    fireEvent.blur(input);
    expect(input).toHaveValue(7);
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue(7);
  });

  it("supports a single page and disables both boundary arrows", async () => {
    render(<PdfDocumentPreview {...props} />);
    await screen.findByRole("document");
    expect(screen.getByRole("button", { name: "Next pages" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous pages" })).toBeDisabled();
    expect(document.querySelectorAll("canvas")).toHaveLength(1);
  });

  it("preserves focused page input across scroll updates and returns keyboard focus to reading", async () => {
    longDocument();
    render(<PdfDocumentPreview {...props} />);
    const input = await screen.findByRole("spinbutton");
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    act(() => input.focus());
    fireEvent.scroll(screen.getByTestId("pdf-scroll-container"), { target: { scrollTop: 12 + (576 * 1.4 + 12) * 4 } });
    await waitFor(() => expect(screen.getByLabelText("PDF page 5")).toHaveAttribute("data-rendered", "true"));
    expect(input).toHaveValue(1);
    fireEvent.change(input, { target: { value: "12" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input).toHaveValue(12);
    expect(screen.getByTestId("pdf-scroll-container")).toHaveFocus();
    act(() => input.focus());
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue(12);
    expect(screen.getByTestId("pdf-scroll-container")).toHaveFocus();
    await scrollToPage(13);
  });

  it("zooms within its limits, preserves the reading anchor and resets for a new source", async () => {
    longDocument();
    const view = render(<PdfDocumentPreview {...props} initialPage={8} />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 8")).toHaveAttribute("data-rendered", "true"));
    const pageWidth = () => parseFloat((screen.getByLabelText("PDF page 8") as HTMLCanvasElement).style.width);
    const scroll = screen.getByTestId("pdf-scroll-container");
    fireEvent.scroll(scroll, { target: { scrollTop: 12 + (576 * 1.4 + 12) * 7 + 100 } });
    await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(8));
    fireEvent.click(screen.getByRole("button", { name: "Zoom in PDF" }));
    await waitFor(() => expect(pageWidth()).toBe(720));
    expect(scroll.scrollTop).toBeCloseTo(12 + (720 * 1.4 + 12) * 7 + 125);
    expect(screen.getByRole("spinbutton")).toHaveValue(8);
    for (let index = 0; index < 6; index++) fireEvent.click(screen.getByRole("button", { name: "Zoom in PDF" }));
    expect(screen.getByRole("button", { name: "Zoom in PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Fit PDF width" })).toHaveTextContent("200%");
    await waitFor(() => expect(pageWidth()).toBe(1152));
    for (const canvas of document.querySelectorAll("canvas")) expect(canvas.width * canvas.height).toBeLessThanOrEqual(4_000_000);
    for (let index = 0; index < 8; index++) fireEvent.click(screen.getByRole("button", { name: "Zoom out PDF" }));
    expect(screen.getByRole("button", { name: "Zoom out PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Fit PDF width" })).toHaveTextContent("50%");
    fireEvent.click(screen.getByRole("button", { name: "Fit PDF width" }));
    await waitFor(() => expect(pageWidth()).toBe(576));
    expect(screen.getByRole("spinbutton")).toHaveValue(8);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in PDF" }));
    view.rerender(<PdfDocumentPreview {...props} url="/other.pdf" />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    expect(screen.getByRole("button", { name: "Fit PDF width" })).toHaveTextContent("100%");
  });

  it("preserves external question/citation navigation after manual scrolling and resizing", async () => {
    longDocument(30);
    const view = render(<PdfDocumentPreview {...props} initialPage={1} />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    view.rerender(<PdfDocumentPreview {...props} initialPage={17} />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 17")).toHaveAttribute("data-rendered", "true"));
    await scrollToPage(20);
    act(() => { width = 400; resize(); });
    await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(20));
    expect(screen.getByTestId("pdf-scroll-container").scrollTop).toBeCloseTo(12 + (376 * 1.4 + 12) * 19);
    view.rerender(<PdfDocumentPreview {...props} initialPage={6} />);
    await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(6));
    await waitFor(() => expect(screen.getByLabelText("PDF page 6")).toBeVisible());
  });

  it("keeps the reading anchor when mixed page sizes are discovered", async () => {
    longDocument(30);
    mocks.getPage.mockImplementation(async (page: number) => ({
      getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: (page % 2 ? 140 : 70) * scale }),
      render: mocks.render, cleanup: mocks.cleanup,
    }));
    render(<PdfDocumentPreview {...props} initialPage={17} />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 17")).toHaveAttribute("data-rendered", "true"));
    expect(screen.getByRole("spinbutton")).toHaveValue(17);
    const shell = document.querySelector('[data-pdf-page="17"]') as HTMLElement;
    expect(screen.getByTestId("pdf-scroll-container").scrollTop).toBeCloseTo(parseFloat(shell.style.top));
  });

  it("ignores a late document after a quick source switch and destroys both workers", async () => {
    let finish!: (value: unknown) => void;
    const oldDestroy = vi.fn().mockResolvedValue(undefined);
    mocks.getDocument.mockReturnValueOnce({ promise: new Promise((resolve) => { finish = resolve; }), destroy: oldDestroy });
    const view = render(<PdfDocumentPreview {...props} url="/old.pdf" />);
    view.rerender(<PdfDocumentPreview {...props} url="/new.pdf" />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toBeVisible());
    await act(async () => finish({ numPages: 900, getPage: mocks.getPage }));
    expect(screen.getByRole("spinbutton")).toHaveAttribute("max", "1");
    expect(oldDestroy).toHaveBeenCalledOnce();
    view.unmount();
    expect(mocks.destroy).toHaveBeenCalledOnce();
  });

  it("clears old canvases, cancels pending renders, and ignores their late failures", async () => {
    let reject!: (reason: Error) => void;
    mocks.render.mockReturnValueOnce({ promise: new Promise((_resolve, fail) => { reject = fail; }), cancel: mocks.cancel });
    const view = render(<PdfDocumentPreview {...props} />);
    await waitFor(() => expect(mocks.render).toHaveBeenCalled());
    const oldCanvas = screen.getByLabelText("PDF page 1") as HTMLCanvasElement;
    view.rerender(<PdfDocumentPreview {...props} url="/second.pdf" />);
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    await act(async () => reject(new Error("old render failed")));
    expect(screen.queryByText("Try again")).not.toBeInTheDocument();
    expect(oldCanvas.width).toBe(0);
    expect(oldCanvas.height).toBe(0);
    expect(mocks.cancel).toHaveBeenCalled();
    expect(mocks.cleanup).toHaveBeenCalled();
    const canvas = screen.getByLabelText("PDF page 1") as HTMLCanvasElement;
    view.unmount();
    expect(canvas.width).toBe(0);
    expect(canvas.height).toBe(0);
  });

  it("does not render a page request that finishes after unmount", async () => {
    let finish!: (value: unknown) => void;
    mocks.getPage.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const view = render(<PdfDocumentPreview {...props} />);
    await waitFor(() => expect(mocks.getPage).toHaveBeenCalled());
    view.unmount();
    await act(async () => finish({ cleanup: mocks.cleanup }));
    expect(mocks.render).not.toHaveBeenCalled();
    expect(mocks.cleanup).toHaveBeenCalled();
  });

  it("shows a recoverable load failure and rejects empty documents", async () => {
    mocks.getDocument.mockReturnValueOnce({ promise: Promise.resolve({ numPages: 0 }), destroy: mocks.destroy });
    render(<PdfDocumentPreview {...props} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByLabelText("PDF page 1")).toHaveAttribute("data-rendered", "true"));
    expect(mocks.destroy).toHaveBeenCalledOnce();
  });

});
