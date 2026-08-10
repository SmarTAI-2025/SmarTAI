import { useCallback, useEffect, useMemo, useState } from "react";
import type { SourceFileDescriptor, SourcePreviewLoadState } from "@/types/sourcePreview";

const FRONTIER_TASK_PREFIXES = [
  "SmarTAI Live Demo",
  // Read-only compatibility for tasks created before the public-brand cleanup.
  "AWS Frontier Live Demo",
] as const;
const MANIFEST_URL = "/frontier-demo/manifest.json";

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

interface Manifest {
  assets: Array<{ path: string; sha256: string }>;
}

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
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    setOpen(false);
    setLoadState("idle");
    setPreviewUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return null;
    });
  }, [source?.path]);

  useEffect(() => {
    if (!open || !source) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoadState("loading");
    Promise.all([
      fetch(`/frontier-demo/${source.path}`, { cache: "no-store" }),
      fetch(MANIFEST_URL, { cache: "no-store" }),
    ])
      .then(async ([fileResponse, manifestResponse]) => {
        if (!fileResponse.ok) throw new Error(`Fixture request failed (${fileResponse.status})`);
        if (!manifestResponse.ok) throw new Error(`Manifest request failed (${manifestResponse.status})`);
        const [blob, manifest] = await Promise.all([
          fileResponse.blob(),
          manifestResponse.json() as Promise<Manifest>,
        ]);
        const expected = manifest.assets.find((asset) => asset.path === source.path)?.sha256;
        if (!expected) throw new Error("Fixture is missing from the manifest");
        const actual = await sha256Hex(await blob.arrayBuffer());
        if (actual !== expected) throw new Error("Fixture integrity check failed");
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        setPreviewUrl((current) => {
          if (current) URL.revokeObjectURL(current);
          return objectUrl;
        });
        setLoadState("ready");
      })
      .catch(() => {
        if (!cancelled) setLoadState("error");
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attempt, open, source]);

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
    setPreviewUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return null;
    });
    setAttempt((current) => current + 1);
  }, []);

  return {
    available: Boolean(source),
    descriptor,
    loadState,
    open,
    previewUrl,
    close: () => setOpen(false),
    openPreview: () => setOpen(true),
    retry,
  };
}

async function sha256Hex(value: ArrayBuffer) {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
