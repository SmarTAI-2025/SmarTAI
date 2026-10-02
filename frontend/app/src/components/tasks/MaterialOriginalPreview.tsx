import { FileSearch } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { getBlob } from "@/api/client";
import { sourcePreviewErrorCode } from "@/api/sourcePreview";
import { useI18n } from "@/i18n/I18nProvider";
import { inferSourcePreviewKind } from "@/lib/sourcePreview";
import type { SourcePreviewErrorCode, SourcePreviewLoadState } from "@/types/sourcePreview";
import { OriginalFilePreviewPanel } from "./OriginalFilePreviewPanel";

export function MaterialOriginalPreview({ taskId, jobId, filename }: {
  taskId: string; jobId: string; filename: string;
}) {
  const { locale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<SourcePreviewLoadState>("idle");
  const [error, setError] = useState<SourcePreviewErrorCode | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const kind = inferSourcePreviewKind(filename);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let resource: string | null = null;
    setState("loading");
    setError(null);
    setUrl(null);
    void getBlob(`/tasks/${encodeURIComponent(taskId)}/material-imports/${encodeURIComponent(jobId)}/source`, {
      signal: controller.signal,
    }).then((blob) => {
      if (controller.signal.aborted) return;
      if (inferSourcePreviewKind(filename, blob.type) !== kind) throw new Error("Source type changed");
      resource = URL.createObjectURL(blob);
      setUrl(resource);
      setState("ready");
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(sourcePreviewErrorCode(reason));
      setState("error");
    });
    return () => {
      controller.abort();
      if (resource) URL.revokeObjectURL(resource);
    };
  }, [open, attempt, taskId, jobId, filename, kind]);

  if (kind === "unsupported") return null;
  return <div className="my-3">
    <button ref={button} type="button" className="inline-flex h-8 items-center gap-2 text-xs font-semibold text-primary" onClick={() => setOpen(true)}>
      <FileSearch aria-hidden="true" className="h-4 w-4" />
      {locale === "zh-CN" ? "核对原件" : "Review original"}
    </button>
    {open ? <OriginalFilePreviewPanel descriptor={null} displayName={filename} previewKind={kind}
      loadState={state} errorCode={error} previewUrl={url} t={t}
      onClose={() => { setOpen(false); button.current?.focus(); }}
      onRetry={() => setAttempt((value) => value + 1)} /> : null}
  </div>;
}
