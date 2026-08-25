import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getTaskSourceFiles, loadSourcePreviewFile, sourcePreviewErrorCode } from "@/api/sourcePreview";
import { inferSourcePreviewKind } from "@/lib/sourcePreview";
import type {
  SourceFileDescriptor,
  SourcePreviewErrorCode,
  SourcePreviewLoadState,
  SourcePreviewTriggerState,
  SourceUnavailableReason,
  TaskSourceFiles,
} from "@/types/sourcePreview";

export function useSourcePreview({
  taskId,
  workflowRevision,
  sourceKind,
  sourceId = null,
  displayName,
}: {
  taskId?: string | null;
  workflowRevision?: number | null;
  sourceKind: "problem" | "submission";
  sourceId?: string | null;
  displayName?: string | null;
}) {
  const [catalogState, setCatalogState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [catalog, setCatalog] = useState<TaskSourceFiles | null>(null);
  const [catalogScopeKey, setCatalogScopeKey] = useState<string | null>(null);
  const [catalogErrorCode, setCatalogErrorCode] = useState<SourcePreviewErrorCode | null>(null);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const requestedCatalogScopeKey = `${taskId ?? "no-task"}:${workflowRevision ?? "unknown-revision"}`;

  useEffect(() => {
    let cancelled = false;
    setCatalog(null);
    setCatalogScopeKey(null);
    setCatalogErrorCode(null);
    if (!taskId) {
      setCatalogState("ready");
      return () => {
        cancelled = true;
      };
    }
    setCatalogState("loading");
    void getTaskSourceFiles(taskId)
      .then((nextCatalog) => {
        if (cancelled) return;
        setCatalog(nextCatalog);
        setCatalogScopeKey(requestedCatalogScopeKey);
        setCatalogState("ready");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setCatalogErrorCode(sourcePreviewErrorCode(error));
        setCatalogState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [catalogAttempt, requestedCatalogScopeKey, taskId]);

  const descriptor = useMemo<SourceFileDescriptor | null>(() => {
    if (!catalog || catalog.task_id !== taskId || catalogScopeKey !== requestedCatalogScopeKey) return null;
    if (sourceKind === "problem") return catalog.problem_source;
    if (!sourceId) return null;
    const selected = catalog.submission_sources[sourceId] ?? null;
    return selected?.source_id === sourceId ? selected : null;
  }, [catalog, catalogScopeKey, requestedCatalogScopeKey, sourceId, sourceKind, taskId]);

  useEffect(() => {
    if (descriptor?.status !== "processing") return;
    const timer = window.setTimeout(() => setCatalogAttempt((current) => current + 1), 3_000);
    return () => window.clearTimeout(timer);
  }, [descriptor?.status]);
  const resolvedDisplayName = descriptor?.display_name.trim() || displayName?.trim() || "";
  const previewKind = descriptor?.preview_kind
    ?? inferSourcePreviewKind(resolvedDisplayName, descriptor?.mime_type);
  const catalogUnavailableReason: SourceUnavailableReason | null = catalogState === "error"
    ? catalogErrorCode === "source_preview_storage_unavailable"
      ? "storage_unavailable"
      : catalogErrorCode === "source_preview_not_found"
        ? "missing"
        : null
    : null;
  const unavailableReason: SourceUnavailableReason | null = descriptor?.unavailable_reason
    ?? catalogUnavailableReason
    ?? (catalogState === "ready" && !descriptor
      ? "missing"
      : !resolvedDisplayName && catalogState !== "loading"
        ? "missing"
        : previewKind === "unsupported" && catalogState === "ready"
          ? "unsupported_type"
          : null);
  const triggerState: SourcePreviewTriggerState = catalogState === "loading" || catalogState === "idle"
    ? "processing"
    : catalogState === "error" && resolvedDisplayName
      ? "ready"
      : unavailableReason
        ? "unavailable"
        : descriptor?.status === "processing"
          ? "processing"
          : descriptor?.status === "unavailable"
            ? "unavailable"
            : "ready";
  const sourceKey = `${requestedCatalogScopeKey}:${sourceKind}:${sourceId ?? "no-source"}:${descriptor?.file_id ?? "no-file"}:${descriptor?.status ?? catalogState}:${resolvedDisplayName}:${previewKind}`;
  const [isOpen, setIsOpen] = useState(false);
  const [loadState, setLoadState] = useState<SourcePreviewLoadState>("idle");
  const [errorCode, setErrorCode] = useState<SourcePreviewErrorCode | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewStateSourceKey, setPreviewStateSourceKey] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const resourceUrlRef = useRef<string | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const releaseResource = useCallback(() => {
    if (!resourceUrlRef.current) return;
    if (typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(resourceUrlRef.current);
    resourceUrlRef.current = null;
  }, []);

  useEffect(() => {
    setAttempt(0);
  }, [sourceKey]);

  useEffect(() => {
    let cancelled = false;
    setPreviewStateSourceKey(sourceKey);
    releaseResource();
    setPreviewUrl(null);
    setErrorCode(null);

    if (!isOpen) {
      setLoadState("idle");
      return () => {
        cancelled = true;
      };
    }

    if (catalogState === "loading" || catalogState === "idle") {
      setLoadState("loading");
      return () => {
        cancelled = true;
      };
    }

    if (catalogState === "error") {
      setErrorCode(catalogErrorCode ?? "source_preview_load_failed");
      setLoadState("error");
      return () => {
        cancelled = true;
      };
    }

    if (triggerState !== "ready" || !taskId || !descriptor) {
      setLoadState("idle");
      return () => {
        cancelled = true;
      };
    }

    setLoadState("loading");
    void loadSourcePreviewFile(taskId, descriptor)
      .then((blob) => {
        if (cancelled) return;
        if (typeof URL.createObjectURL !== "function") throw new Error("object_url_unavailable");
        const url = URL.createObjectURL(blob);
        resourceUrlRef.current = url;
        setPreviewUrl(url);
        setLoadState("ready");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setErrorCode(sourcePreviewErrorCode(error));
        setLoadState("error");
      });

    return () => {
      cancelled = true;
      releaseResource();
    };
  }, [attempt, catalogErrorCode, catalogState, descriptor, isOpen, releaseResource, sourceKey, taskId, triggerState]);

  useEffect(() => () => releaseResource(), [releaseResource]);

  const openPreview = useCallback(() => {
    if (triggerState === "unavailable") return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setIsOpen(true);
  }, [triggerState]);

  const closePreview = useCallback(() => {
    setIsOpen(false);
    window.requestAnimationFrame(() => openerRef.current?.focus());
  }, []);

  const retryPreview = useCallback(() => {
    if (catalogState === "error") {
      setCatalogAttempt((current) => current + 1);
      return;
    }
    setAttempt((current) => current + 1);
  }, [catalogState]);

  const previewStateIsCurrent = previewStateSourceKey === sourceKey;

  return {
    descriptor,
    displayName: resolvedDisplayName,
    previewKind,
    triggerState,
    unavailableReason,
    isOpen,
    loadState: previewStateIsCurrent ? loadState : isOpen ? "loading" : "idle",
    errorCode: previewStateIsCurrent ? errorCode : null,
    previewUrl: previewStateIsCurrent ? previewUrl : null,
    openPreview,
    closePreview,
    retryPreview,
  };
}
