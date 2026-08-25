import { LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist/legacy/build/pdf.mjs";
import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { Button } from "@/components/ui/Button";

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

const MAX_RENDERED_PAGES = 100;

export function PdfDocumentPreview({
  url,
  title,
  loadingLabel,
  errorTitle,
  errorDescription,
  retryLabel,
  openLabel,
}: {
  url: string;
  title: string;
  loadingLabel: string;
  errorTitle: string;
  errorDescription: string;
  retryLabel: string;
  openLabel: string;
}) {
  const [attempt, setAttempt] = useState(0);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState(false);
  const [canvasFailed, setCanvasFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const loadingTask = getDocument({ url });
    setDocument(null);
    setError(false);
    setCanvasFailed(false);
    void loadingTask.promise.then((nextDocument) => {
      if (cancelled) return;
      setDocument(nextDocument);
    }).catch(() => {
      if (!cancelled) setError(true);
    });
    return () => {
      cancelled = true;
      void loadingTask.destroy();
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
          <a href={url} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center rounded-md border px-3 text-xs font-semibold text-primary hover:bg-muted">
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
            <a href={url} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-md border px-2.5 text-xs font-semibold text-primary hover:bg-muted">
              {openLabel}
            </a>
          </div>
        </div>
        <object data={url} type="application/pdf" title={title} className="min-h-0 flex-1 bg-white">
          <a href={url} target="_blank" rel="noreferrer" className="p-4 text-sm font-semibold text-primary">{openLabel}</a>
        </object>
      </div>
    );
  }

  const pageCount = Math.min(document.numPages, MAX_RENDERED_PAGES);
  return (
    <div
      role="document"
      aria-label={title}
      className="h-full w-full overflow-auto rounded-[7px] bg-slate-300/80 px-2 py-3 dark:bg-slate-950/45 sm:px-3"
    >
      <div className="mx-auto grid w-full max-w-[960px] gap-3">
        {Array.from({ length: pageCount }, (_, index) => (
          <PdfCanvasPage
            key={`${url}:${index + 1}`}
            document={document}
            pageNumber={index + 1}
            onRenderError={() => setCanvasFailed(true)}
          />
        ))}
      </div>
    </div>
  );
}

function PdfCanvasPage({
  document,
  pageNumber,
  onRenderError,
}: {
  document: PDFDocumentProxy;
  pageNumber: number;
  onRenderError: () => void;
}) {
  const shellRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const updateWidth = () => setWidth(Math.max(1, Math.floor(shell.clientWidth)));
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(shell);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!width) return;
    let cancelled = false;
    let renderTask: RenderTask | null = null;
    void (async () => {
      try {
        const page = await document.getPage(pageNumber);
        if (cancelled) return;
        const baseViewport = page.getViewport({ scale: 1 });
        const cssScale = width / Math.max(1, baseViewport.width);
        const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
        const viewport = page.getViewport({ scale: cssScale * pixelRatio });
        const canvas = canvasRef.current;
        if (!canvas) return;
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.style.width = `${Math.round(viewport.width / pixelRatio)}px`;
        canvas.style.height = `${Math.round(viewport.height / pixelRatio)}px`;

        // Supplying the context explicitly avoids Safari's intermittent failure
        // when pdf.js lazily creates a 2D context with Chromium-only hints.
        const canvasContext = canvas.getContext("2d", { alpha: false });
        if (!canvasContext) throw new Error("A 2D canvas context is unavailable.");
        renderTask = page.render({
          canvas: null,
          canvasContext,
          viewport,
          background: "rgb(255,255,255)",
        });
        await renderTask.promise;
        if (!cancelled) setError(false);
      } catch (renderError: unknown) {
        if (cancelled || (renderError as { name?: string })?.name === "RenderingCancelledException") return;
        // Do not include the source URL, document contents, or raw exception in diagnostics.
        console.error(`PDF page ${pageNumber} render failed`);
        setError(true);
        onRenderError();
      }
    })();
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [document, onRenderError, pageNumber, width]);

  return (
    <div ref={shellRef} className="min-h-24 w-full overflow-hidden bg-white shadow-[0_8px_28px_rgb(15_23_42_/_0.16)]">
      {error ? <div className="flex min-h-40 items-center justify-center px-4 text-center text-xs text-danger">Page {pageNumber} could not be rendered.</div> : null}
      <canvas ref={canvasRef} aria-label={`PDF page ${pageNumber}`} className={error ? "hidden" : "block max-w-full bg-white"} />
    </div>
  );
}
