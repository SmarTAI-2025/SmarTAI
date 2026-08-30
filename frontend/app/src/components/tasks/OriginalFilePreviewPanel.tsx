import { FileText, Image, LoaderCircle, RefreshCw, X } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type {
  SourceFileDescriptor,
  SourcePreviewErrorCode,
  SourcePreviewKind,
  SourcePreviewLoadState,
  SourceUnavailableReason,
} from "@/types/sourcePreview";

const PdfDocumentPreview = lazy(async () => {
  const module = await import("./PdfDocumentPreview");
  return { default: module.PdfDocumentPreview };
});

export function OriginalFilePreviewPanel({
  descriptor,
  displayName,
  previewKind,
  loadState,
  errorCode,
  previewUrl,
  unavailableReason,
  onClose,
  onRetry,
  provenanceNote,
  t,
}: {
  descriptor: SourceFileDescriptor | null;
  displayName: string;
  previewKind: SourcePreviewKind;
  loadState: SourcePreviewLoadState;
  errorCode: SourcePreviewErrorCode | null;
  previewUrl: string | null;
  unavailableReason?: SourceUnavailableReason | null;
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
      className="flex flex-col overflow-hidden rounded-[10px] border bg-card lg:h-[calc(100vh-102px)]"
      data-testid="source-preview-panel"
      data-source-preview-panel="true"
    >
      <header className="flex min-h-14 items-center justify-between gap-3 border-b bg-card px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[7px] bg-primary/10 text-primary">
            {previewKind === "image"
              ? <Image aria-hidden="true" className="h-4 w-4" />
              : <FileText aria-hidden="true" className="h-4 w-4" />}
          </span>
          <div className="min-w-0">
            <h2 id="source-preview-title" className="text-sm font-bold text-foreground">{t("sourcePreviewTitle")}</h2>
            <p className="truncate text-[11px] leading-4 text-muted-foreground" title={displayName || undefined}>
              {displayName || t("sourcePreviewUnknownFile")}
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
        <PreviewContent
          descriptor={descriptor}
          displayName={displayName}
          previewKind={previewKind}
          loadState={loadState}
          errorCode={errorCode}
          previewUrl={previewUrl}
          unavailableReason={unavailableReason}
          onRetry={onRetry}
          t={t}
        />
      </div>
      {provenanceNote ? <p className="border-t bg-card px-4 py-2.5 text-[11px] leading-4 text-muted-foreground">{provenanceNote}</p> : null}
    </section>
  );
}

