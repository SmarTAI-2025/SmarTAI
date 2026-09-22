import { Eye, LoaderCircle, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { HelpTooltip } from "@/components/ui/HelpTooltip";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type { SourcePreviewTriggerState, SourceUnavailableReason } from "@/types/sourcePreview";

export function OriginalFilePreviewTrigger({
  state,
  unavailableReason: unavailableReasonCode,
  open,
  onOpen,
  onClose,
  t,
  openLabel,
  className,
}: {
  state: SourcePreviewTriggerState;
  unavailableReason?: SourceUnavailableReason | null;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  t: (key: MessageKey) => string;
  openLabel?: string;
  className?: string;
}) {
  const unavailable = state === "unavailable";
  const cleanupPending = state === "cleanup_pending";
  const resolvedOpenLabel = openLabel ?? t("sourcePreviewOpen");
  const label = open ? t("sourcePreviewClose") : resolvedOpenLabel;
  const icon = open
    ? <X aria-hidden="true" className="h-4 w-4" />
    : state === "processing" || cleanupPending
      ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
      : <Eye aria-hidden="true" className="h-4 w-4" />;

  if (unavailable || cleanupPending) {
    const reason = unavailableReason(unavailableReasonCode, t);
    return (
      <span className={cn("inline-flex min-w-0 items-center gap-1", className)}>
        <Button type="button" variant="secondary" className="h-10 px-3" disabled>
          {cleanupPending
            ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
            : <Eye aria-hidden="true" className="h-4 w-4" />}
          {resolvedOpenLabel}
        </Button>
        <HelpTooltip label={reason} />
      </span>
    );
  }

  return (
    <Button
      type="button"
      variant="secondary"
      className={cn("h-10 px-3", open && "border-primary/30 bg-primary/5 text-primary", className)}
      aria-expanded={open}
      onClick={open ? onClose : onOpen}
    >
      {icon}
      {label}
    </Button>
  );
}

function unavailableReason(reason: SourceUnavailableReason | null | undefined, t: (key: MessageKey) => string) {
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
