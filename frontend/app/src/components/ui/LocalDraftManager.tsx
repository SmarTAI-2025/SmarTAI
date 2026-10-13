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
function draftLabel(scope: string, zh: boolean) {
  const [kind, taskId, objectId, questionId] = scope.split(":").map(part => { try { return decodeURIComponent(part); } catch { return part; } });
  const names: Record<string, [string, string]> = {
    "new-task": ["新建任务", "New task"], metadata: ["任务信息", "Task details"], problems: ["题目文件", "Question sources"], submissions: ["学生作答上传", "Submission upload"], grading: ["批改设置", "Grading setup"], question: ["题目资料编辑", "Question materials"], answers: ["学生作答编辑", "Student answer"], identity: ["学生身份", "Student identity"], "results-review": ["教师改分与评语", "Teacher review"], "material-import": ["资料导入", "Material import"], "material-review": ["资料导入核对", "Import review"], "ai-completion": ["AI 补全选项", "AI completion options"], charts: ["图表分析", "Chart analysis"], "history-tags": ["任务标签", "Task tags"], "library-upload": ["资料库上传", "Library upload"], "library-group": ["资料分组", "Library group"], "library-material": ["资料信息", "Material details"], "admin-config": ["管理业务设置", "Administrative settings"],
  };
  const title = names[kind]?.[zh ? 0 : 1] ?? (zh ? "业务草稿" : "Business draft");
  if (!taskId || taskId === "new") return title;
  return [title, taskId.slice(-12), objectId, ["answers", "results-review"].includes(kind) ? questionId : null].filter(Boolean).join(" · ");
}
const EXPIRING_SOON_MS = 2 * 24 * 60 * 60 * 1000;
type DraftSummary = Awaited<ReturnType<typeof listPageDrafts>>[number];
type LocalDraftManagerProps = {
  libraryOnly?: boolean;
  maxItems?: number;
  showViewAll?: boolean;
};

