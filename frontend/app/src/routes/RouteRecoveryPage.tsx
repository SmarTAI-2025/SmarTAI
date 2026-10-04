import { useDraftLeave } from "@/hooks/useDraftLeave";
import { ArrowLeft, History, RefreshCw } from "lucide-react";
import { useLocation } from "react-router-dom";
import { workflowBackLabel, workflowRetryLabel } from "@/components/ui/RecoverableActionState";
import { useI18n } from "@/i18n/I18nProvider";

export function RouteRecoveryPage() {
  const leave = useDraftLeave();
  const { locale } = useI18n();
  const { pathname } = useLocation();
  const taskRoute = pathname.match(/^\/tasks\/([^/]+)\/(.+)$/);
  const stage = taskRoute?.[2];
  const configurationHref = taskRoute && stage
    ? stage.startsWith("problems/") || stage === "upload/problems" ? `/tasks/${taskRoute[1]}/upload/problems`
      : stage.startsWith("submissions/") ? `/tasks/${taskRoute[1]}/submissions/upload`
        : stage.startsWith("grading") ? `/tasks/${taskRoute[1]}/grading-setup`
          : null
    : null;
  const zh = locale === "zh-CN";
  return (
    <main className="mx-auto grid min-h-[60vh] max-w-2xl content-center gap-5 px-6 py-12" role="alert">
      <h1 className="text-2xl font-semibold">{zh ? "页面暂时无法显示" : "This page could not be displayed"}</h1>
      <p className="text-sm leading-6 text-muted-foreground">
        {zh ? "已提交的任务资料不会因此删除。可以重新加载页面，或从历史任务继续；重新加载不会自动重新识别或批改。"
          : "Submitted task materials are not deleted. Reload this page or continue from task history. Reloading does not automatically restart recognition or grading."}
      </p>
      <div className="flex flex-wrap gap-3">
        <button type="button" onClick={() => leave.request(() => window.location.reload())} className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">
          <RefreshCw className="h-4 w-4" aria-hidden="true" />{configurationHref ? workflowRetryLabel(locale) : zh ? "重新加载页面" : "Reload page"}
        </button>
        {configurationHref ? <a href={configurationHref} className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium">
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />{workflowBackLabel(locale)}
        </a> : null}
        <a href="/history" className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium">
          <History className="h-4 w-4" aria-hidden="true" />{zh ? "返回历史任务" : "Task history"}
        </a>
      </div>
    </main>
  );
}
