import { useI18n } from "@/i18n/I18nProvider";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useDraftOwner } from "@/hooks/useDraftProtection";
import { draftError, listPageDrafts, removePageDraft, subscribeDraftChanges } from "@/lib/pageDraftStore";
export function draftDestination(scope: string) {
  let parts: string[];
  try { parts = scope.split(":").map(decodeURIComponent); } catch { return null; }
  const [kind, taskId, objectId] = parts;
  const task = `/tasks/${encodeURIComponent(taskId ?? "")}`;
  if (["answers", "results-review"].includes(kind)) {
    const [, , studentId, questionId] = parts;
    return kind === "answers" ? `${task}/students/${encodeURIComponent(studentId ?? "")}?question=${encodeURIComponent(questionId ?? "")}` : `${task}/review/${encodeURIComponent(studentId ?? "")}/${encodeURIComponent(questionId ?? "all")}`;
  }
  if (scope === "new-task") return "/tasks/new";
  if (kind.startsWith("library-")) return `/knowledge-base?localDraft=${encodeURIComponent(scope)}`;
  const routes: Record<string, string> = { metadata: `${task}/edit`, problems: `${task}/upload/problems`, submissions: `${task}/submissions/upload`, grading: `${task}/grading-setup`, "ai-completion": `${task}/questions/ai-complete`, "material-import": `${task}/questions/import`, "material-review": `${task}/questions/import/review/${encodeURIComponent(objectId ?? "")}`, question: `${task}/questions/${encodeURIComponent(objectId ?? "")}/content`, answers: `${task}/students/${encodeURIComponent(parts.slice(2).join(":"))}`, identity: `${task}/students/${encodeURIComponent(parts.slice(2).join(":"))}?identity=edit`, "results-review": `${task}/review/${encodeURIComponent(parts.slice(2).join(":"))}/all`, charts: `${task}/results/visualizations`, "history-tags": "/history" };
  return routes[kind] ?? null;
}
export function LocalDraftManager({ libraryOnly = false }: { libraryOnly?: boolean }) {
  const { locale } = useI18n(); const zh = locale === "zh-CN";
  const owner = useDraftOwner(); const [rows, setRows] = useState<Awaited<ReturnType<typeof listPageDrafts>>>([]); const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    function refresh() { if (owner) void listPageDrafts(owner).then((next) => { if (!disposed) setRows(next); }).catch((failure) => { if (!disposed) setError(draftError(failure)); }); }
    refresh(); const unsubscribe = subscribeDraftChanges(refresh);
    return () => { disposed = true; unsubscribe(); };
  }, [owner]);
  const visible = rows.filter((row) => !libraryOnly || row.scope.startsWith("library-"));
  if (libraryOnly && !visible.length && !error) return null;
  return <section aria-label={zh ? "本机已暂存草稿" : "Saved drafts on this device"} className="my-5 rounded-lg border bg-card p-4">
    <h2 className="text-base font-semibold">{zh ? "本机已暂存草稿" : "Saved drafts on this device"}</h2>
    <p className="mt-2 text-xs text-muted-foreground">{zh ? "仅属于当前账号，保存 7 天；正式提交成功后删除对应草稿。退出、切换账号或会话失效会清理本机草稿，请先正式保存需要长期保留的内容。" : "For this account only, kept for 7 days and removed after successful submission. Sign-out, account changes and session expiry clear local drafts. Submit work you need to keep long term."}</p>
    {!visible.length ? <p className="mt-3 text-sm">{zh ? "没有有效的本地草稿。" : "No valid local drafts."}</p> : <ul className="mt-3 space-y-2">{visible.map((row) => <li key={row.scope} className="flex flex-wrap items-center justify-between gap-2 border-t pt-2 text-sm"><div className="min-w-0 break-all">{draftDestination(row.scope) ? <Link className="text-primary underline" to={draftDestination(row.scope)!}>{zh ? "打开草稿" : "Open draft"} · {row.scope}</Link> : row.scope}<p className="mt-1 text-xs text-muted-foreground">{new Date(row.savedAt).toLocaleString()} · {(row.bytes / 1048576).toFixed(2)} MiB</p></div><button type="button" className="rounded border px-3 py-2" onClick={() => { if (owner && window.confirm("删除这份本地草稿？正式业务结果不受影响。")) void removePageDraft(owner, row.scope).catch((failure) => setError(draftError(failure))); }}>{zh ? "删除" : "Delete"}</button></li>)}</ul>}
    {error ? <p role="alert" className="mt-3 text-sm text-danger">{error}</p> : null}
  </section>;
}