export function LocalDraftManager({ libraryOnly = false, maxItems, showViewAll = false }: LocalDraftManagerProps) {
  const { locale, t } = useI18n();
  const zh = locale === "zh-CN";
  const owner = useDraftOwner();
  const [snapshot, setSnapshot] = useState<{ owner: string | null; rows: DraftSummary[]; error: string; loading: boolean }>({ owner: null, rows: [], error: "", loading: true });
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    let disposed = false;
    let request = 0;
    setSnapshot({ owner, rows: [], error: "", loading: Boolean(owner) });
    function refresh() {
      if (!owner) return;
      const current = ++request;
      void listPageDrafts(owner).then((rows) => {
        if (!disposed && current === request) setSnapshot({ owner, rows, error: "", loading: false });
      }).catch((failure) => {
        if (!disposed && current === request) setSnapshot({ owner, rows: [], error: draftError(failure), loading: false });
      });
    }
    refresh();
    const unsubscribe = subscribeDraftChanges(event => {
      if ((event as CustomEvent<{ type: string }>)?.detail?.type === "clear") {
        request += 1;
        setSnapshot({ owner, rows: [], error: "", loading: false });
      } else refresh();
    });
    window.addEventListener("focus", refresh);
    return () => { disposed = true; unsubscribe(); window.removeEventListener("focus", refresh); };
  }, [owner]);
  const rows = snapshot.owner === owner ? snapshot.rows : [];
  const error = snapshot.owner === owner ? snapshot.error : "";
  const loading = Boolean(owner) && (snapshot.owner !== owner || snapshot.loading);
  // Expiry affects only this listing. The existing draft store owns retention and recovery.
  useEffect(() => {
    const nextChange = rows.flatMap(row => [row.expires, row.expires - EXPIRING_SOON_MS]).filter(time => time > now).sort((a, b) => a - b)[0];
    if (!nextChange) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(1, nextChange - Date.now()));
    return () => window.clearTimeout(timer);
  }, [rows, now]);
  const currentTime = Math.max(now, Date.now());
  const sorted = rows.filter(row => row.expires > currentTime && (!libraryOnly || row.scope.startsWith("library-"))).sort((a, b) => {
    const aSoon = a.expires - currentTime <= EXPIRING_SOON_MS;
    const bSoon = b.expires - currentTime <= EXPIRING_SOON_MS;
    if (aSoon !== bSoon) return aSoon ? -1 : 1;
    return (aSoon ? a.expires - b.expires : b.savedAt - a.savedAt) || a.scope.localeCompare(b.scope);
  });
  const visible = maxItems === undefined ? sorted : sorted.slice(0, maxItems);
  if (libraryOnly && !visible.length && !error) return null;
  return <section aria-label={zh ? "暂存草稿" : "Saved drafts"} className="my-5 rounded-lg border bg-card p-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-base font-semibold">{zh ? "暂存草稿" : "Saved drafts"}</h2>
      {showViewAll ? <Link to="/history?view=drafts" className="text-sm font-medium text-primary hover:underline">{zh ? "查看全部草稿" : "View all drafts"}</Link> : null}
    </div>
    <p className="mt-2 text-xs text-muted-foreground">{zh ? "即将清理的草稿优先，其余按最近暂存排序。" : "Drafts expiring soon appear first, followed by recently saved drafts."}</p>
    <details className="mt-2 text-xs text-muted-foreground">
      <summary className="w-fit cursor-pointer">{zh ? "本机保存 7 天 · 保存说明" : "Kept on this device for 7 days · Storage details"}</summary>
      <p className="mt-2">{zh ? "仅属于当前账号；正式提交成功后删除对应草稿。退出、切换账号或会话失效会清理本机草稿，请先正式保存需要长期保留的内容。" : "For this account only, removed after successful submission. Sign-out, account changes and session expiry clear local drafts. Submit work you need to keep long term."}</p>
    </details>
    {loading ? <p role="status" className="mt-3 text-sm text-muted-foreground">{t("loading")}</p> : !visible.length && !error ? <p className="mt-3 text-sm text-muted-foreground">{zh ? "没有有效的本地草稿。" : "No valid local drafts."}</p> : <ul className="mt-3 space-y-2">{visible.map((row) => {
      const destination = draftDestination(row.scope);
      const expiringSoon = row.expires - currentTime <= EXPIRING_SOON_MS;
      return <li key={row.scope} className="flex flex-wrap items-center justify-between gap-3 border-t py-2 text-sm">
        <div className="min-w-0 flex-1 break-words">
          {destination ? <Link className="text-primary hover:underline" to={destination}>{zh ? "打开草稿" : "Open draft"} · {draftLabel(row.scope, zh)}</Link> : draftLabel(row.scope, zh)}
          <p className="mt-1 text-xs text-muted-foreground">{zh ? "暂存于 " : "Saved "}{new Date(row.savedAt).toLocaleString(locale)} · {(row.bytes / 1048576).toFixed(2)} MiB</p>
          <p className={`mt-1 text-xs ${expiringSoon ? "font-medium text-amber-700 dark:text-amber-300" : "text-muted-foreground"}`}>
            {expiringSoon ? (zh ? "即将清理 · " : "Expiring soon · ") : null}{zh ? "清理时间：" : "Expires: "}{new Date(row.expires).toLocaleString(locale)}
          </p>
        </div>
        <button type="button" className="shrink-0 rounded border px-3 py-2 hover:bg-muted" onClick={() => {
          if (owner && window.confirm(zh ? "删除这份本地草稿？正式业务结果不受影响。" : "Delete this local draft? Submitted business results are unchanged.")) {
            void removePageDraft(owner, row.scope).catch((failure) => setSnapshot(current => current.owner === owner ? { ...current, error: draftError(failure) } : current));
          }
        }}>{zh ? "删除" : "Delete"}</button>
      </li>;
    })}</ul>}
    {error ? <p role="alert" className="mt-3 text-sm text-danger">{error}</p> : null}
  </section>;
}
