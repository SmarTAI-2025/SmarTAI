import { ChevronLeft, ChevronRight, LoaderCircle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type PDFPageProxy,
  type RenderTask,
} from "pdfjs-dist/legacy/build/pdf.mjs";
import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { Button } from "@/components/ui/Button";

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

const PAGE_GAP = 12;
const PAGE_PADDING = 12;

function validPage(value: number) {
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : 1;
}

interface PdfDocumentPreviewProps {
  url: string;
  title: string;
  loadingLabel: string;
  errorTitle: string;
  errorDescription: string;
  retryLabel: string;
  openLabel: string;
  initialPage?: number;
}

export function PdfDocumentPreview(props: PdfDocumentPreviewProps) {
  // A source change must remove the old canvases before any new async work starts.
  return <PdfSourcePreview key={props.url} {...props} />;
}

function PdfSourcePreview({
  url, title, loadingLabel, errorTitle, errorDescription, retryLabel, openLabel,
  initialPage = 1,
}: PdfDocumentPreviewProps) {
  const [attempt, setAttempt] = useState(0);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState(false);
  const [canvasFailed, setCanvasFailed] = useState(false);
  const [pageStart, setPageStart] = useState(() => validPage(initialPage));
  const onRenderError = useCallback(() => setCanvasFailed(true), []);
  useEffect(() => { setPageStart(validPage(initialPage)); }, [initialPage, url]);
  const sourcePage = document ? Math.min(document.numPages, validPage(pageStart)) : validPage(initialPage);
  const sourceUrl = `${url.split("#")[0]}#page=${sourcePage}`;

  useEffect(() => {
    let cancelled = false;
    const assets = new URL(`${import.meta.env.BASE_URL}pdfjs/`, window.location.href).href;
    const loadingTask = getDocument({
      url, wasmUrl: `${assets}wasm/`, cMapUrl: `${assets}cmaps/`, cMapPacked: true,
      standardFontDataUrl: `${assets}standard_fonts/`, iccUrl: `${assets}iccs/`,
      stopAtErrors: true,
    });
    setDocument(null);
    setError(false);
    setCanvasFailed(false);
    void loadingTask.promise.then((nextDocument) => {
      if (cancelled) return;
      if (nextDocument.numPages < 1) throw new Error("Empty PDF");
      setDocument(nextDocument);
      setPageStart((value) => Math.min(nextDocument.numPages, Math.max(1, value)));
    }).catch(() => {
      if (!cancelled) setError(true);
    });
    return () => {
      cancelled = true;
      void loadingTask.destroy().catch(() => undefined);
    };
  }, [attempt, url]);

  if (error) {
    return (
      <div role="alert" className="flex max-w-sm flex-col items-center rounded-[9px] border border-danger/25 bg-card px-5 py-5 text-center shadow-sm">
        <p className="text-sm font-bold text-danger">{errorTitle}</p>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{errorDescription}</p>
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          <Button type="button" variant="secondary" className="h-8 px-3" onClick={() => setAttempt((value) => value + 1)}>
            <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
            {retryLabel}
          </Button>
          <a href={sourceUrl} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center rounded-md border px-3 text-xs font-semibold text-primary hover:bg-muted">
            {openLabel}
          </a>
        </div>
      </div>
    );
  }

  if (!document) {
    return (
      <div role="status" className="flex flex-col items-center text-center text-muted-foreground">
        <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" />
        <p className="mt-3 text-sm font-semibold text-foreground">{loadingLabel}</p>
      </div>
    );
  }

  if (canvasFailed) {
    return (
      <div className="flex h-full w-full flex-col overflow-hidden rounded-[7px] bg-slate-300/80 dark:bg-slate-950/45">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-card px-3 py-2 text-xs">
          <span className="text-muted-foreground">{errorDescription}</span>
          <div className="flex items-center gap-2">
            <Button type="button" variant="secondary" className="h-7 px-2.5 text-xs" onClick={() => setAttempt((value) => value + 1)}>
              <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
              {retryLabel}
            </Button>
            <a href={sourceUrl} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-md border px-2.5 text-xs font-semibold text-primary hover:bg-muted">
              {openLabel}
            </a>
          </div>
        </div>
        <object data={sourceUrl} type="application/pdf" title={title} className="min-h-0 flex-1 bg-white">
          <a href={sourceUrl} target="_blank" rel="noreferrer" className="p-4 text-sm font-semibold text-primary">{openLabel}</a>
        </object>
      </div>
    );
  }

  return (
    <ContinuousPdfPages
      key={attempt}
      document={document}
      title={title}
      loadingLabel={loadingLabel}
      initialPage={initialPage}
      onCurrentPage={setPageStart}
      onRenderError={onRenderError}
    />
  );
}

