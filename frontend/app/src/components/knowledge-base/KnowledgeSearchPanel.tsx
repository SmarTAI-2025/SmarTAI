import { LoaderCircle, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { postJSON } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { KnowledgeCitationPreview } from "./KnowledgeCitationPreview";
import type { KnowledgeMatch } from "@/types/knowledgeCitation";

export function KnowledgeSearchPanel({ documentIds }: { documentIds: string[] }) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<KnowledgeMatch[] | null>(null);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const generation = useRef(0);
  const scope = JSON.stringify([...new Set(documentIds)].sort());
  useEffect(() => { generation.current += 1; setMatches(null); setPending(false); setFailed(false); }, [scope]);
  useEffect(() => () => { generation.current += 1; }, []);
  const ids = JSON.parse(scope) as string[];
  if (!ids.length) return null;
  return <section className="mt-4 min-w-0 border-t pt-3" aria-label={zh ? "资料内容检索" : "Search material content"}>
    <form className="flex items-center gap-2" onSubmit={(event) => {
      event.preventDefault();
      if (!query.trim() || pending || ids.length > 20) return;
      const current = ++generation.current;
      setPending(true); setFailed(false); setMatches(null);
      void postJSON<{ matches: KnowledgeMatch[] }>("/knowledge/search", { query: query.trim(), document_ids: ids, limit: 5 })
        .then((result) => { if (current === generation.current) setMatches(result.matches); })
        .catch(() => { if (current === generation.current) setFailed(true); })
        .finally(() => { if (current === generation.current) setPending(false); });
    }}>
      <input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={4000}
        aria-label={zh ? "检索词或题目" : "Search terms or question"} placeholder={zh ? "检索资料内容" : "Search material content"}
        className="h-9 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm" />
      <button type="submit" disabled={pending || !query.trim() || ids.length > 20} title={zh ? "检索" : "Search"}
        aria-label={zh ? "检索" : "Search"} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border text-primary disabled:opacity-50">
        {pending ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Search aria-hidden="true" className="h-4 w-4" />}
      </button>
    </form>
    {ids.length > 20 ? <p className="mt-2 text-xs text-muted-foreground">{zh ? "请将范围缩小到 20 份资料以内。" : "Narrow the selection to at most 20 materials."}</p> : null}
    {failed ? <p role="alert" className="mt-2 text-xs text-danger">{zh ? "检索暂不可用，请稍后重试或缩小资料范围。" : "Search unavailable. Retry or narrow the material selection."}</p> : null}
    {matches?.length === 0 ? <p role="status" className="mt-2 text-xs text-muted-foreground">{zh ? "已入库内容中未找到相关片段。" : "No matching passages in indexed content."}</p> : null}
    <ol className="mt-3 divide-y">
      {matches?.map((match) => <li key={match.citation.citation_id} className="min-w-0 py-3">
        <MarkdownMath className="break-words text-sm leading-6">{match.content}</MarkdownMath>
        <KnowledgeCitationPreview citation={match.citation} />
      </li>)}
    </ol>
  </section>;
}
