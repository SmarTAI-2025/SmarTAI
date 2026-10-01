import { FileSearch } from "lucide-react";
import { useEffect, useState } from "react";
import { getBlob, getJSON } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { inferSourcePreviewKind } from "@/lib/sourcePreview";
import { OriginalFilePreviewPanel } from "@/components/tasks/OriginalFilePreviewPanel";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import type { KnowledgeCitation } from "@/types/knowledgeCitation";

export function KnowledgeCitationPreview({ citation }: { citation: KnowledgeCitation }) {
  const { locale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const kind = inferSourcePreviewKind(citation.original_name);
  const needsReview = citation.coverage_complete === false || Boolean(citation.warning_codes?.length)
    || citation.confidence === "low" || citation.confidence === "unverified";
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let resource: string | null = null;
    setUrl(null); setText(null); setFailed(false);
    void (async () => {
      const evidence = await getJSON<{ content: string; content_version: string; source_sha256: string }>(
        `/knowledge/documents/${encodeURIComponent(citation.document_id)}/citations/${encodeURIComponent(citation.chunk_id)}`, { signal: controller.signal });
      if (evidence.content_version !== citation.content_version || evidence.source_sha256 !== citation.source_sha256) throw new Error("Citation version unavailable");
      if (controller.signal.aborted) return;
      setText(evidence.content);
      if (kind !== "unsupported") {
        const blob = await getBlob(`/knowledge/documents/${encodeURIComponent(citation.document_id)}/citations/${encodeURIComponent(citation.chunk_id)}/download`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        resource = URL.createObjectURL(blob); setUrl(resource);
      }
    })().catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => { controller.abort(); if (resource) URL.revokeObjectURL(resource); };
  }, [open, attempt, citation.document_id, citation.chunk_id, citation.content_version, citation.source_sha256, kind]);
  return <div className="min-w-0">
    <button type="button" aria-expanded={open} className="inline-flex max-w-full items-center gap-1 py-1 text-left text-xs text-primary" onClick={() => setOpen(!open)}>
      <FileSearch aria-hidden="true" className="h-4 w-4 shrink-0" /><span className="min-w-0 [overflow-wrap:anywhere]">{citation.original_name}{citation.page_number ? ` · ${citation.unit === "page" ? (locale === "zh-CN" ? "页" : "p.") : citation.unit} ${citation.page_number}` : ""}</span>
    </button>
    {needsReview ? <p className="break-words text-xs text-amber-700 dark:text-amber-300">
      {locale === "zh-CN" ? "原文覆盖不完整或识别待核对。" : "Incomplete coverage or unverified recognition."}
    </p> : null}
    {open ? <div className="mt-2 space-y-2">
      {text && kind === "unsupported" ? <MarkdownMath className="text-xs leading-5">{text}</MarkdownMath> : null}
      {kind !== "unsupported" ? <OriginalFilePreviewPanel descriptor={null} displayName={citation.original_name} previewKind={kind}
        initialPage={citation.unit === "page" ? citation.page_number ?? 1 : 1}
        loadState={failed ? "error" : url ? "ready" : "loading"} errorCode={failed ? "source_preview_load_failed" : null}
        previewUrl={url} t={t} onClose={() => setOpen(false)} onRetry={() => setAttempt((value) => value + 1)} />
        : failed ? <p role="alert">{locale === "zh-CN" ? "引用内容暂不可用" : "Citation unavailable"}</p> : null}
    </div> : null}
  </div>;
}