type PageLayout = { offsets: number[]; heights: number[]; total: number };

function pageAt(layout: PageLayout, top: number) {
  let low = 0;
  let high = layout.offsets.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (layout.offsets[middle] <= top) low = middle;
    else high = middle - 1;
  }
  return low;
}

function ContinuousPdfPages({ document, title, loadingLabel, initialPage, onCurrentPage, onRenderError }: {
  document: PDFDocumentProxy;
  title: string;
  loadingLabel: string;
  initialPage: number;
  onCurrentPage: (page: number) => void;
  onRenderError: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const previousLayout = useRef<PageLayout | null>(null);
  const readingAnchor = useRef({ index: 0, fraction: 0 });
  const requestedPage = useRef<number | null>(null);
  const [viewport, setViewport] = useState({ width: 0, height: 0, pixelRatio: 1 });
  const [ratios, setRatios] = useState<Record<number, number>>({});
  const [top, setTop] = useState(0);
  const [currentPage, setCurrentPage] = useState(() => Math.min(document.numPages, validPage(initialPage)));
  const [pageInput, setPageInput] = useState<string | null>(null);
  const width = Math.min(960, Math.max(1, viewport.width - PAGE_PADDING * 2));
  const layout = useMemo(() => {
    const offsets: number[] = [];
    const heights: number[] = [];
    let total = PAGE_PADDING;
    for (let index = 0; index < document.numPages; index += 1) {
      offsets.push(total);
      const height = Math.max(96, width * (ratios[index + 1] ?? ratios[1] ?? Math.SQRT2));
      heights.push(height);
      total += height + PAGE_GAP;
    }
    return { offsets, heights, total };
  }, [document.numPages, ratios, width]);

  const syncPosition = useCallback(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    setTop(scroll.scrollTop);
    const index = pageAt(layout, scroll.scrollTop + 1);
    readingAnchor.current = { index, fraction: (scroll.scrollTop - layout.offsets[index]) / layout.heights[index] };
    // The toolbar prefers the most fully visible page (including short pages), rather
    // than a thin trailing strip of the preceding page.
    let visibleIndex = index;
    let mostVisible = 0;
    for (let candidate = index; candidate < layout.offsets.length && layout.offsets[candidate] < scroll.scrollTop + scroll.clientHeight; candidate += 1) {
      const visible = Math.min(layout.offsets[candidate] + layout.heights[candidate], scroll.scrollTop + scroll.clientHeight) - Math.max(layout.offsets[candidate], scroll.scrollTop);
      const visibility = visible / Math.min(layout.heights[candidate], scroll.clientHeight);
      if (visibility > mostVisible) { mostVisible = visibility; visibleIndex = candidate; }
    }
    const page = visibleIndex + 1;
    setCurrentPage(page);
    onCurrentPage(page);
  }, [layout, onCurrentPage]);

  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const updateViewport = () => setViewport({
      width: scroll.clientWidth, height: scroll.clientHeight,
      pixelRatio: Math.min(2, window.devicePixelRatio || 1),
    });
    updateViewport();
    const observer = new ResizeObserver(updateViewport);
    observer.observe(scroll);
    window.addEventListener("resize", updateViewport);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateViewport);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, []);

  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll || !viewport.width) return;
    if (frameRef.current !== null) { cancelAnimationFrame(frameRef.current); frameRef.current = null; }
    if (requestedPage.current !== initialPage) {
      // initialPage is a navigation request from the existing question/citation UI.
      requestedPage.current = initialPage;
      scroll.scrollTop = layout.offsets[Math.min(document.numPages, validPage(initialPage)) - 1];
      setPageInput(null);
    } else if (previousLayout.current && previousLayout.current !== layout) {
      // Preserve the visible page and position as mixed page sizes become known,
      // or the splitter/browser zoom changes the available width.
      // Use the saved anchor: the browser may already have clamped scrollTop
      // to a smaller document height before this layout effect runs.
      const { index, fraction } = readingAnchor.current;
      scroll.scrollTop = layout.offsets[index] + fraction * layout.heights[index];
    }
    previousLayout.current = layout;
    syncPosition();
  }, [document.numPages, initialPage, layout, syncPosition, viewport.width]);

  const onPageSize = useCallback((page: number, ratio: number) => {
    setRatios((previous) => previous[page] === ratio ? previous : { ...previous, [page]: ratio });
  }, []);

  function jumpTo(value: number) {
    const page = Math.min(document.numPages, validPage(value));
    if (scrollRef.current) scrollRef.current.scrollTop = layout.offsets[page - 1];
    setPageInput(null);
    syncPosition();
  }

  function commitPageInput() {
    const value = Number(pageInput);
    if (pageInput?.trim() && Number.isFinite(value)) jumpTo(value);
    else setPageInput(null);
  }

  const first = Math.max(0, pageAt(layout, top) - 1);
  const last = Math.min(document.numPages - 1, pageAt(layout, top + viewport.height) + 1);
  const pageCount = last - first + 1;
  return (
    <div className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden rounded-[7px] bg-slate-300/80 dark:bg-slate-950/45">
      <div className="mx-auto my-2 flex h-10 shrink-0 items-center gap-2 rounded-md border bg-card px-2 text-xs">
        <button type="button" title="Previous pages" aria-label="Previous pages" disabled={currentPage === 1} onClick={() => jumpTo(currentPage - 1)} className="h-8 w-8 disabled:opacity-40"><ChevronLeft className="mx-auto h-4 w-4" /></button>
        <input type="number" aria-label="PDF page" min={1} max={document.numPages} step={1} value={pageInput ?? currentPage}
          onChange={(event) => setPageInput(event.target.value)} onBlur={commitPageInput}
          onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); commitPageInput(); }
            if (event.key === "Escape") { event.stopPropagation(); setPageInput(null); }
          }} className="h-8 w-20 rounded border bg-background px-2 tabular-nums" />
        <span className="min-w-12 tabular-nums">/ {document.numPages}</span>
        <button type="button" title="Next pages" aria-label="Next pages" disabled={currentPage === document.numPages} onClick={() => jumpTo(currentPage + 1)} className="h-8 w-8 disabled:opacity-40"><ChevronRight className="mx-auto h-4 w-4" /></button>
      </div>
      <div ref={scrollRef} role="document" aria-label={title} tabIndex={0} data-testid="pdf-scroll-container"
        className="min-h-0 flex-1 overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        style={{ overflowAnchor: "none" }}
        onScroll={(event) => {
          const offset = event.currentTarget.scrollTop;
          const index = pageAt(layout, offset + 1);
          readingAnchor.current = { index, fraction: (offset - layout.offsets[index]) / layout.heights[index] };
          if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
          frameRef.current = requestAnimationFrame(() => { frameRef.current = null; syncPosition(); });
        }}>
        <div className="relative mx-auto" style={{ width, height: layout.total + Math.max(0, viewport.height - layout.heights[document.numPages - 1]) }}>
          {viewport.width > 0 ? Array.from({ length: pageCount }, (_, index) => {
            const pageIndex = first + index;
            return <div key={pageIndex} data-pdf-page={pageIndex + 1} className="absolute left-0 w-full overflow-hidden bg-white shadow-[0_8px_28px_rgb(15_23_42_/_0.16)]" style={{ top: layout.offsets[pageIndex], height: layout.heights[pageIndex] }}>
              <PdfCanvasPage document={document} pageNumber={pageIndex + 1} width={width} pixelRatio={viewport.pixelRatio}
                pixelBudget={4_000_000}
                loadingLabel={loadingLabel} onPageSize={onPageSize} onRenderError={onRenderError} />
            </div>;
          }) : null}
        </div>
      </div>
    </div>
  );
}

