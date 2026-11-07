import { useSearchParams } from "react-router-dom";
import { useTaskListDeletion } from "@/components/history/TaskListActions";
import { GradingHistoryPanel } from "@/components/history/GradingHistoryPanel";
import { KnowledgeActivityPanel } from "@/components/knowledge-base/KnowledgeActivityPanel";
import { LocalDraftManager } from "@/components/ui/LocalDraftManager";
import { useI18n } from "@/i18n/I18nProvider";

export function HistoryPage() {
  const { locale, t } = useI18n();
  const deletion = useTaskListDeletion();
  const [params, setParams] = useSearchParams();
  const tabs = ["grading", "knowledge", "drafts"] as const;
  const view = tabs.find(value => value === params.get("view")) ?? "grading";
  function choose(value: typeof tabs[number]) {
    const next = new URLSearchParams(params);
    if (value === "grading") next.delete("view"); else next.set("view", value);
    setParams(next);
  }
  return <div className="w-full max-w-[1290px]">
    <h1 className="text-[30px] font-bold leading-9 text-foreground">{t("historyTitle")}</h1>
    <div role="tablist" aria-label={locale === "zh-CN" ? "任务类型" : "Task type"} className="mt-4 flex gap-5 border-b">
      {tabs.map((value, index) => <button key={value} type="button" role="tab" aria-selected={view === value}
        tabIndex={view === value ? 0 : -1}
        id={`${value}-history-tab`} aria-controls="history-content"
        onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1] : tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
          choose(next);
          document.getElementById(`${next}-history-tab`)?.focus();
        }}
        onClick={() => choose(value)} className={`border-b-2 py-2 text-sm font-medium ${view === value ? "border-primary text-primary" : "border-transparent text-muted-foreground"}`}>
        {value === "drafts" ? (locale === "zh-CN" ? "暂存草稿" : "Saved drafts") : value === "knowledge" ? (locale === "zh-CN" ? "教材识别" : "Textbook recognition") : (locale === "zh-CN" ? "作业批改" : "Grading")}
      </button>)}
    </div>
    <div id="history-content" role="tabpanel" aria-labelledby={`${view}-history-tab`} className="mt-5">
      {view === "drafts" ? <LocalDraftManager /> : view === "knowledge" ? <KnowledgeActivityPanel /> : <GradingHistoryPanel deletion={deletion} />}
    </div>
    {deletion.dialog}
  </div>;
}
