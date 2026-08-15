import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { buildSourcePreviewMockScenario, type SourcePreviewMockVariant } from "@/lib/sourcePreview";
import type { SourcePreviewLoadState } from "@/types/sourcePreview";

export function useMockSourcePreview({
  scope,
  sourceId,
  displayName,
  taskFinalized,
  variant,
}: {
  scope: "problem" | "submission";
  sourceId: string;
  displayName?: string | null;
  taskFinalized: boolean;
  variant: SourcePreviewMockVariant;
}) {
  const scenario = useMemo(() => buildSourcePreviewMockScenario({
    scope,
    sourceId,
    displayName,
    taskFinalized,
    variant,
  }), [displayName, scope, sourceId, taskFinalized, variant]);
  const scenarioKey = `${scenario.descriptor.file_id}:${scenario.descriptor.status}:${scenario.descriptor.preview_kind}:${scenario.variant ?? "default"}`;
  const [isOpen, setIsOpen] = useState(false);
  const [loadState, setLoadState] = useState<SourcePreviewLoadState>("idle");
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
  }, [scenarioKey]);

  useEffect(() => {
    let cancelled = false;
    releaseResource();
    setPreviewUrl(null);

    if (!isOpen || scenario.descriptor.status !== "available") {
      setLoadState("idle");
      return () => {
        cancelled = true;
      };
    }
    if (!import.meta.env.DEV && import.meta.env.MODE !== "test") {
      setLoadState("idle");
      return () => {
        cancelled = true;
      };
    }

    setLoadState("loading");
    void import("@/mocks/sourcePreview")
      .then(({ loadSourcePreviewMock }) => loadSourcePreviewMock(scenario, attempt))
      .then((blob) => {
        if (cancelled) return;
        if (typeof URL.createObjectURL !== "function") throw new Error("object_url_unavailable");
        const url = URL.createObjectURL(blob);
        resourceUrlRef.current = url;
        setPreviewUrl(url);
        setLoadState("ready");
      })
      .catch(() => {
        if (!cancelled) setLoadState("error");
      });

    return () => {
      cancelled = true;
      releaseResource();
    };
  }, [attempt, isOpen, releaseResource, scenario, scenarioKey]);

  useEffect(() => () => releaseResource(), [releaseResource]);

  const openPreview = useCallback(() => {
    if (scenario.descriptor.status === "unavailable") return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setIsOpen(true);
  }, [scenario.descriptor.status]);

  const closePreview = useCallback(() => {
    setIsOpen(false);
    window.requestAnimationFrame(() => openerRef.current?.focus());
  }, []);

  const retryPreview = useCallback(() => {
    setAttempt((current) => current + 1);
  }, []);

  return {
    descriptor: scenario.descriptor,
    isOpen,
    loadState,
    previewUrl,
    openPreview,
    closePreview,
    retryPreview,
  };
}
