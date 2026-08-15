import { useCallback, useEffect, useRef, useState } from "react";
import { loadSourcePreviewFile, sourcePreviewErrorCode } from "@/api/sourcePreview";
import { inferSourcePreviewKind } from "@/lib/sourcePreview";
import type {
  SourceFileDescriptor,
  SourcePreviewErrorCode,
  SourcePreviewLoadState,
  SourcePreviewTriggerState,
  SourceUnavailableReason,
} from "@/types/sourcePreview";

export function useSourcePreview({
  descriptor = null,
  displayName,
}: {
  descriptor?: SourceFileDescriptor | null;
  displayName?: string | null;
}) {
  const resolvedDisplayName = descriptor?.display_name.trim() || displayName?.trim() || "";
  const previewKind = descriptor?.preview_kind
    ?? inferSourcePreviewKind(resolvedDisplayName, descriptor?.mime_type);
  const unavailableReason: SourceUnavailableReason | null = descriptor?.unavailable_reason
    ?? (!resolvedDisplayName ? "missing" : previewKind === "unsupported" ? "unsupported_type" : null);
  const triggerState: SourcePreviewTriggerState = unavailableReason
    ? "unavailable"
    : descriptor?.status === "processing"
      ? "processing"
      : descriptor?.status === "unavailable"
        ? "unavailable"
        : "ready";
  const sourceKey = `${descriptor?.file_id ?? "not-connected"}:${descriptor?.status ?? "unknown"}:${resolvedDisplayName}:${previewKind}`;
  const [isOpen, setIsOpen] = useState(false);
  const [loadState, setLoadState] = useState<SourcePreviewLoadState>("idle");
  const [errorCode, setErrorCode] = useState<SourcePreviewErrorCode | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
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
    releaseResource();
    setPreviewUrl(null);
    setErrorCode(null);

    if (!isOpen || triggerState !== "ready") {
      setLoadState("idle");
      return () => {
        cancelled = true;
      };
    }

    setLoadState("loading");
    void loadSourcePreviewFile(descriptor)
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
  }, [attempt, descriptor, isOpen, releaseResource, sourceKey, triggerState]);

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
    setAttempt((current) => current + 1);
  }, []);

  return {
    descriptor,
    displayName: resolvedDisplayName,
    previewKind,
    triggerState,
    unavailableReason,
    isOpen,
    loadState,
    errorCode,
    previewUrl,
    openPreview,
    closePreview,
    retryPreview,
  };
}
