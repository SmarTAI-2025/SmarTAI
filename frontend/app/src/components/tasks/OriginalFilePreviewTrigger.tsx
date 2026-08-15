import { Eye, LoaderCircle, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { HelpTooltip } from "@/components/ui/HelpTooltip";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type { SourceFileDescriptor, SourceUnavailableReason } from "@/types/sourcePreview";

export function OriginalFilePreviewTrigger({
  descriptor,
  open,
  onOpen,
  onClose,
  t,
  openLabel,
  className,
}: {
  descriptor: SourceFileDescriptor;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  t: (key: MessageKey) => string;
  openLabel?: string;
  className?: string;
}) {
  const unavailable = descriptor.status === "unavailable";
  const resolvedOpenLabel = openLabel ?? t("sourcePreviewOpen");
  const label = open ? t("sourcePreviewClose") : resolvedOpenLabel;
  const icon = open
    ? <X aria-hidden="true" className="h-4 w-4" />
    : descriptor.status === "processing"
      ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
      : <Eye aria-hidden="true" className="h-4 w-4" />;

  if (unavailable) {
    const reason = unavailableReason(descriptor.unavailable_reason, t);
    return (
      <span className={cn("inline-flex min-w-0 items-center gap-1", className)}>
        <Button type="button" variant="secondary" className="h-10 px-3" disabled>
          <Eye aria-hidden="true" className="h-4 w-4" />
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
    case "task_finalized":
      return t("sourcePreviewFinalizedReason");
    case "unsupported_type":
      return t("sourcePreviewUnsupportedReason");
    case "missing":
      return t("sourcePreviewMissingReason");
    default:
      return t("sourcePreviewNotPersistedReason");
  }
}
