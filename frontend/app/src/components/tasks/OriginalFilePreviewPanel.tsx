import { FileText, Image, LoaderCircle, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type { SourceFileDescriptor, SourcePreviewLoadState, SourceUnavailableReason } from "@/types/sourcePreview";

export function OriginalFilePreviewPanel({
  descriptor,
  loadState,
  previewUrl,
  onClose,
  onRetry,
  provenanceNote,
  t,
}: {
  descriptor: SourceFileDescriptor;
  loadState: SourcePreviewLoadState;
  previewUrl: string | null;
  onClose: () => void;
  onRetry: () => void;
  provenanceNote?: string;
  t: (key: MessageKey) => string;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButtonRef.current?.focus();
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    onClose();
  }

  return (
    <section
      aria-labelledby="source-preview-title"
      onKeyDown={handleKeyDown}
      className="flex flex-col overflow-hidden rounded-[10px] border bg-card lg:sticky lg:top-[86px] lg:h-[calc(100vh-102px)]"
      data-testid="source-preview-panel"
      data-source-preview-panel="true"
    >
      <header className="flex min-h-14 items-center justify-between gap-3 border-b bg-card px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[7px] bg-primary/10 text-primary">
            {descriptor.preview_kind === "image"
              ? <Image aria-hidden="true" className="h-4 w-4" />
              : <FileText aria-hidden="true" className="h-4 w-4" />}
          </span>
          <div className="min-w-0">
            <h2 id="source-preview-title" className="text-sm font-bold text-foreground">{t("sourcePreviewTitle")}</h2>
            <p className="truncate text-[11px] leading-4 text-muted-foreground" title={descriptor.display_name || undefined}>
              {descriptor.display_name || t("sourcePreviewUnknownFile")}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StatusBadge descriptor={descriptor} t={t} />
          <button
            ref={closeButtonRef}
            type="button"
            aria-label={t("sourcePreviewClose")}
            onClick={onClose}
            className="inline-flex h-8 w-8 items-center justify-center rounded-[7px] text-muted-foreground outline-none transition hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div className="flex h-[42vh] min-h-[280px] items-center justify-center overflow-hidden bg-muted p-3 md:h-[52vh] md:p-4 lg:h-auto lg:min-h-0 lg:flex-1">
        <PreviewContent descriptor={descriptor} loadState={loadState} previewUrl={previewUrl} onRetry={onRetry} t={t} />
      </div>
      {provenanceNote ? <p className="border-t bg-card px-4 py-2.5 text-[11px] leading-4 text-muted-foreground">{provenanceNote}</p> : null}
    </section>
  );
}

function PreviewContent({ descriptor, loadState, previewUrl, onRetry, t }: {
  descriptor: SourceFileDescriptor;
  loadState: SourcePreviewLoadState;
  previewUrl: string | null;
  onRetry: () => void;
  t: (key: MessageKey) => string;
}) {
  if (descriptor.status === "processing") {
    return <InlineNotice tone="warning" title={t("sourcePreviewProcessingTitle")} className="max-w-md bg-card">{t("sourcePreviewProcessingDescription")}</InlineNotice>;
  }
  if (descriptor.status === "unavailable") {
    return (
      <InlineNotice tone={descriptor.unavailable_reason === "task_finalized" ? "warning" : "neutral"} title={t("sourcePreviewUnavailableTitle")} className="max-w-md bg-card">
        {unavailableDescription(descriptor.unavailable_reason, t)}
      </InlineNotice>
    );
  }
  if (loadState === "error") {
    return (
      <InlineNotice
        tone="danger"
        title={t("sourcePreviewErrorTitle")}
        className="max-w-md bg-card"
        action={<Button type="button" variant="secondary" className="h-8 px-3" onClick={onRetry}><RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />{t("sourcePreviewRetry")}</Button>}
      >
        {t("sourcePreviewErrorDescription")}
      </InlineNotice>
    );
  }
  if (loadState !== "ready" || !previewUrl) {
    return (
      <div role="status" className="flex max-w-sm flex-col items-center text-center text-muted-foreground">
        <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" />
        <p className="mt-3 text-sm font-semibold text-foreground">{t("sourcePreviewLoading")}</p>
        <p className="mt-1 text-xs leading-5">{t("sourcePreviewLoadingDescription")}</p>
      </div>
    );
  }
  if (descriptor.preview_kind === "image") {
    return (
      <div className="flex h-full w-full items-center justify-center overflow-auto rounded-[8px] bg-slate-200/70 p-3 dark:bg-slate-950/35">
        <img src={previewUrl} alt={descriptor.display_name} className="max-h-full max-w-full rounded-[3px] bg-white object-contain shadow-[0_8px_28px_rgb(15_23_42_/_0.12)]" />
      </div>
    );
  }
  return (
    <object data={previewUrl} type="application/pdf" title={`${t("sourcePreviewTitle")} · ${descriptor.display_name}`} className="h-full w-full rounded-[5px] bg-white shadow-[0_8px_28px_rgb(15_23_42_/_0.12)]">
      <p className="p-4 text-sm text-muted-foreground">{t("sourcePreviewPdfFallback")}</p>
    </object>
  );
}

function StatusBadge({ descriptor, t }: { descriptor: SourceFileDescriptor; t: (key: MessageKey) => string }) {
  const processing = descriptor.status === "processing";
  const unavailable = descriptor.status === "unavailable";
  return (
    <span className={cn(
      "hidden rounded-full px-2.5 py-1 text-[10px] font-semibold sm:inline-flex",
      processing && "bg-warning/10 text-warning",
      unavailable && "bg-muted text-muted-foreground",
      !processing && !unavailable && "bg-accent/10 text-accent",
    )}>
      {t(processing ? "sourcePreviewProcessingBadge" : unavailable ? "sourcePreviewUnavailableBadge" : "sourcePreviewReadyBadge")}
    </span>
  );
}

function unavailableDescription(reason: SourceUnavailableReason | null | undefined, t: (key: MessageKey) => string) {
  switch (reason) {
    case "task_finalized": return t("sourcePreviewFinalizedReason");
    case "unsupported_type": return t("sourcePreviewUnsupportedReason");
    case "missing": return t("sourcePreviewMissingReason");
    default: return t("sourcePreviewNotPersistedReason");
  }
}
