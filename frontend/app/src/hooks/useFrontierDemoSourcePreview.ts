import { useCallback, useEffect, useMemo, useState } from "react";
import type { SourceFileDescriptor, SourcePreviewLoadState } from "@/types/sourcePreview";

const FRONTIER_TASK_PREFIXES = [
  "SmarTAI Live Demo",
  // Read-only compatibility for tasks created before the public-brand cleanup.
  "AWS Frontier Live Demo",
] as const;

const SUBMISSION_FIXTURES: Record<string, { path: string; kind: "pdf" | "image"; mime: string }> = {
  "DEMO-001_typeset.pdf": { path: "live/DEMO-001_typeset_raw.pdf", kind: "pdf", mime: "application/pdf" },
  "DEMO-002_handwritten.pdf": { path: "live/DEMO-002_handwritten_raw.pdf", kind: "pdf", mime: "application/pdf" },
  "DEMO-003_mixed.pdf": { path: "live/DEMO-003_mixed_raw.pdf", kind: "pdf", mime: "application/pdf" },
  "scan_004.pdf": { path: "live/scan_004_raw.pdf", kind: "pdf", mime: "application/pdf" },
};

const QUESTION_FIXTURE = {
  displayName: "question_source.pdf",
  path: "live/question_source.pdf",
  kind: "pdf" as const,
  mime: "application/pdf",
};

interface FixtureSource {
  displayName: string;
  path: string;
  kind: "pdf" | "image";
  mime: string;
}

export function isFrontierDemoTask(taskName: string | null | undefined) {
  return Boolean(taskName && FRONTIER_TASK_PREFIXES.some((prefix) => taskName.startsWith(prefix)));
}

export function useFrontierDemoSourcePreview({
  enabled,
  sourceFilename,
  questionSource = false,
}: {
  enabled: boolean;
  sourceFilename?: string | null;
  questionSource?: boolean;
}) {
  const source = useMemo<FixtureSource | null>(() => {
    if (!enabled) return null;
    if (questionSource) return QUESTION_FIXTURE;
    if (!sourceFilename) return null;
    const fixture = SUBMISSION_FIXTURES[sourceFilename];
    return fixture ? { ...fixture, displayName: sourceFilename } : null;
  }, [enabled, questionSource, sourceFilename]);
  const [open, setOpen] = useState(false);
  const [loadState, setLoadState] = useState<SourcePreviewLoadState>("idle");
  const previewUrl = source ? `/frontier-demo/${source.path}` : null;

  useEffect(() => {
    setOpen(false);
    setLoadState("idle");
  }, [source?.path]);

  const descriptor = useMemo<SourceFileDescriptor>(() => source ? {
    file_id: `frontier:${source.path}`,
    display_name: source.displayName,
    mime_type: source.mime,
    status: "available",
    preview_kind: source.kind,
  } : {
    file_id: "frontier:missing",
    display_name: sourceFilename ?? "",
    mime_type: "application/octet-stream",
    status: "unavailable",
    preview_kind: "unsupported",
    unavailable_reason: "missing",
  }, [source, sourceFilename]);

  const retry = useCallback(() => {
    setLoadState(source ? "ready" : "error");
    setOpen(Boolean(source));
  }, [source]);

  const openPreview = useCallback(() => {
    setOpen(Boolean(source));
    setLoadState(source ? "ready" : "error");
  }, [source]);

  return {
    available: Boolean(source),
    descriptor,
    loadState,
    open,
    previewUrl,
    close: () => setOpen(false),
    openPreview,
    retry,
  };
}