function PreviewContent({ descriptor, displayName, previewKind, loadState, errorCode, previewUrl, unavailableReason, onRetry, t }: {
  descriptor: SourceFileDescriptor | null;
  displayName: string;
  previewKind: SourcePreviewKind;
  loadState: SourcePreviewLoadState;
  errorCode: SourcePreviewErrorCode | null;
  previewUrl: string | null;
  unavailableReason?: SourceUnavailableReason | null;
  onRetry: () => void;
  t: (key: MessageKey) => string;
}) {
  if (descriptor?.status === "processing") {
    return (
      <InlineNotice tone="warning" title={t("sourcePreviewProcessingTitle")} className="max-w-md bg-card">
        {t("sourcePreviewProcessingDescription")}
      </InlineNotice>
    );
  }
  if (descriptor?.status === "cleanup_pending") {
    return (
      <InlineNotice tone="warning" title={t("sourcePreviewCleanupTitle")} className="max-w-md bg-card">
        {unavailableDescription(descriptor.unavailable_reason, t)}
      </InlineNotice>
    );
  }
  if (descriptor?.status === "unavailable") {
    return (
      <InlineNotice
        tone="neutral"
        title={t("sourcePreviewUnavailableTitle")}
        className="max-w-md bg-card"
      >
        {unavailableDescription(descriptor.unavailable_reason, t)}
      </InlineNotice>
    );
  }
  if (loadState === "error") {
    if (errorCode === "source_cleanup_pending") {
      return (
        <InlineNotice tone="warning" title={t("sourcePreviewCleanupTitle")} className="max-w-md bg-card">
          {errorDescription(errorCode, t)}
        </InlineNotice>
      );
    }
    if (errorCode === "source_unavailable_task_finalized" || errorCode === "source_unavailable_missing") {
      return (
        <InlineNotice tone="neutral" title={t("sourcePreviewUnavailableTitle")} className="max-w-md bg-card">
          {errorDescription(errorCode, t)}
        </InlineNotice>
      );
    }
    const retryable = errorCode === "source_preview_processing"
      || errorCode === "source_preview_storage_unavailable"
      || errorCode === "source_preview_load_failed";
    return (
      <InlineNotice
        tone="danger"
        title={t(errorCode === "source_preview_processing"
          ? "sourcePreviewProcessingTitle"
          : errorCode === "source_preview_not_found"
              || errorCode === "source_preview_unavailable"
              || errorCode === "source_preview_unsupported_type"
            ? "sourcePreviewUnavailableTitle"
            : "sourcePreviewErrorTitle")}
        className="max-w-md bg-card"
        action={retryable ? (
          <Button type="button" variant="secondary" className="h-8 px-3" onClick={onRetry}>
            <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
            {t("sourcePreviewRetry")}
          </Button>
        ) : undefined}
      >
        {errorDescription(errorCode, t)}
      </InlineNotice>
    );
  }
  if (!descriptor && unavailableReason) {
    return (
      <InlineNotice tone="neutral" title={t("sourcePreviewUnavailableTitle")} className="max-w-md bg-card">
        {unavailableDescription(unavailableReason, t)}
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
  if (previewKind === "image") {
    return (
      <div className="flex h-full w-full items-center justify-center overflow-auto rounded-[8px] bg-slate-200/70 p-3 dark:bg-slate-950/35">
        <img
          src={previewUrl}
          alt={displayName}
          className="max-h-full max-w-full rounded-[3px] bg-white object-contain shadow-[0_8px_28px_rgb(15_23_42_/_0.12)]"
        />
      </div>
    );
  }
  return (
    <Suspense fallback={<PreviewLoading t={t} />}>
      <PdfDocumentPreview
        url={previewUrl}
        title={`${t("sourcePreviewTitle")} · ${displayName}`}
        loadingLabel={t("sourcePreviewLoading")}
        errorTitle={t("sourcePreviewErrorTitle")}
        errorDescription={t("sourcePreviewErrorDescription")}
        retryLabel={t("sourcePreviewRetry")}
        openLabel={t("sourcePreviewPdfFallback")}
      />
    </Suspense>
  );
}

function PreviewLoading({ t }: { t: (key: MessageKey) => string }) {
  return (
    <div role="status" className="flex max-w-sm flex-col items-center text-center text-muted-foreground">
      <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" />
      <p className="mt-3 text-sm font-semibold text-foreground">{t("sourcePreviewLoading")}</p>
    </div>
  );
}

function StatusBadge({ descriptor, t }: {
  descriptor: SourceFileDescriptor | null;
  t: (key: MessageKey) => string;
}) {
  if (!descriptor) return null;
  const processing = descriptor.status === "processing" || descriptor.status === "cleanup_pending";
  const unavailable = descriptor.status === "unavailable";
  return (
    <span className={cn(
      "hidden rounded-full px-2.5 py-1 text-[10px] font-semibold sm:inline-flex",
      processing && "bg-warning/10 text-warning",
      unavailable && "bg-muted text-muted-foreground",
      !processing && !unavailable && "bg-accent/10 text-accent",
    )}>
      {t(descriptor.status === "cleanup_pending" ? "sourcePreviewCleanupBadge" : processing ? "sourcePreviewProcessingBadge" : unavailable ? "sourcePreviewUnavailableBadge" : "sourcePreviewReadyBadge")}
    </span>
  );
}

function unavailableDescription(reason: SourceUnavailableReason | null | undefined, t: (key: MessageKey) => string) {
  switch (reason) {
    case "unsupported_type":
      return t("sourcePreviewUnsupportedReason");
    case "missing":
      return t("sourcePreviewMissingReason");
    case "storage_unavailable":
      return t("sourcePreviewStorageUnavailableReason");
    case "cleanup_pending":
      return t("sourcePreviewCleanupReason");
    case "task_finalized":
      return t("sourcePreviewTaskFinalizedReason");
    case "storage_delete_failed":
      return t("sourcePreviewCleanupRetryReason");
    default:
      return t("sourcePreviewNotPersistedReason");
  }
}

function errorDescription(errorCode: SourcePreviewErrorCode | null, t: (key: MessageKey) => string) {
  switch (errorCode) {
    case "source_preview_processing":
      return t("sourcePreviewProcessingDescription");
    case "source_preview_not_found":
      return t("sourcePreviewMissingReason");
    case "source_preview_unavailable":
      return t("sourcePreviewNotPersistedReason");
    case "source_preview_unsupported_type":
      return t("sourcePreviewUnsupportedReason");
    case "source_preview_storage_unavailable":
      return t("sourcePreviewStorageUnavailableReason");
    case "source_cleanup_pending":
      return t("sourcePreviewCleanupReason");
    case "source_unavailable_task_finalized":
      return t("sourcePreviewTaskFinalizedReason");
    case "source_unavailable_missing":
      return t("sourcePreviewMissingReason");
    default:
      return t("sourcePreviewErrorDescription");
  }
}
