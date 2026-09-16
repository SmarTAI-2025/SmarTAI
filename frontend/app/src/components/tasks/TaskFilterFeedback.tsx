import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import type { TaskFilterController } from "@/hooks/useTaskFilterIntent";
import { useI18n } from "@/i18n/I18nProvider";
import { classifyRecoverableError } from "@/lib/taskActionGuards";

export function TaskFilterFeedback({ filter, taskId }: { filter: TaskFilterController; taskId?: string }) {
  const { locale } = useI18n();
  if (filter.error) return <RecoverableActionState compact locale={locale} info={classifyRecoverableError(filter.error, { locale, phase: "analytics_filter_intent", returnTo: taskId ? `/tasks/${encodeURIComponent(taskId)}` : "/history" })} primaryAction={{ label: locale === "zh-CN" ? "重新尝试" : "Try again", onClick: () => void filter.apply() }} />;
  if (filter.unrecognized) return <p role="status" className="mt-2 text-xs text-amber-700">{filter.explanation || (locale === "zh-CN" ? "暂时无法完整理解此条件，请换一种表达；未应用部分筛选。" : "This request could not be fully understood. No partial filter was applied.")}</p>;
  if (filter.pending) return <p role="status" className="mt-2 text-xs text-muted-foreground">{locale === "zh-CN" ? "SmarTAI 正在理解筛选与排序…" : "SmarTAI is interpreting your filter and sort…"}</p>;
  if (filter.resolution === "local") return <p role="status" className="mt-2 text-xs text-muted-foreground">{locale === "zh-CN" ? "已本地匹配，未调用模型。" : "Matched locally; no model call."}</p>;
  if (filter.explanation) return <p role="status" className="mt-2 text-xs text-muted-foreground">{filter.explanation}</p>;
  return null;
}