function PdfCanvasPage({ document, pageNumber, width, pixelRatio, pixelBudget, loadingLabel, onPageSize, onRenderError }: {
  document: PDFDocumentProxy;
  pageNumber: number;
  width: number;
  pixelRatio: number;
  pixelBudget: number;
  loadingLabel: string;
  onPageSize: (page: number, ratio: number) => void;
  onRenderError: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderedSize, setRenderedSize] = useState("");
  const sizeKey = `${width}:${pixelRatio}:${pixelBudget}`;
  const ready = renderedSize === sizeKey;
  useEffect(() => {
    let cancelled = false;
    let renderTask: RenderTask | null = null;
    let page: PDFPageProxy | null = null;
    // Each render owns its canvas, so a cancelled render can never paint over
    // its replacement after resize or a quick return to the same page.
    const canvas = canvasRef.current;
    void (async () => {
      try {
        page = await document.getPage(pageNumber);
        if (cancelled || !canvas) return;
        const baseViewport = page.getViewport({ scale: 1 });
        onPageSize(pageNumber, baseViewport.height / Math.max(1, baseViewport.width));
        const cssScale = width / Math.max(1, baseViewport.width);
        const renderScale = Math.min(cssScale * pixelRatio, Math.sqrt(pixelBudget / (baseViewport.width * baseViewport.height)), 16384 / Math.max(baseViewport.width, baseViewport.height));
        const viewport = page.getViewport({ scale: renderScale });
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${baseViewport.height * cssScale}px`;
        // Preserve Safari's explicit context and all existing PDF.js asset paths.
        const canvasContext = canvas.getContext("2d", { alpha: false });
        if (!canvasContext) throw new Error("A 2D canvas context is unavailable.");
        renderTask = page.render({ canvas: null, canvasContext, viewport, background: "rgb(255,255,255)" });
        await renderTask.promise;
        if (!cancelled) setRenderedSize(sizeKey);
      } catch (renderError: unknown) {
        if (cancelled || (renderError as { name?: string })?.name === "RenderingCancelledException") return;
        console.error(`PDF page ${pageNumber} render failed`);
        onRenderError();
      } finally {
        if (cancelled) {
          if (canvas) { canvas.width = 0; canvas.height = 0; }
          page?.cleanup();
        }
      }
    })();
    return () => {
      cancelled = true;
      renderTask?.cancel();
      if (canvas) { canvas.width = 0; canvas.height = 0; }
      // PDF.js defers cleanup if cancellation is still settling.
      page?.cleanup();
    };
  }, [document, onPageSize, onRenderError, pageNumber, pixelBudget, pixelRatio, sizeKey, width]);

  return (
    <div aria-busy={!ready} className="relative h-full w-full">
      {!ready ? <div role="status" className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">{loadingLabel} · {pageNumber}</div> : null}
      <canvas key={sizeKey} ref={canvasRef} aria-label={`PDF page ${pageNumber}`} data-rendered={ready} className={ready ? "block bg-white" : "invisible"} />
    </div>
  );
}
